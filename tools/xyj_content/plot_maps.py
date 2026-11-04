"""Conservative, non-executing map relations for the verified server's compiled plot scripts."""

from collections import Counter, deque
from pathlib import Path
import hashlib
import re
import struct

from .binary import ContentError, read_bytes

NATIVE_SHA256 = '6b78293ec1bd221152ea38a143f992e38978ed62fd8caafc45e5e8510206e982'
PLOT_FIELDS = ('BKAutoPlotID', 'BKTouchPlotID', 'BKRangePlotID', 'BKDeadPlotID')
MAX_RELATIONS = 100000
MAX_SCRIPT_BYTES = 4 * 1024 * 1024
# Word widths verified from unconditional PC advancement in the pinned native handlers.
# Variable-size commands are deliberately absent; their operands must never be scanned as code.
_WIDTH_GROUPS = {
    1: (1,8,9,10,13,16,22,23,24,25,28,29,34,35,37,38,44,45,46,214,215,250,251,254,258,
        501,502,555,558,559,651,654,656,657,658,660,661,662,665,667,668,669,670,708,800,801,
        802,804,808,810,812,813,817,818,825,828,830,831,838,839,841,843,852,854,870,872,
        873,874,875,906,915,916,918),
    2: (4,5,6,7,14,15,30,31,32,33,40,41,42,43,101,102,103,104,105,108,109,110,111,112,
        113,114,115,131,132,133,138,141,142,143,148,155,201,202,203,206,
        207,208,209,210,211,212,213,252,253,255,256,257,262,263,264,265,266,267,269,270,
        271,272,273,274,275,278,279,301,400,401,412,413,500,504,505,552,560,803,807,809,
        815,816,819,827,829,832,833,842,844,845,848,849,850,851,855,856,860,865,868,907,
        908,910,914,919,920,921),
    3: (17,36,200,261,276,277,406,408,409,410,411,551,557,710,820,822,823,826,834,835,
        836,846,847,861,862,863,866,869,876,900,901,903,905,911,912,922),
    4: (47,48,259,268,824,859,877,923), 5: (858,867), 6: (700,711,712),
    7: (405,407,871), 9: (853,864), 16: (811,), 21: (821,), 24: (904,),
}
WIDTHS = {opcode: width for width, opcodes in _WIDTH_GROUPS.items() for opcode in opcodes}
# These can change actor/location/script context. Stop rather than inherit an unproved map.
CONTEXT_BARRIERS = frozenset((13,16,34,35,700,706,707,710,711,712))
CONDITIONAL_JUMPS = frozenset((108,109,110,111,112,113))


def decode_script(blob: bytes, filename: str) -> dict:
    """Validate observed 512-byte script wrappers and bounded function ranges without executing their contents."""
    if not re.fullmatch(r'TALK\d{4}\.EXE', filename, re.I) or not 520 <= len(blob) <= MAX_SCRIPT_BYTES:
        raise ContentError('脚本文件名称、标记或长度无效。')
    header = 512
    if blob[:2] == b'MZ':
        last, pages = struct.unpack_from('<HH', blob, 2)
        if (pages == 0 or last > 511 or (pages - 1) * 512 + (last or 512) != len(blob) or
                struct.unpack_from('<H', blob, 8)[0] * 16 != header):
            raise ContentError('脚本 MZ 文件头或声明长度无效。')
    elif blob[:48] != bytes(48):
        raise ContentError('未知脚本包装格式。')
    data = blob[header:]
    first, count = struct.unpack_from('<II', data)
    if count > 200 or first != int(filename[4:8]) * 200 or 8 + count * 4 > len(data):
        raise ContentError('脚本编号表越界。')
    plots, previous_end = {}, 8 + count * 4
    for index in range(count):
        start = struct.unpack_from('<I', data, 8 + index * 4)[0]
        if start == 0:
            continue
        if start % 4 or start < previous_end or start + 12 > len(data):
            raise ContentError('脚本函数入口重叠或越界。')
        own, end = struct.unpack_from('<II', data, start)
        if own != start or end % 4 or not start + 8 <= end <= len(data) - 4:
            raise ContentError('脚本函数边界无效。')
        plots[first + index] = (data, start + 8, end + 4, filename.upper(), header)
        previous_end = end + 4
    return plots


def trace_plot(program: tuple) -> tuple[list[dict], list[int], dict]:
    """Walk known instruction boundaries and potential branches; stop at unsupported semantics or context changes."""
    data, begin, end, filename, header = program
    pending, visited, claims = [begin], set(), {}
    spawns, calls, stopped = [], [], Counter()
    while pending:
        pc = pending.pop()
        if pc in visited:
            continue
        if pc == end:
            continue
        if pc % 4 or not begin <= pc < end or pc in claims and claims[pc] != pc:
            # An overlapping branch is not reliable evidence, even for already-seen creates.
            return [], [], {'ambiguous_boundary': 1}
        visited.add(pc)
        if len(visited) > 50000:
            raise ContentError('单个脚本超过静态解析步数上限。')
        opcode = struct.unpack_from('<i', data, pc)[0]
        if opcode == 0:
            continue
        if opcode in CONTEXT_BARRIERS:
            stopped['context_barrier'] += 1
            continue
        width = 2 if opcode == 100 else WIDTHS.get(opcode)
        if width is None:
            stopped['unsupported_opcode'] += 1
            continue
        finish = pc + width * 4
        if finish > end or any(address in claims and claims[address] != pc for address in range(pc, finish, 4)):
            return [], [], {'ambiguous_boundary': 1}
        claims.update((address, pc) for address in range(pc, finish, 4))
        words = struct.unpack_from('<' + 'i' * width, data, pc)
        if opcode == 100:
            pending.append(words[1])
            continue
        if opcode in CONDITIONAL_JUMPS:
            pending.append(words[1])
        if opcode in (821, 904):
            template = words[2] if opcode == 821 else words[1]
            hooks = words[3:7] if opcode == 821 else words[6:10]
            quantity = words[18] if opcode == 821 else words[21]
            if quantity > 0 and template > 0 and all(-1 <= plot <= 1000000 for plot in hooks):
                spawns.append({'template_id': template, 'hooks': hooks, 'opcode': opcode,
                               'file': filename, 'offset': header + pc})
        if opcode == 261 and 0 <= words[1] <= 3 and words[2] >= 0:
            calls.append(words[2])
        pending.append(finish)
    return spawns, calls, dict(stopped)


def build_script_index(instances: list[dict], records: tuple, directory: Path) -> tuple[dict, list[Path]]:
    """Read verified scripts and follow stored plot roots to deduplicated potential map relations; never run game code."""
    native = directory / 'NewLocalServer.exe'
    if hashlib.sha256(read_bytes(native)).hexdigest() != NATIVE_SHA256:
        raise ContentError('服务端脚本指令版本未核验，不能生成脚本地图索引。')
    paths = sorted(path for path in (directory / 'ASM').glob('TALK*.EXE')
                   if re.fullmatch(r'TALK\d{4}\.EXE', path.name, re.I))
    if not paths or len(paths) > 64:
        raise ContentError('区域目录缺少 ASM/TALK*.EXE 或脚本数量超过上限。')
    programs, files, total = {}, [], 0
    for path in paths:
        blob = read_bytes(path)
        total += len(blob)
        if total > 16 * 1024 * 1024:
            raise ContentError('脚本总大小超过 16 MiB。')
        decoded = decode_script(blob, path.name)
        if programs.keys() & decoded.keys():
            raise ContentError('多个脚本文件声明了相同的脚本编号。')
        programs.update(decoded)
        files.append({'file': path.name.upper(), 'sha256': hashlib.sha256(blob).hexdigest()})
    ids = Counter(struct.unpack_from('<I', row, 4)[0] for row in records)
    valid = {struct.unpack_from('<I', row, 4)[0] for row in records
             if struct.unpack_from('<H', row, 8)[0] == 0 and ids[struct.unpack_from('<I', row, 4)[0]] == 1}
    pending = deque((row['map_id'], plot, row['key']) for row in instances
                    for plot in row['plots'] if plot >= 0)
    visited, cache, relations, diagnostics = set(), {}, {}, Counter()
    while pending:
        zone, plot, root_key = pending.popleft()
        if (zone, plot) in visited:
            continue
        visited.add((zone, plot))
        if len(visited) > MAX_RELATIONS:
            raise ContentError('脚本地图遍历超过上限。')
        if plot not in programs:
            diagnostics['missing_plot'] += 1
            continue
        if plot not in cache:
            cache[plot] = trace_plot(programs[plot])
        spawns, calls, stops = cache[plot]
        diagnostics.update(stops)
        for spawn in spawns:
            if spawn['template_id'] not in valid:
                continue
            key = (zone, spawn['template_id'], plot, spawn['offset'])
            relations[key] = {k: v for k, v in spawn.items() if k != 'hooks'} | {
                'map_id': zone, 'plot_id': plot, 'root_key': root_key}
            if len(relations) > MAX_RELATIONS:
                raise ContentError('脚本地图关系超过上限。')
            for hook in spawn['hooks']:
                if hook >= 0:
                    pending.append((zone, hook, root_key))
        for called in calls:
            pending.append((zone, called, root_key))
    # Reject inputs changed during analysis; settings retain these same digests until activation.
    if hashlib.sha256(read_bytes(native)).hexdigest() != NATIVE_SHA256 or any(
            hashlib.sha256(read_bytes(path)).hexdigest() != entry['sha256'] for path, entry in zip(paths, files)):
        raise ContentError('解析期间服务端脚本发生变化，请重新生成快照。')
    return {'native_sha256': NATIVE_SHA256, 'files': files,
            'relations': [relations[key] for key in sorted(relations)],
            'diagnostics': dict(diagnostics), 'visited_contexts': len(visited)}, [native, *paths]


def validate_script_index(value: dict, roles: dict, templates: dict) -> list[dict]:
    """Validate portable snapshot evidence without requiring original executables on the viewing machine."""
    if not isinstance(value, dict) or set(value) != {'native_sha256','files','relations','diagnostics','visited_contexts'}:
        raise ContentError('脚本地图索引结构无效。')
    if value['native_sha256'] != NATIVE_SHA256 or not isinstance(value['files'], list) or not 1 <= len(value['files']) <= 64:
        raise ContentError('脚本地图版本或源文件集合无效。')
    files = set()
    for entry in value['files']:
        if (not isinstance(entry, dict) or set(entry) != {'file','sha256'} or
                not isinstance(entry['file'], str) or not re.fullmatch(r'TALK\d{4}\.EXE', entry['file']) or
                entry['file'] in files or not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])):
            raise ContentError('脚本来源清单无效。')
        files.add(entry['file'])
    if (type(value['visited_contexts']) is not int or not 0 <= value['visited_contexts'] <= MAX_RELATIONS or
            not isinstance(value['diagnostics'], dict) or set(value['diagnostics']) -
            {'missing_plot','context_barrier','unsupported_opcode','ambiguous_boundary'} or
            any(type(v) is not int or not 0 <= v <= 10000000 for v in value['diagnostics'].values())):
        raise ContentError('脚本解析统计无效。')
    rows = value['relations']
    if not isinstance(rows, list) or len(rows) > MAX_RELATIONS:
        raise ContentError('脚本地图关系数量无效。')
    keys = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'template_id','map_id','plot_id','opcode','file','offset','root_key'}:
            raise ContentError('脚本地图关系字段无效。')
        if any(type(row[key]) is not int for key in ('template_id','map_id','plot_id','opcode','offset')):
            raise ContentError('脚本地图关系数值无效。')
        if (row['template_id'] not in templates or row['opcode'] not in (821,904) or
                not 0 <= row['plot_id'] <= 1000000 or not 0 <= row['offset'] < MAX_SCRIPT_BYTES or
                not isinstance(row['file'], str) or row['file'] not in files or
                not isinstance(row['root_key'], str) or row['root_key'] not in roles or
                roles[row['root_key']]['map_id'] != row['map_id']):
            raise ContentError('脚本地图关系来源或目标无效。')
        key = (row['map_id'], row['template_id'], row['plot_id'], row['offset'])
        if key in keys:
            raise ContentError('脚本地图关系重复。')
        keys.add(key)
    return rows
