"""Native item acquisition rules and conservative, version-pinned script editing."""

from collections import Counter, defaultdict
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import struct

from .binary import ContentError, encode_table, read_bytes
from .creatures import generation_chance
from .files import sha256
from .objects import find_unique, view_record
from .plot_maps import NATIVE_SHA256, WIDTHS, decode_script

SCRIPT_NAME = re.compile(r'TALK\d{4}\.EXE')
BRANCHES = {100, 108, 109, 110, 111, 112, 113}
# See research/2026-11-05/acquisition-native.json; unknown commands remain unwritable.
EXTRA_WIDTHS = {503: 2}


def probability(value) -> dict:
    """Quantize a requested percentage to the nearest native threshold, returning its true rate."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ContentError('掉落率必须是 0 至 100 的数值。')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 <= number <= 100:
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise ContentError('掉落率必须是 0 至 100 的有限数值。') from None
    threshold = min(range(-1, 100), key=lambda t: abs(Decimal(str(generation_chance(t))) - number))
    return {'requested_percent': float(number), 'threshold': threshold,
            'effective_percent': generation_chance(threshold)}


def normalize_acquisition(value) -> dict:
    """Validate the optional acquisition extension without changing legacy item operations."""
    if not isinstance(value, dict) or set(value) != {'drop', 'shops', 'rewards'}:
        raise ContentError('获取途径必须包含 drop、shops、rewards。')
    drop = value['drop']
    if drop is not None:
        if not isinstance(drop, dict) or set(drop) != {'monster_id', 'map_id', 'percent'}:
            raise ContentError('每件道具只能配置一个地图与怪物来源及掉落率。')
        if any(type(drop[k]) is not int or not 0 <= drop[k] <= 32767 for k in ('monster_id', 'map_id')) or drop['monster_id'] == 0:
            raise ContentError('怪物与地图编号无效。')
        probability(drop['percent'])
        drop = {**drop, 'percent': probability(drop['percent'])['requested_percent']}
    result = {'drop': drop, 'shops': [], 'rewards': []}
    for kind in ('shops', 'rewards'):
        entries = value[kind]
        if not isinstance(entries, list) or len(entries) > 20:
            raise ContentError('每类获取途径最多配置 20 项。')
        seen = set()
        for entry in entries:
            expected = {'node'} if kind == 'shops' else {'node', 'count'}
            if not isinstance(entry, dict) or set(entry) != expected:
                raise ContentError('商店或任务奖励配置字段无效。')
            node = entry['node']
            if not isinstance(node, str) or not re.fullmatch(r'TALK\d{4}\.EXE:\d{1,5}:\d{1,7}', node) or node in seen:
                raise ContentError('获取途径节点无效或重复。')
            seen.add(node)
            if kind == 'rewards' and (type(entry['count']) is not int or not 1 <= entry['count'] <= 32767):
                raise ContentError('任务奖励数量必须是 1 至 32767 的整数。')
            result[kind].append(dict(entry))
    return result


def instructions(program) -> list[tuple[int, tuple[int, ...]]]:
    """Decode a whole function; reject unknown lengths, invalid branches and truncated operands."""
    data, begin, end, *_ = program
    rows, pc = [], begin
    while pc < end:
        op = struct.unpack_from('<i', data, pc)[0]
        width = 1 if op == 0 else 2 if op in BRANCHES else EXTRA_WIDTHS.get(op, WIDTHS.get(op))
        if op in (11, 12, 402, 556, 806, 909, 917):
            count_offset = 16 if op == 402 else 4
            if pc + count_offset + 4 > end:
                raise ContentError('脚本变长指令截断。')
            count = struct.unpack_from('<i', data, pc + count_offset)[0]
            if not 0 <= count <= 4096:
                raise ContentError('脚本列表长度超限。')
            width = 5 + count if op == 402 else 2 + count * (2 if op in (806, 909, 917) else 1)
        if width is None or pc + width * 4 > end:
            raise ContentError(f'该脚本含尚未支持的指令 {op}，不能安全修改。')
        rows.append((pc, struct.unpack_from('<' + 'i' * width, data, pc)))
        pc += width * 4
    boundaries = {pc for pc, _ in rows} | {end}
    for _, words in rows:
        targets = words[1:2] if words[0] in BRANCHES else words[2:] if words[0] in (11, 12) else ()
        if any(target not in boundaries for target in targets):
            raise ContentError('脚本跳转未指向本函数指令边界。')
    if end - begin > 65520:
        raise ContentError('脚本函数超过原生 16 位指令位置范围。')
    return rows


class AcquisitionCatalog:
    """Read source scripts and map evidence without writing game files or accessing a database."""

    def __init__(self, loaded, creatures, directory: Path | None):
        self.loaded, self.creatures, self.directory = loaded, creatures, directory
        self.scripts, self.programs, self.nodes = {}, {}, {}
        self.script_error = ''
        if directory is not None:
            try:
                if sha256(read_bytes(directory / 'NewLocalServer.exe')) != NATIVE_SHA256:
                    raise ContentError('服务端程序版本未核验，不能修改商店或奖励脚本。')
                for path in sorted((directory / 'ASM').glob('TALK*.EXE')):
                    if not SCRIPT_NAME.fullmatch(path.name.upper()):
                        continue
                    blob = read_bytes(path)
                    if len(self.scripts) >= 64 or sum(map(len, self.scripts.values())) + len(blob) > 16 * 1024 * 1024:
                        raise ContentError('脚本集合超过安全读取上限。')
                    filename = path.name.upper()
                    self.scripts[filename] = blob
                    self.programs.update(decode_script(blob, filename))
                if not self.scripts:
                    raise ContentError('区域目录没有可读取的编号脚本。')
            except (ContentError, OSError) as exc:
                self.scripts.clear(); self.programs.clear()
                self.script_error = str(exc)
        else:
            self.script_error = '请在设置中配置含 ASM 的区域与脚本目录。'
        parsed = {}
        self.skipped = Counter()
        for pid, program in self.programs.items():
            try:
                parsed[pid] = instructions(program)
            except ContentError:
                self.skipped['unsupported_functions'] += 1
        owners = defaultdict(set)
        roots = {(pid, role['template_id'], role['map_id']) for role in creatures.roles.values()
                 for pid in role.get('plots', []) if pid >= 0}
        visited_owners = set()
        for root_pid, tid, zone in roots:
            pending = [root_pid]
            while pending:
                pid = pending.pop()
                context = (pid, tid, zone)
                if context in visited_owners or pid not in self.programs:
                    continue
                visited_owners.add(context)
                if len(visited_owners) > 100000:
                    raise ContentError('获取途径的 NPC 关联超过解析上限。')
                owners[pid].add((tid, zone))
                rows = parsed.get(pid, [])
                pending.extend(w[2] for _, w in rows if w[0] == 261 and 0 <= w[1] <= 3)
        for pid, rows in parsed.items():
            program = self.programs[pid]
            for pc, words in rows:
                if words[0] not in (556, 900):
                    continue
                filename = program[3]
                key = f'{filename}:{pid}:{pc}'
                kind = 'shop' if words[0] == 556 else 'reward'
                owner_labels = [f'{creatures.definitions[tid].name} · {creatures.zones.get(zone, zone)}'
                                for tid, zone in sorted(owners[pid]) if tid in creatures.definitions]
                item_ids = list(words[2:]) if kind == 'shop' else [words[1]]
                names = [creatures.definitions[i].name if i in creatures.definitions else str(i) for i in item_ids]
                self.nodes[key] = {'node': key, 'kind': kind, 'plot_id': pid, 'pc': pc,
                                   'file': filename, 'items': item_ids, 'count': len(item_ids),
                                   'label': f'{"商店" if kind == "shop" else "奖励"} #{pid} · ' +
                                            (' / '.join(owner_labels[:2]) or '、'.join(names[:3]) or '空商品表'),
                                   'owners': owner_labels, 'original_count': words[2] if kind == 'reward' else None}

    def public(self) -> dict:
        """Expose bounded selection data and distinguish unavailable scripts from an empty catalog."""
        return {'maps': [{'id': mid, 'name': name} for mid, name in sorted(self.creatures.zones.items())],
                'monsters': [{'id': e['id'], 'name': e['name'], 'maps': e['maps'],
                              'free_slots': sum(i in (-1, 0) for i in struct.unpack_from('<12h', self.creatures.records[e['id']], 254))}
                             | {'reset_maps': [zone for zone in e['maps'] if self.creatures.map_sources(e['id'], zone) == ['reset']]}
                             | {'summon_maps': [zone for zone in e['maps'] if self.creatures.map_sources(e['id'], zone) == ['summon']]}
                             for e in self.creatures.entries],
                'shops': [v for v in self.nodes.values() if v['kind'] == 'shop'],
                'rewards': [v for v in self.nodes.values() if v['kind'] == 'reward'],
                'script_error': self.script_error, 'coverage': dict(self.skipped),
                'probability_basis': 'user_gameplay_confirmation_2026-11-05',
                'probabilities': [{'threshold': t, 'percent': generation_chance(t)} for t in range(-1, 100)]}

    def validate(self, edits: list) -> None:
        """Check live map membership, script nodes and combined capacity before saving a draft."""
        validate_drop_edits(self.loaded, edits, self.creatures)
        planned = Counter()
        for edit in edits:
            value = edit.get('acquisition')
            if value is None:
                continue
            for kind, entries in (('shop', value['shops']), ('reward', value['rewards'])):
                for entry in entries:
                    node = self.nodes.get(entry['node'])
                    if node is None or node['kind'] != kind:
                        raise ContentError(self.script_error or '商店或奖励节点已变化，请重新选择。')
                    if edit['target_id'] in node['items']:
                        raise ContentError('所选节点已经包含该道具，不能重复添加。')
                    planned[entry['node']] += 1
                    if kind == 'shop' and node['count'] + planned[entry['node']] > 32:
                        raise ContentError('商店商品最多 32 项，请选择有空位的商店。')
        # Compile now as well: an export must not be the first time invalid relocation is detected.
        build_script_payloads(self.scripts, edits)

    def export_sources(self, edits: list) -> dict[str, bytes]:
        """Return only touched script baselines after detecting changes to their native version and contents."""
        self.validate(edits)
        touched = touched_scripts(edits)
        if touched:
            if sha256(read_bytes(self.directory / 'NewLocalServer.exe')) != NATIVE_SHA256:
                raise ContentError('服务端程序在编辑期间发生变化，请重新校验设置。')
            for filename in touched:
                if read_bytes(self.directory / 'ASM' / filename) != self.scripts[filename]:
                    raise ContentError('脚本在编辑期间发生变化，请重新打开编辑器。')
        return {filename: self.scripts[filename] for filename in sorted(touched)}


def touched_scripts(edits: list) -> set[str]:
    """Derive the exact script allowlist from normalized operations, never from supplied paths."""
    return {entry['node'].split(':')[0] for edit in edits for kind in ('shops', 'rewards')
            for entry in edit.get('acquisition', {}).get(kind, [])}


def has_acquisition(edits: list) -> bool:
    """Keep legacy editing independent of optional NPC and script data sources."""
    return any(any(edit.get('acquisition', {}).values()) for edit in edits)


def validate_drop_edits(loaded, edits: list, creatures=None) -> None:
    """Reject source collisions, duplicate old drops and candidate-capacity suppression."""
    additions = Counter()
    for edit in edits:
        drop = edit.get('acquisition', {}).get('drop')
        if drop is None:
            continue
        _, record = find_unique(loaded.server, drop['monster_id'])
        if struct.unpack_from('<H', record, 8)[0] != 0:
            raise ContentError('掉落来源必须是 NPC／怪物模板。')
        if creatures is not None and drop['map_id'] not in creatures.map_memberships.get(drop['monster_id'], set()):
            raise ContentError('所选怪物在该地图没有已解析的出现记录。')
        sources = [r for r in loaded.server.records if struct.unpack_from('<H', r, 8)[0] == 0
                   and edit['target_id'] in struct.unpack_from('<12h', r, 254)]
        if sources:
            raise ContentError('该道具已有模板掉落来源；本次仅支持为尚无来源的道具添加来源。')
        additions[drop['monster_id']] += 1
    for tid, count in additions.items():
        record = find_unique(loaded.server, tid)[1]
        ids, thresholds = struct.unpack_from('<12h', record, 254), struct.unpack_from('<12h', record, 278)
        free = sum(i in (-1, 0) for i in ids)
        active = sum(i not in (-1, 0) and t >= 0 for i, t in zip(ids, thresholds))
        if count > free or active + count > 8:
            raise ContentError(f'怪物 {tid} 的候选槽或有效生成容量不足，不能保证新增道具按设定概率掉落。')


def apply_drop_edits(loaded, server, edits: list) -> tuple[object, list[dict]]:
    """Add one native item candidate per operation to a server-table copy, preserving unrelated bytes."""
    validate_drop_edits(loaded, edits)
    changes = []
    for edit in edits:
        drop = edit.get('acquisition', {}).get('drop')
        if drop is None:
            continue
        index, before = find_unique(server, drop['monster_id'])
        row = bytearray(before)
        slot = next(i for i, value in enumerate(struct.unpack_from('<12h', row, 254)) if value in (-1, 0))
        chance = probability(drop['percent'])
        struct.pack_into('<h', row, 254 + slot * 2, edit['target_id'])
        struct.pack_into('<h', row, 278 + slot * 2, chance['threshold'])
        server = replace(server, records=server.records[:index] + (bytes(row),) + server.records[index+1:])
        changes.append({'item_id': edit['target_id'], 'monster_id': drop['monster_id'], 'map_id': drop['map_id'],
                        'slot': slot + 1, 'count': 1, **chance})
    return server, changes


def build_script_payloads(sources: dict[str, bytes], edits: list) -> dict[str, bytes]:
    """Copy changed functions to the file tail and relocate only proven intra-function branches."""
    plans = defaultdict(lambda: defaultdict(dict))
    for edit in edits:
        for kind in ('shops', 'rewards'):
            for entry in edit.get('acquisition', {}).get(kind, []):
                filename, pid, pc = entry['node'].split(':')
                plan = plans[filename][int(pid)].setdefault(int(pc), {'kind': kind, 'items': []})
                if plan['kind'] != kind:
                    raise ContentError('同一脚本位置的获取途径类型冲突。')
                plan['items'].append((edit['target_id'], entry.get('count', 1)))
    output = {}
    for filename, functions in plans.items():
        if not SCRIPT_NAME.fullmatch(filename) or filename not in sources:
            raise ContentError('缺少获取途径脚本基线。')
        original = sources[filename]
        programs = decode_script(original, filename)
        result = bytearray(original)
        for pid, changes in sorted(functions.items()):
            if pid not in programs:
                raise ContentError('奖励或商店脚本编号不存在。')
            program = programs[pid]
            rows = instructions(program)
            if not set(changes).issubset({pc for pc, _ in rows}):
                raise ContentError('获取途径位置不是指令边界。')
            result.extend(bytes((-len(result)) % 4))
            start = len(result) - 512
            cursor = start + 8
            mapping, chunks = {}, []
            for pc, words in rows:
                mapping[pc] = cursor
                words = list(words)
                change = changes.get(pc)
                extra = []
                if change:
                    ids = [i for i, _ in change['items']]
                    if len(ids) != len(set(ids)):
                        raise ContentError('同一获取节点含重复道具。')
                    if change['kind'] == 'shops':
                        if words[0] != 556 or set(ids) & set(words[2:]) or words[1] + len(ids) > 32:
                            raise ContentError('商店列表已满、类型不符或商品重复。')
                        words = [556, words[1] + len(ids), *words[2:], *ids]
                    else:
                        if words[0] != 900 or words[1] in ids or words[2] <= 0:
                            raise ContentError('只能向有效的原生物品奖励节点追加不同道具。')
                        # Native 900 writes mark=3 on success. Skip all extra rewards on failure;
                        # stop the function on an added-reward failure, before completion marks.
                        extra = [113, 0]
                        for item, count in change['items']:
                            extra.extend((900, item, count, 112, 0, 0))
                chunks.append((pc, words, extra))
                cursor += (len(words) + len(extra)) * 4
            mapping[program[2]] = cursor
            if cursor - start > 65532:
                raise ContentError('增加获取途径后脚本函数超过原生位置范围。')
            compiled = bytearray(struct.pack('<II', start, cursor - 4))
            for pc, words, extra in chunks:
                if words[0] in BRANCHES:
                    words[1] = mapping[words[1]]
                elif words[0] in (11, 12):
                    words[2:] = [mapping[target] for target in words[2:]]
                if extra:
                    extra[1] = mapping[pc + 12]
                    # Each success branch skips its local stop word, preserving mark=3.
                    for offset in range(2, len(extra), 6):
                        extra[offset+4] = mapping[pc] + (len(words) + offset + 6) * 4
                compiled.extend(struct.pack('<' + 'i' * (len(words) + len(extra)), *words, *extra))
            result.extend(compiled)
            first = struct.unpack_from('<I', original, 512)[0]
            struct.pack_into('<I', result, 520 + (pid - first) * 4, start)
        if len(result) > 4 * 1024 * 1024:
            raise ContentError('生成脚本超过 4 MiB 上限。')
        if result[:2] == b'MZ':
            struct.pack_into('<HH', result, 2, len(result) % 512, (len(result) + 511) // 512)
        checked = decode_script(bytes(result), filename)
        for pid in functions:
            instructions(checked[pid])
        output[filename] = bytes(result)
    return output
