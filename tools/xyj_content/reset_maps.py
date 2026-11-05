"""Version-pinned, read-only map witnesses for native NPC reset candidate pools."""

from collections import Counter, defaultdict, deque
import struct

from .binary import ContentError
from .plot_maps import NATIVE_SHA256

RESET_OFFSET = 516
RESET_SLOTS = 5
MAX_RESET_STATES = 100000
MAX_RESET_DEPTH = 64


def reset_candidates(template_id: int, records: dict) -> list[dict]:
    """Resolve five ordered native slots; -1 keeps the current template and invalid IDs stay labelled."""
    if template_id not in records:
        raise ContentError('刷新候选的源模板不存在。')
    row = records[template_id]
    if len(row) != 832:
        raise ContentError('刷新候选的模板宽度无效。')
    return [{'slot': index + 1, 'raw_id': value, 'id': template_id if value == -1 else value,
             'is_self': value in (-1, template_id),
             'valid': (template_id if value == -1 else value) > 0 and
                      (template_id if value == -1 else value) in records}
            for index, value in enumerate(struct.unpack_from('<5h', row, RESET_OFFSET))]


def build_reset_map_index(records: dict, roles: dict, script_relations: dict, *, native_sha256: str | None) -> dict:
    """Follow bounded reset paths from evidenced seeds, keeping direct instance ownership unchanged."""
    result = {'available': native_sha256 == NATIVE_SHA256, 'relations': [], 'diagnostics': {}}
    if not result['available']:
        return result
    diagnostics = Counter()
    edges = {}
    for tid in records:
        grouped = defaultdict(list)
        for candidate in reset_candidates(tid, records):
            if not candidate['valid']:
                diagnostics['invalid_slots'] += 1
            elif not candidate['is_self']:
                grouped[candidate['id']].append(candidate['slot'])
        edges[tid] = dict(grouped)
    seeds = {}
    for key, role in sorted(roles.items()):
        tid, zone = role['template_id'], role['map_id']
        if type(zone) is not int or zone < 0:
            diagnostics['invalid_map_instances'] += 1
            continue
        revive = role.get('revive_map_id')
        if type(revive) is not int or revive != zone:
            diagnostics['cross_map_or_unknown_revive_instances'] += 1
            continue
        if tid in records:
            seeds.setdefault((tid, zone), {'kind': 'saved', 'root_key': key, 'template_id': tid,
                                           'map_id': zone, 'revive_map_id': revive})
    # A verified create instruction establishes the newborn object's initial map. Such
    # descendants remain potential reset relations, not assertions about live monsters.
    for tid, rows in sorted(script_relations.items()):
        for row in sorted(rows, key=lambda r: (r['map_id'], r['plot_id'], r['offset'])):
            if tid in records:
                seeds.setdefault((tid, row['map_id']), {'kind': 'script', 'root_key': row['root_key'],
                    'template_id': tid, 'map_id': row['map_id'], 'plot_id': row['plot_id'],
                    'file': row['file'], 'offset': row['offset']})
    if len(seeds) > MAX_RESET_STATES:
        raise ContentError('刷新候选地图种子超过安全上限。')
    paths = {state: (state[0],) for state in seeds}
    roots = dict(seeds)
    pending = deque(sorted(seeds, key=lambda state: (state[1], state[0])))
    witnesses = {}
    while pending:
        tid, zone = pending.popleft()
        path, root = paths[tid, zone], roots[tid, zone]
        for child, slots in edges[tid].items():
            if child in path:
                diagnostics['cycle_edges'] += 1
                continue
            if len(path) >= MAX_RESET_DEPTH:
                diagnostics['depth_limited_edges'] += 1
                continue
            state = (child, zone)
            witnesses.setdefault(state, {'template_id': child, 'map_id': zone, 'parent_template_id': tid,
                'slots': slots, 'path': [*path, child], 'root': dict(root), 'status': 'potential_reset'})
            if state in paths:
                continue
            if len(paths) >= MAX_RESET_STATES:
                raise ContentError('刷新候选地图遍历超过安全上限。')
            paths[state] = (*path, child)
            roots[state] = root
            pending.append(state)
    result['relations'] = [witnesses[state] for state in sorted(witnesses, key=lambda s: (s[1], s[0]))]
    result['diagnostics'] = dict(diagnostics)
    result['visited_states'] = len(paths)
    result['seed_states'] = len(seeds)
    return result
