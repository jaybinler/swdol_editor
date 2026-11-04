"""Read-only NPC directory, map membership and evidence-labelled inventory rules."""

from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import struct
import re

from .binary import ContentError, read_bytes
from .objects import view_record
from .models import read_action, read_mesh, resolve_model, item_model
from .appearance import _body_texture, _attach, HEAD_SLOT, MAINHAND_SLOT, OFFHAND_SLOT
from .plot_maps import validate_script_index

PAGE_SIZE = 60
SNAPSHOT = Path('output/editor/npc-snapshot')


def generation_chance(threshold: int) -> float:
    """Return the single-check probability under uniform native 15-bit random outputs, before capacity limits."""
    accepted = max(0, min(100, threshold + 1))
    return (327 * accepted + min(accepted, 68)) * 100 / 32768


def inventory_rules(record: bytes, definitions: dict) -> list[dict]:
    """Read twelve ordered creation candidates; never present creation chance as kill probability."""
    result = []
    for slot, (object_id, threshold) in enumerate(zip(struct.unpack_from('<12h', record, 254),
                                                    struct.unpack_from('<12h', record, 278)), 1):
        if object_id in (-1, 0):
            continue
        definition = definitions.get(object_id)
        result.append({'slot': slot, 'id': object_id,
                       'name': definition.name if definition else '未找到物品定义',
                       'known': definition is not None, 'type': definition.type_code if definition else None,
                       'threshold': threshold, 'check_percent': generation_chance(threshold),
                       'drop_percent': None, 'status': 'creation_check_only',
                       'capacity_limited': len(result) >= 8})
    return result


def saved_inventory(instance: dict, definitions: dict) -> dict:
    """Describe stored per-instance slots without turning absent data into empty or inventing probabilities."""
    if 'hand_items' not in instance:
        return {'status': 'not_collected', 'items': []}
    items = []
    for slot, (object_id, count) in enumerate(zip(instance['hand_items'], instance['hand_counts']), 1):
        if object_id in (-1, 0):
            continue
        definition = definitions.get(object_id)
        items.append({'slot': slot, 'id': object_id, 'count': count, 'known': definition is not None,
                      'name': definition.name if definition else '未找到物品定义'})
    return {'status': 'stored', 'items': items}


class CreatureCatalog:
    """Load a validated local projection without connecting to a database or writing editor state."""

    def __init__(self, root: Path, loaded, *, client=None, directory: Path | None = None):
        self.loaded = loaded
        self.client = client if client is not None else root / 'swdol'
        directory = directory if directory is not None else root / SNAPSHOT
        try:
            manifest = json.loads(read_bytes(directory / 'manifest.json'))
            digest = manifest.get('catalog_sha256', '')
            if not re.fullmatch('[0-9a-f]{64}', digest) or manifest.get('catalog_file') != f'catalog-{digest}.json':
                raise ContentError('NPC 快照文件索引无效。')
            raw = read_bytes(directory / manifest['catalog_file'])
            if manifest.get('format') not in ('xyj-npc-snapshot/v1', 'xyj-npc-snapshot/v2', 'xyj-npc-snapshot/v3') or hashlib.sha256(raw).hexdigest() != manifest['catalog_sha256']:
                raise ContentError('NPC 快照校验失败，请重新生成经过核验的快照。')
            if manifest['server_object_sha256'] != hashlib.sha256(loaded.blobs[0]).hexdigest():
                raise ContentError('NPC 快照与服务端本地基线不一致，请更新快照。')
            data = json.loads(raw)
            if len(data['instances']) > 100000 or len(data['zones']) > 1000:
                raise ContentError('NPC 快照超过读取上限。')
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, ContentError):
                raise
            raise ContentError('NPC 快照结构无效。') from exc
        self.source = manifest['source']
        ids = Counter(struct.unpack_from('<I', r, 4)[0] for r in loaded.server.records)
        self.records = {struct.unpack_from('<I', r, 4)[0]: r for r in loaded.server.records
                        if struct.unpack_from('<H', r, 8)[0] == 0 and struct.unpack_from('<I', r, 4)[0]
                        and ids[struct.unpack_from('<I', r, 4)[0]] == 1}
        self.objects = {struct.unpack_from('<I', r, 4)[0]: r for r in loaded.server.records
                        if struct.unpack_from('<I', r, 4)[0] and ids[struct.unpack_from('<I', r, 4)[0]] == 1}
        self.definitions = {struct.unpack_from('<I', r, 4)[0]: view_record(r, i)
                            for i, r in enumerate(loaded.server.records) if struct.unpack_from('<I', r, 4)[0] and ids[struct.unpack_from('<I', r, 4)[0]] == 1}
        self.instances = defaultdict(list)
        self.roles = {}
        self.zones = {int(z['id']): z['name'] for z in data['zones']}
        if len(self.zones) != len(data['zones']):
            raise ContentError('NPC 快照包含重复地图编号。')
        self.map_counts = Counter()
        try:
            for instance in data['instances']:
                expected = {'key', 'id', 'template_id', 'map_id', 'name', 'nickname', 'equipment',
                            'revive_map_id', 'revive_position', 'hair', 'face'}
                if manifest['format'] in ('xyj-npc-snapshot/v2', 'xyj-npc-snapshot/v3'):
                    expected.update(('hand_items', 'hand_counts'))
                if manifest['format'] == 'xyj-npc-snapshot/v3':
                    expected.add('plots')
                    plots = instance.get('plots')
                    if not isinstance(plots, list) or len(plots) != 4 or any(type(v) is not int or not -2147483648 <= v <= 2147483647 for v in plots):
                        raise ContentError('NPC 脚本入口无效。')
                if set(instance) != expected:
                    raise ContentError('NPC 实例包含未批准字段或缺少字段。')
                if 'hand_items' in instance:
                    for key, lower, upper in (('hand_items', -32768, 32767), ('hand_counts', 0, 2147483647)):
                        values = instance[key]
                        if not isinstance(values, list) or len(values) != 9 or any(type(v) is not int or not lower <= v <= upper for v in values):
                            raise ContentError('NPC 存档携带槽或数量无效。')
                tid, role, zone = instance['template_id'], instance['id'], instance['map_id']
                key = instance['key']
                if (any(type(v) is not int for v in (tid, role, zone)) or key in self.roles
                        or not re.fullmatch(r'npcpool(?:_add_on)?:\d+', key) or key.rsplit(':', 1)[1] != str(role)):
                    raise ContentError('NPC 实例编号无效或重复。')
                if tid not in self.records or len(instance['equipment']) != 10:
                    raise ContentError('NPC 实例模板或装备结构无效。')
                if (any(type(v) is not int or not -32768 <= v <= 32767 for v in instance['equipment'])
                        or not isinstance(instance['name'], str) or len(instance['name']) > 200
                        or not isinstance(instance['nickname'], str) or len(instance['nickname']) > 200):
                    raise ContentError('NPC 实例名称或装备数值无效。')
                self.roles[key] = instance
                self.instances[tid].append(instance)
                self.map_counts[zone] += 1
                self.zones.setdefault(zone, f'地图 {zone}（名称未确认）')
        except (KeyError, TypeError) as exc:
            raise ContentError('NPC 实例快照结构无效。') from exc
        self.map_memberships = {tid: {r['map_id'] for r in rows} for tid, rows in self.instances.items()}
        self.script_relations = defaultdict(list)
        self.script_coverage = {'available': False, 'relations': 0}
        if manifest['format'] == 'xyj-npc-snapshot/v3':
            rows = validate_script_index(data.get('script_index'), self.roles, self.records)
            for row in rows:
                self.map_memberships.setdefault(row['template_id'], set()).add(row['map_id'])
                self.script_relations[row['template_id']].append(row)
            self.script_coverage = {'available': True, 'relations': len(rows),
                                    'diagnostics': data['script_index']['diagnostics']}
        self.entries = []
        for tid, record in self.records.items():
            instances = self.instances[tid]
            name = self.definitions[tid].name
            maps = sorted(self.map_memberships.get(tid, ()))
            aliases = sorted({r['name'] for r in instances if r['name'] != name})
            self.entries.append({'id': tid, 'name': name, 'maps': maps, 'instance_count': len(instances),
                                 'search': ' '.join([name, str(tid), *aliases, *(str(r['id']) for r in instances)]).lower()})
        self.entries.sort(key=lambda e: e['id'])
        self.entries_by_id = {entry['id']: entry for entry in self.entries}
        self.item_sources = defaultdict(list)
        for entry in self.entries:
            item_ids = {item['id'] for item in inventory_rules(self.records[entry['id']], self.definitions)
                        if item['id'] > 0}
            for item_id in item_ids:
                self.item_sources[item_id].append(entry['id'])

    def drop_sources(self, object_id: int, page: int = 0) -> dict:
        """Read deduplicated template-configured sources; new IDs return empty without inheriting a template."""
        if type(object_id) is not int or not 1 <= object_id <= 32767:
            raise ContentError('道具 ID 必须是 1 至 32767 的整数。')
        if type(page) is not int or not 0 <= page <= 100000:
            raise ContentError('掉落怪物页码无效。')
        ids = self.item_sources.get(object_id, ())
        pages = max(1, (len(ids) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages - 1)
        return {'item_id': object_id, 'total': len(ids), 'page': page, 'pages': pages,
                'scope': 'npc_template_inventory',
                'items': [{'id': tid, 'name': self.entries_by_id[tid]['name'],
                           'maps': [{'id': zone, 'name': self.zones[zone]}
                                    for zone in self.entries_by_id[tid]['maps']]}
                          for tid in ids[page*PAGE_SIZE:(page+1)*PAGE_SIZE]]}

    def metadata(self) -> dict:
        """Expose map counts and provenance without leaking snapshot paths or raw database columns."""
        return {'source': self.source, 'templates': len(self.records), 'instances': len(self.roles),
                'script_map_coverage': self.script_coverage,
                'unplaced': sum(not entry['maps'] for entry in self.entries),
                'maps': [{'id': key, 'name': name, 'instances': self.map_counts[key],
                          'templates': sum(key in e['maps'] for e in self.entries)}
                         for key, name in sorted(self.zones.items())]}

    def _map(self, value: str) -> int | str:
        if value in ('all', 'unplaced'):
            return value
        try:
            number = int(value)
        except (ValueError, TypeError) as exc:
            raise ContentError('地图编号无效。') from exc
        if number not in self.zones:
            raise ContentError('地图编号不存在。')
        return number

    def listing(self, query: str = '', map_id: str = 'all', page: int = 0, template_id: int | None = None) -> dict:
        """Search all aliases and map memberships, returning at most sixty templates per page."""
        if len(query) > 128 or not 0 <= page <= 100000:
            raise ContentError('搜索条件或页码超出范围。')
        if template_id is not None and (type(template_id) is not int or template_id not in self.records):
            raise ContentError('NPC／怪物模板不存在。')
        selected = self._map(map_id)
        query = query.strip().lower()
        entries = [e for e in self.entries if (template_id is None or e['id'] == template_id) and
                   (not query or query in e['search']) and
                   (selected == 'all' or selected == 'unplaced' and not e['maps'] or selected in e['maps'])]
        pages = max(1, (len(entries) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages - 1)
        return {'total': len(entries), 'page': page, 'pages': pages,
                'items': [{**{k: v for k, v in e.items() if k != 'search'},
                           'matched_instances': sum(r['map_id'] == selected for r in self.instances[e['id']])
                           if type(selected) is int else e['instance_count']}
                          for e in entries[page*PAGE_SIZE:(page+1)*PAGE_SIZE]]}

    def detail(self, template_id: int, map_id: str = 'all', page: int = 0) -> dict:
        """Return template inventory rules and paginated, independently named saved occurrences."""
        if template_id not in self.records:
            raise ContentError('NPC／怪物模板不存在。')
        if not 0 <= page <= 100000:
            raise ContentError('实例页码无效。')
        selected = self._map(map_id)
        instances = [r for r in self.instances[template_id] if selected in ('all', 'unplaced') or r['map_id'] == selected]
        pages = max(1, (len(instances) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages - 1)
        record = self.records[template_id]
        view = self.definitions[template_id]
        return {'id': template_id, 'name': view.name, 'description': view.description,
                'maps': [{'id': key, 'name': self.zones[key],
                          'instances': sum(r['map_id'] == key for r in self.instances[template_id]),
                          'sources': (['saved'] if any(r['map_id'] == key for r in self.instances[template_id]) else []) +
                                     (['script'] if any(r['map_id'] == key for r in self.script_relations[template_id]) else []),
                          'script_sources': [r for r in self.script_relations[template_id] if r['map_id'] == key]}
                         for key in sorted(self.map_memberships.get(template_id, ()))],
                'instances': [{**{k:v for k,v in r.items() if k not in ('hand_items', 'hand_counts')},
                               'saved_inventory': saved_inventory(r, self.definitions)}
                              for r in instances[page*PAGE_SIZE:(page+1)*PAGE_SIZE]], 'instance_total': len(instances),
                'instance_page': page, 'instance_pages': pages,
                'inventory': inventory_rules(record, self.definitions),
                'drop_status': 'unconfirmed',
                'drop_note': '以下来自 Object 模板，同一模板的实例共用这些候选与阈值；不是所选实例的实际携带或已确认死亡掉落。',
                'probability_note': '服务端逐槽取 r = rand() % 100，r 不大于原始阈值时尝试生成。rand 输出为 0 至 32767；令 t 为阈值加 1 后限制在 0 至 100，单次判定百分比 = (327 × t + min(t, 68)) ÷ 32768 × 100。尚未计入最多 8 次成功候选、容量、合并、脚本及死亡获取条件，因此不等于最终击杀掉率；最终掉率尚未计算，显示“未确认”。'}

    def model(self, template_id: int, role_id: str | None = None) -> dict:
        """Build a fresh first-pose character with real saved equipment; retain partial-load warnings."""
        if template_id not in self.records:
            raise ContentError('NPC／怪物模板不存在。')
        record = self.records[template_id]
        instance = None
        if role_id is not None:
            instance = self.roles.get(role_id)
            if instance is None or instance['template_id'] != template_id:
                raise ContentError('实例不属于所选 NPC／怪物。')
        actor, mode = struct.unpack_from('<HH', record, 60)
        action = read_action(self.client, actor, mode)
        if action['kind'] != 0:
            raise ContentError('该角色外观不是已支持的独立三维模型。')
        model = deepcopy(read_mesh(resolve_model(self.client, action['frame'])))
        warnings = ['静态首帧预览；动画、染色、动态特效及实例缩放尚未完整还原。', *action['warnings']]
        # Native template initializer: head pool +0xda, body +0xe4, main pool +0xf0, off pool +0xe6.
        if instance:
            equipment = instance['equipment'][:4]
        else:
            choose = lambda offset: next((v for v in struct.unpack_from('<5h', record, offset) if v > 0), -1)
            equipment = [choose(218), struct.unpack_from('<h', record, 228)[0], choose(240), choose(230)]
            warnings.append('模板外观使用首个可用头部与手持变体。')
        for index, object_id in enumerate(equipment):
            if object_id <= 0:
                continue
            definition = self.definitions.get(object_id)
            label = ('头部', '身体', '主手', '副手')[index]
            try:
                # Use the server's verified Object association with the client resource files.
                item = self.objects.get(object_id)
                if item is None:
                    raise ContentError('未找到装备定义')
                if index == 1:
                    if not any(m['material_index'] == 4 for m in model['meshes']):
                        continue  # Independent creature meshes already have their native textures.
                    a, m = struct.unpack_from('<HH', item, 60)
                    _body_texture(model, read_action(self.client, a, m + 2))
                else:
                    part = item_model(self.client, item)
                    _attach(model, part, (HEAD_SLOT, None, MAINHAND_SLOT, OFFHAND_SLOT)[index])
                    warnings.extend(part['warnings'])
            except ContentError as exc:
                warnings.append(f'{label} {object_id}（{definition.name if definition else "未知"}）未还原：{exc}')
        model.update(action=action, warnings=list(dict.fromkeys(warnings)), mode='creature',
                     mode_label='存档实例装扮' if instance else '模板基础外观',
                     vertices=sum(len(m['positions'])//3 for m in model['meshes']),
                     triangles=sum(len(m['indices'])//3 for m in model['meshes']))
        return model
