"""Bounded, version-pinned map witnesses for NPC skills that summon at the caster's position."""

from collections import Counter, defaultdict, deque
import struct

from .binary import ContentError
from .plot_maps import NATIVE_SHA256

MAX_SUMMON_STATES = 100000
MAX_SUMMON_DEPTH = 64


def build_summon_map_index(records: dict, objects: dict, memberships: dict, *, native_sha256: str | None) -> dict:
    """Follow verified effect-18 skills from known NPC maps without editing records or inventing instances."""
    result = {'available': native_sha256 == NATIVE_SHA256, 'relations': [], 'diagnostics': {}}
    if not result['available']:
        return result
    if any(len(row) != 832 for row in objects.values()) or any(len(row) != 832 for row in records.values()):
        raise ContentError('技能召唤索引的模板宽度无效。')
    diagnostics, edges = Counter(), {}
    for caster, record in sorted(records.items()):
        slots = defaultdict(list)
        # SetNPCInitData copies these 16 i16 skill IDs into the NPC runtime skill array.
        for index, skill_id in enumerate(struct.unpack_from('<16h', record, 382), 1):
            if skill_id > 0:
                slots[skill_id].append(index)
        edges[caster] = []
        for skill_id, positions in slots.items():
            skill = objects.get(skill_id)
            if (skill is None or struct.unpack_from('<H', skill, 8)[0] != 3 or
                    not struct.unpack_from('<I', skill, 32)[0] & 0x20 or
                    struct.unpack_from('<i', skill, 528)[0] != 18):
                continue
            target = struct.unpack_from('<i', skill, 532)[0]
            if target not in records:
                diagnostics['invalid_targets'] += 1
                continue
            edges[caster].append({'caster_template_id': caster, 'skill_id': skill_id,
                                 'template_id': target, 'slots': positions})
    seeds = {(tid, zone) for tid, zones in memberships.items() if tid in records
             for zone in zones if type(zone) is int and zone >= 0}
    if len(seeds) > MAX_SUMMON_STATES:
        raise ContentError('技能召唤地图种子超过安全上限。')
    pending = deque(sorted(seeds, key=lambda state: (state[1], state[0])))
    paths = {state: ([state[0]], []) for state in seeds}
    witnesses = {}
    while pending:
        caster, zone = pending.popleft()
        path, steps = paths[caster, zone]
        for edge in edges[caster]:
            target = edge['template_id']
            if target in path:
                diagnostics['cycle_edges'] += 1
                continue
            if len(path) >= MAX_SUMMON_DEPTH:
                diagnostics['depth_limited_edges'] += 1
                continue
            state = target, zone
            next_path, next_steps = [*path, target], [*steps, edge]
            witnesses.setdefault(state, {**edge, 'map_id': zone, 'path': next_path,
                'steps': next_steps, 'root_template_id': path[0], 'status': 'potential_skill_summon'})
            if state in paths:
                continue
            if len(paths) >= MAX_SUMMON_STATES:
                raise ContentError('技能召唤地图遍历超过安全上限。')
            paths[state] = next_path, next_steps
            pending.append(state)
    result.update({'relations': [witnesses[state] for state in sorted(witnesses, key=lambda s: (s[1], s[0]))],
                   'diagnostics': dict(diagnostics), 'seed_states': len(seeds), 'visited_states': len(paths)})
    return result
