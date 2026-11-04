"""Read-only template restrictions and properties verified against native item tooltips."""

import struct

from .binary import ContentError
from .objects import OBJECT_STRIDE
from .taxonomy import ITEM_TYPES

# 0x5822b0: allow-list bits, not exclusion bits. Bit 0 is the GM profession.
RACES = ((0x10000, '人族'), (0x20000, '魔族'), (0x40000, '仙族'))
JOBS = tuple((1 << i, label) for i, label in enumerate(('剑', '刀', '武', '刺', '药', '医', '阴', '符'), 1))
SEX_BITS = ((0x8000, '男'), (0x4000, '女'))
# 0x582aab..0x582b18 additionally checks Object +0x50 before the allow-list.
SEX_CODES = {1: ('男',), 2: ('女',), 3: ('男', '女')}
RESISTANCES = tuple((key, label, 186 + i) for i, (key, label) in enumerate((
    ('fire', '抗火'), ('water', '抗水'), ('wind', '抗风'),
    ('thunder', '抗雷'), ('light', '抗光'), ('dark', '抗暗'))))

# Read-only projection schema: (key, label, offset, signed format, display multiplier).
# Only the editor's separate field allowlist permits writes to recovery attributes.
# Movement / attack speed / guard: 0x5862cd..0x5864a8.
# Conversion / armor break: Object +0xfc/+0xfa enter character +0x276/+0x274
# at 0x587f0f..0x587f44 and 0x5c80f8..0x5c812d. The tooltip names those
# character fields at 0x585f3d..0x585fc8 (Language 131 conversion, 132 armor break).
# Recovery: 0x5864c2..0x5865fd, Language 297/298. Read each template field
# independently; do not reproduce the native MP-affix branch's HP-field read.
EXTRA_FIELDS = (
    ('conversion', '化元', 252, 'h', 1),
    ('guard', '御劲', 472, 'i', -1),
    ('armor_break', '崩甲', 250, 'h', 1),
    ('hp_recovery', '体力回复', 130, 'h', 1),
    ('mp_recovery', '灵力回复', 132, 'h', 1),
    ('movement', '移动力', 138, 'h', 1),
    ('attack_speed', '攻击速度', 476, 'i', 1),
)


def _condition(key, label, allowed, all_labels, applicable, *, unknown=None):
    if not applicable:
        status, text = 'not_applicable', '不适用'
    elif unknown is not None:
        status, text = 'unknown', f'未解析（原值 {unknown}）'
    elif not allowed:
        status, text = 'none', '无允许项'
    elif set(allowed) == set(all_labels):
        status, text = 'all', '不限'
    else:
        status, text = 'limited', ' / '.join(allowed)
    return {'key': key, 'label': label, 'status': status, 'text': text,
            'allowed': list(allowed) if applicable and unknown is None else []}


def _template_value(record, offset, fmt, sign):
    value = struct.unpack_from('<'+fmt, record, offset)[0] * sign
    # Native IMUL is signed 32-bit, including the minimum integer overflow case.
    return (value + 2**31) % 2**32 - 2**31 if fmt == 'i' else value


def template_properties(record: bytes) -> dict:
    """Project fixed template values, including zero; never infer equipment instance bonuses."""
    if len(record) != OBJECT_STRIDE:
        raise ContentError('道具属性记录宽度无效。')
    applicable = struct.unpack_from('<H', record, 8)[0] in ITEM_TYPES
    mask = struct.unpack_from('<I', record, 356)[0]
    sex = struct.unpack_from('<h', record, 80)[0]
    groups = []
    for key, label, options in (('race', '种族', RACES), ('profession', '职业', JOBS)):
        allowed = [name for bit, name in options if mask & bit]
        if key == 'profession' and not allowed and mask & 1:
            allowed = ['GM']
        groups.append(_condition(key, label, allowed,
                                 [name for _, name in options], applicable))
    allowed_sexes = [name for bit, name in SEX_BITS if mask & bit and name in SEX_CODES.get(sex, ())]
    groups.insert(1, _condition('sex', '性别', allowed_sexes, ('男', '女'), applicable,
                                unknown=sex if sex not in SEX_CODES else None))
    resistances = [{'key': key, 'label': label,
                    'value': struct.unpack_from('<b', record, offset)[0] if applicable else None}
                   for key, label, offset in RESISTANCES]
    extras = [{'key': key, 'label': label,
               'value': _template_value(record, offset, fmt, sign) if applicable else None,
               'unit': '%' if key == 'guard' else '',
               'source': 'template' if applicable else 'not_applicable'}
              for key, label, offset, fmt, sign in EXTRA_FIELDS]
    return {'scope': 'client_template', 'applicable': applicable,
            'requirements': groups, 'resistances': resistances,
            'hidden_attributes': extras,
            'raw_conditions': {'allow_mask': mask, 'sex_code': sex}}
