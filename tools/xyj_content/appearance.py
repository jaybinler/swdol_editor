"""Static equipped-character previews following native material slots and attachment matrices."""

from copy import deepcopy
from pathlib import Path
import struct

from .binary import ContentError
from .models import read_action, read_mesh, resolve_model, item_model
from .objects import find_unique
from .sprites import sprite_png
from .item_properties import template_properties

# Native actor pages and world Object defaults; each body's TSW label confirms the variant.
PROFILES = {
    'human_male': {'label': '人族男', 'race': '人族', 'sex': '男', 'actor': 1, 'default_body': 5169},
    'human_female': {'label': '人族女', 'race': '人族', 'sex': '女', 'actor': 3, 'default_body': 5059},
    'demon_male': {'label': '魔族男', 'race': '魔族', 'sex': '男', 'actor': 5, 'default_body': 5215},
    'demon_female': {'label': '魔族女', 'race': '魔族', 'sex': '女', 'actor': 7, 'default_body': 5206},
    'fairy_male': {'label': '仙族男', 'race': '仙族', 'sex': '男', 'actor': 9, 'default_body': 5200},
    'fairy_female': {'label': '仙族女', 'race': '仙族', 'sex': '女', 'actor': 11, 'default_body': 5192},
}

# Native world renderer: offhand actor +0x688 uses slot 2 (0x462aac/0x462b2c);
# mainhand actor +0x588 uses slot 1 (0x46304d..0x4631bc). Slot + 1 is the PLY marker.
HEAD_SLOT, MAINHAND_SLOT, OFFHAND_SLOT = 0, 1, 2


def profile_options() -> list[dict]:
    """Expose six verified variants and automatic selection by actual item restrictions."""
    return [{'key': 'auto', 'label': '自动匹配'}, *({'key': key, 'label': p['label']} for key, p in PROFILES.items())]


def _matching_profiles(record: bytes, compatible: list[str]) -> list[str]:
    conditions = {group['key']: group for group in template_properties(record)['requirements']}
    if any(conditions[key]['status'] == 'unknown' for key in ('race', 'sex')):
        raise ContentError('物品的种族或性别限制未解析，无法自动匹配；请手动选择基础人物。')
    races, sexes = conditions['race']['allowed'], conditions['sex']['allowed']
    if not races or not sexes:
        raise ContentError('物品没有允许的种族或性别，无法自动匹配；请手动选择基础人物。')
    return [key for key, definition in PROFILES.items()
            if key in compatible and definition['race'] in races and definition['sex'] in sexes]


def automatic_profile(record: bytes, compatible: list[str]) -> str:
    """Choose a stable compatible race/sex using item restrictions; never infer from texture labels."""
    candidates = _matching_profiles(record, compatible)
    if candidates:
        return candidates[0]
    raise ContentError('物品的种族、性别要求与可用人物外观不兼容，无法自动匹配。')


def _action(client: Path, record: bytes) -> dict:
    actor, mode = struct.unpack_from('<HH', record, 60)
    return read_action(client, actor, mode + 2)


def _body_texture(model: dict, action: dict) -> None:
    if action['kind'] != 1:
        raise ContentError("该身体装备不是已核对的人物贴图类型。")
    body = next((mesh for mesh in model['meshes'] if mesh['material_index'] == 4), None)
    if body is None:
        raise ContentError("人物模型缺少已核对的身体材质槽。")
    body['texture_url'] = f"/api/sprite/{action['frame']}/{action.get('pose', 0)}.png"


def _attach(model: dict, equipment: dict, slot: int, *, particle_anchors=None) -> None:
    anchor = model['anchors'].get(str(slot + 1))
    if anchor is None:
        raise ContentError("人物模型缺少对应的装备挂点。")
    matrix = list(anchor['matrix'])
    # 0x4d5c2f..0x4d5d53: attachment rotation axes are negated, translation retained.
    for r in range(3):
        for c in range(3):
            matrix[r*4+c] = -matrix[r*4+c]
    scale = equipment['action']['scale']
    def transform(point):
        xyz = [point[c] * scale[c] for c in range(3)]
        return [sum(matrix[r*4+c]*xyz[c] for c in range(3)) + matrix[r*4+3] for r in range(3)]

    for mesh in equipment['meshes']:
        positions = []
        for i in range(0, len(mesh['positions']), 3):
            positions.extend(transform(mesh['positions'][i:i+3]))
        model['meshes'].append({**mesh, 'positions': positions, 'part': 'equipment'})
    if particle_anchors:
        model['particle_anchors'] = [transform(point) for point in particle_anchors]


def equipped_model(client: Path, table, record: bytes, profile: str = 'auto') -> dict:
    """Apply the selected item to a real character; never substitute an unrelated mesh for missing data."""
    if profile != 'auto' and profile not in PROFILES:
        raise ContentError("人物类型无效。")
    if struct.unpack_from('<H', record, 8)[0] != 2:
        raise ContentError("普通物品没有人物穿戴外观，请查看图标或单件展示。")
    mask = struct.unpack_from('<I', record, 52)[0]
    if not mask & (1 | 2 | 4 | 8):
        raise ContentError("该部位没有已验证的独立穿戴动作；可查看单件展示与图标。")
    selected = _action(client, record)
    allowed = list(PROFILES)
    restriction_warning = ''
    if mask & 2 and selected['kind'] == 1:
        # SWDOL.exe 0x58b8d2..0x58b96a applies ACT FR/AP directly to the
        # character's body material. TSW directory labels are not compatibility rules.
        sprite_png(client, selected['frame'], selected.get('pose', 0))
        try:
            allowed = _matching_profiles(record, allowed)
        except ContentError:
            # Preserve manual inspection of legacy records with unknown/empty restrictions.
            # Auto still calls automatic_profile below and explicitly rejects those records.
            restriction_warning = '该装备的种族或性别限制未明确，手动预览不代表实际可装备。'
    profile = automatic_profile(record, allowed) if profile == 'auto' else profile
    if profile not in allowed:
        raise ContentError("这件身体装备的种族或性别要求与所选人物不兼容，请使用自动匹配。")
    definition = PROFILES[profile]
    action = read_action(client, definition['actor'], 0)
    model = deepcopy(read_mesh(resolve_model(client, action['frame'])))
    default_body = find_unique(table, definition['default_body'])[1]
    _body_texture(model, _action(client, default_body))
    warnings = ['静态人物穿戴预览；尚未播放动画，实际可装备条件与染色/特效需游戏内验收。']
    if mask & 2:
        _body_texture(model, selected)
        warnings.extend(selected['warnings'])
        if restriction_warning:
            warnings.append(restriction_warning)
    else:
        from .particles import weapon_anchors
        equipment = item_model(client, record)
        slot = HEAD_SLOT if mask & 1 else MAINHAND_SLOT if mask & 4 else OFFHAND_SLOT
        _attach(model, equipment, slot, particle_anchors=weapon_anchors(equipment) if slot != HEAD_SLOT else None)
        warnings.extend(equipment['warnings'])
    model.update(action=action, profile=profile, profile_label=definition['label'],
                 allowed_profiles=allowed, warnings=list(dict.fromkeys(warnings)), mode='equipped',
                 vertices=sum(len(m['positions'])//3 for m in model['meshes']),
                 triangles=sum(len(m['indices'])//3 for m in model['meshes']))
    return model
