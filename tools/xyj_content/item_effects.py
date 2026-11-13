"""Read item skill and effect references from the verified Object layout."""

from collections import Counter
import struct

from .objects import view_record

EFFECT_FIELDS = {
    'active_skill': ('主动技能', 374, 'h'),
    'passive_skill': ('被动技能', 376, 'h'),
    'passive_chance': ('释放几率阈值', 380, 'h'),
    'weapon_particle': ('武器粒子效果', 416, 'i'),
    'particle_speed': ('粒子流速', 424, 'i'),
}


def skill_choices(loaded) -> list[dict]:
    """List unique type-3 definitions that exist on both sides of the patch."""
    def skill_ids(table):
        rows = [struct.unpack_from('<IH', row, 4) for row in table.records]
        counts = Counter(tid for tid, _ in rows)
        return {tid for tid, kind in rows if kind == 3 and 1 <= tid <= 32767 and counts[tid] == 1}

    ids = skill_ids(loaded.server) & skill_ids(loaded.client)
    choices = []
    for i, row in enumerate(loaded.client.records):
        tid = struct.unpack_from('<I', row, 4)[0]
        if tid in ids:
            choices.append({'id': tid, 'name': view_record(row, i, loaded.language).name})
    return sorted(choices, key=lambda row: row['id'])
