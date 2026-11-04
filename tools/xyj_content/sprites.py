"""Resolve ACT icon references and bounded multi-frame TSW resources."""

from functools import lru_cache
from pathlib import Path
import struct

from .binary import ContentError, decompress, read_bytes
from .models import read_action, image_png


@lru_cache(maxsize=16)
def sprite_index(client: Path, category: int) -> bytes:
    """Cache a validated fixed-size TSW directory for one resource namespace."""
    names = ('char', 'item', 'magic', 'sys', 'map1', 'map2')
    if not 0 <= category < len(names):
        raise ContentError("不支持的图像命名空间。")
    blob = read_bytes(client / 'Tsw' / f'all_{names[category]}.tsw')
    if len(blob) != 28 + 3000 * 44:
        raise ContentError("TSW 汇总索引格式不受支持。")
    return blob


def sprite_path(client: Path, frame: int) -> Path:
    """Use the directory's physical file ID instead of assuming item IDs are sprite IDs."""
    category, index = divmod(frame, 3000)
    if not index:
        raise ContentError("图像编号不能为零。")
    blob = sprite_index(client, category)
    physical = struct.unpack_from('<I', blob, 28 + (index - 1) * 44 + 28)[0]
    if not 1 <= physical <= 99999:
        raise ContentError("图像索引为空。")
    prefix = ('char', 'item', 'magic', 'sys', 'map1', 'map2')[category]
    return client / 'Tsw' / f'{prefix}{physical:05d}.tsw'


@lru_cache(maxsize=256)
def sprite_png(client: Path, frame: int, pose: int = 0) -> bytes:
    """Decode exactly one offset-addressed TSW frame; preserve transparent scanline runs."""
    blob = read_bytes(sprite_path(client, frame))
    if len(blob) < 12:
        raise ContentError("TSW 头不完整。")
    _, magic, count, header_a, header_b = struct.unpack_from('<I4H', blob)
    if magic != 0xabcd or (header_a, header_b) != (16, 12) or not 0 < count <= 4096:
        raise ContentError("TSW 头格式不受支持。")
    if type(pose) is not int or not 0 <= pose < count or 12 + 36 * count > len(blob):
        raise ContentError("TSW 图像帧越界。")
    offset, packed, raw_size = struct.unpack_from('<3I', blob, 12 + pose * 36)
    if offset < 12 + count * 36 or not packed or offset + packed > len(blob):
        raise ContentError("TSW 帧数据越界。")
    return image_png(decompress(blob[offset:offset + packed], raw_size))


def icon_reference(client: Path, record: bytes) -> dict:
    """Resolve the game's inventory icon action (mode plus one)."""
    actor, mode = struct.unpack_from('<HH', record, 60)
    action = read_action(client, actor, mode + 1)
    if action['kind'] != 2:
        raise ContentError("该记录没有可识别的物品图标动作。")
    return {'frame': action['frame'], 'pose': action.get('pose', 0)}
