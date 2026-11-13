"""Read the native effect directory and resolve its particle actor resources."""

import math
import struct

from .binary import ContentError, read_bytes
from .models import read_action, read_mesh, resolve_model, texture_png
from .objects import find_unique

EFFECT_COUNT = 5000
NAME_START = 24
DATA_START = NAME_START + EFFECT_COUNT * 40
EFFECT_SIZE = 248


def effect_table(client) -> bytes:
    """Accept the fixed layout consumed by the researched client Effect.dat loader."""
    data = read_bytes(client / 'Act' / 'Effect.dat')
    if len(data) != DATA_START + EFFECT_COUNT * EFFECT_SIZE:
        raise ContentError('Effect.dat 的资源表长度不匹配。')
    return data


def effect_entry(data: bytes, effect_id: int) -> tuple[str, bytes]:
    """Resolve a nonempty, one-based particle preset and its original record."""
    if type(effect_id) is not int or not 1 <= effect_id <= EFFECT_COUNT:
        raise ContentError('武器粒子效果编号无效。')
    index = effect_id - 1
    name_start = NAME_START + index * 40
    name = data[name_start:name_start + 32].split(b'\0', 1)[0].decode('big5', 'replace').strip()
    start = DATA_START + index * EFFECT_SIZE
    row = data[start:start + EFFECT_SIZE]
    if not name or len(row) != EFFECT_SIZE or not all(struct.unpack_from('<HH', row, 194)):
        raise ContentError(f'粒子效果 #{effect_id} 缺少名称或有效的发射定义。')
    return name, row


def effect_choices(client) -> list[dict]:
    """List selectable native presets without loading hundreds of preview meshes."""
    data = effect_table(client)
    choices = []
    for i in range(1, EFFECT_COUNT + 1):
        try:
            name, _ = effect_entry(data, i)
        except ContentError:
            continue
        choices.append({'id': i, 'name': name})
    return choices


def particle_preview(client, effect_id: int) -> dict:
    """Return verified effect mesh, textures and emitter parameters for browser playback."""
    name, row = effect_entry(effect_table(client), effect_id)
    count, actor, selector = struct.unpack_from('<3H', row, 194)
    action = read_action(client, actor, selector)
    if action['kind'] != 0:
        raise ContentError('该粒子使用尚未支持的显示类型。')
    model = read_mesh(resolve_model(client, action['frame']))
    if model['vertices'] > 4096 or model['triangles'] > 4096 or len(model['meshes']) > 8:
        raise ContentError('粒子网格超过实时预览上限。')
    colors = [list(struct.unpack_from('<3f', row, offset)) for offset in (0, 16)]
    rate = struct.unpack_from('<f', row, 136)[0]
    velocity = list(struct.unpack_from('<3f', row, 64))
    if (not all(math.isfinite(v) and 0 <= v <= 255 for color in colors for v in color)
            or not math.isfinite(rate) or not 0 <= rate <= 10000
            or not all(math.isfinite(v) and abs(v) <= 10000 for v in velocity)):
        raise ContentError('粒子色彩或发射率超出可预览范围。')
    for mesh in model['meshes']:
        if mesh['texture'] is not None:
            texture_png(client, mesh['texture'])
    return {'id': effect_id, 'name': name, 'actor': actor, 'selector': selector,
            'file': model['file'], 'count': min(count, 96), 'rate': rate, 'colors': colors,
            'scale': action['scale'], 'velocity': velocity, 'meshes': model['meshes']}


def validate_effect_references(loaded, edits, client) -> None:
    """Require a real target preset when changing a particle association."""
    data = None
    for edit in edits:
        value = edit['stats'].get('weapon_particle')
        if value is None or value <= 0:
            continue
        row = find_unique(loaded.client, edit['source_id'])[1]
        original = struct.unpack_from('<i', row, 416)[0]
        if value == original:
            continue
        if data is None:
            data = effect_table(client)
        effect_entry(data, value)


def weapon_anchors(model: dict) -> list[list[float]] | None:
    """Extract the native first vertices of the two weapon effect anchor groups."""
    anchors = model.get('anchors', {})
    points = [anchors.get(str(group), {}).get('points', []) for group in (21, 22)]
    return [p[0] for p in points] if all(points) else None
