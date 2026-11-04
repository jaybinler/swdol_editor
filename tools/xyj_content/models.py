"""Read the game's ACT / IDX / PLY / TEX assets without modifying game files."""

from functools import lru_cache
from pathlib import Path
import math
import struct
import zlib

from .binary import ContentError, decompress, read_bytes


def unpack_asset(path: Path) -> bytes:
    """Decode the eight-byte length envelope used by PLY and TEX files."""
    blob = read_bytes(path)
    if len(blob) < 8:
        raise ContentError("资源头不完整。")
    raw_size, packed_size = struct.unpack_from("<II", blob)
    if packed_size != len(blob) - 8:
        raise ContentError("资源压缩长度不一致。")
    if not raw_size:
        raise ContentError("这是空资源占位文件。")
    return decompress(blob[8:], raw_size)


@lru_cache(maxsize=32)
def actor_data(client: Path, actor: int) -> bytes:
    """Read one immutable actor page, retaining bounded decoded pages in memory."""
    category, index = divmod(actor, 3000)
    names = ("all_char", "all_item", "all_magic", "all_sys", "all_map1", "all_map2")
    if category >= len(names) or not 1 <= index <= 3000:
        raise ContentError("该外观脚本编号不受支持。")
    blob = read_bytes(client / "Act" / (names[category] + ".act"))
    pos = 10 + (index - 1) * 12
    if pos + 12 > len(blob):
        raise ContentError("脚本索引越界。")
    size, offset, tag, packed = struct.unpack_from("<IIHH", blob, pos)
    if tag != actor or not size or offset + (packed or size) > len(blob):
        raise ContentError("脚本索引无效或为空。")
    raw = decompress(blob[offset:offset + packed], size) if packed else blob[offset:offset + size]
    return raw


def read_action(client: Path, actor: int, selector: int) -> dict:
    """Parse model, dynamic-texture or icon actions without confusing their resource types."""
    raw = actor_data(client, actor)
    if selector < 0 or len(raw) < 2 or selector >= struct.unpack_from("<H", raw)[0] or 2 + 6 * (selector + 1) > len(raw):
        raise ContentError("道具没有对应的装备动作。")
    start, length = struct.unpack_from("<IH", raw, 2 + 6 * selector)
    if not length or start + length > len(raw):
        raise ContentError("装备动作为空或越界。")
    code, cursor = raw[start:start + length], 0
    result = {"actor": actor, "selector": selector, "scale": [1, 1, 1], "warnings": []}
    sizes = {b"PT": 1, b"AT": 2, b"XB": 32, b"FR": 2, b"AP": 2,
             b"OC": 8, b"CS": 12, b"VO": 0, b"e\0": 0, b"XF": 4,
             b"IS": 12, b"PS": 2, b"DE": 0}
    # Native 0x4966a0..0x496788: numeric opcodes 100..106 only OR flags at actor +0x80.
    # They consume no payload. Keep their effect explicit instead of guessing blend/depth behavior.
    sizes.update({struct.pack('<H', value): 0 for value in range(100, 107)})
    ended = False
    while cursor + 2 <= len(code):
        op = code[cursor:cursor + 2]
        cursor += 2
        if op not in sizes or cursor + sizes[op] > len(code):
            raise ContentError(f"外观包含暂未解析的脚本指令 {op.hex()}。")
        data = code[cursor:cursor + sizes[op]]
        cursor += sizes[op]
        if op == b"PT":
            result["kind"] = data[0]
        elif op == b"FR":
            result["frame"] = struct.unpack("<H", data)[0]
        elif op == b"AP":
            result.setdefault("pose", struct.unpack("<H", data)[0])
        elif op == b"XB":
            bounds = struct.unpack("<8f", data)
            result["bounds"] = [list(bounds[:3]), list(bounds[4:7])]
        elif op == b"CS":
            result["scale"] = list(struct.unpack("<3f", data))
        elif op in (b"OC", b"XF"):
            result["warnings"].append("当前预览基础模型与贴图；脚本染色和动态特效尚未还原。")
        elif op == b"DE":
            result["animated"] = True
        elif 100 <= int.from_bytes(op, 'little') <= 106:
            result['render_flags'] = result.get('render_flags', 0) | 1 << (int.from_bytes(op, 'little') - 100)
            result['warnings'].append('已读取原生渲染标记；相关混合与深度效果尚未还原。')
        elif op == b"VO":
            ended = True
            break
    if not ended or result.get("kind") not in (0, 1, 2) or "frame" not in result:
        raise ContentError("该动作不包含可识别的模型、贴图或图标。")
    if not all(math.isfinite(v) and abs(v) < 10000 for v in result["scale"]):
        raise ContentError("模型缩放值无效。")
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    return result


def resolve_model(client: Path, frame: int) -> Path:
    """Translate the native 3000-wide model namespace through its IDX table."""
    category, index = divmod(frame, 3000)
    names = ("CHAR", "ITEM", "MAGIC", "SYS", "MAP", "OTHER")
    if category >= len(names) or not index:
        raise ContentError("模型编号不受支持。")
    blob = read_bytes(client / "MODEL" / f"ALL_{names[category]}.IDX")
    if blob[:8] != b"IDX_PLY\0" or len(blob) < 128 + index * 4:
        raise ContentError("模型索引格式无效。")
    physical = struct.unpack_from("<I", blob, 128 + (index - 1) * 4)[0]
    prefix = blob[72:104].split(b"\0", 1)[0].decode("ascii", "strict")
    if prefix not in ("CHR", "ITM", "MAG", "SYS", "MAP", "OTH") or not 1 <= physical <= 99999:
        raise ContentError("模型索引的文件名无效。")
    return client / "MODEL" / f"{prefix}{physical:05d}.PLY"


class Reader:
    """A bounds-checked binary cursor for variable-size resource arrays."""

    def __init__(self, raw: bytes, offset: int = 0):
        self.raw, self.offset = raw, offset

    def take(self, size: int) -> bytes:
        """Consume an exact slice; reject truncation and negative lengths."""
        if size < 0 or self.offset + size > len(self.raw):
            raise ContentError("模型/贴图数据截断。")
        data = self.raw[self.offset:self.offset + size]
        self.offset += size
        return data

    def unpack(self, fmt: str) -> tuple:
        """Read a little-endian structure without unchecked offsets."""
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))

    def count(self, limit: int) -> int:
        """Read an array count with a practical rendering limit."""
        count, = self.unpack("I")
        if count > limit:
            raise ContentError("资源数组超过预览限制。")
        return count


@lru_cache(maxsize=64)
def read_mesh(path: Path) -> dict:
    """Decode indexed geometry and the first native pose's per-group transforms."""
    raw = unpack_asset(path)
    reader = Reader(raw)
    if reader.unpack("I")[0] != len(raw):
        raise ContentError("模型内部长度无效。")
    lo, hi = reader.unpack("4f")[:3], reader.unpack("4f")[:3]
    if not all(math.isfinite(v) and abs(v) < 1e7 for v in (*lo, *hi)) or any(lo[a] > hi[a] for a in range(3)):
        raise ContentError("模型头包围盒无效。")
    reader.take(20)  # center and radius
    group_count = reader.count(1024)
    groups = [reader.unpack("7I") for _ in range(group_count)]
    nv = reader.count(200000)
    vertices = [reader.unpack("9fI") for _ in range(nv)]
    nf = reader.count(400000)
    triangles = [reader.unpack("3H") for _ in range(nf)]
    materials = []
    for _ in range(reader.count(1024)):
        values = reader.unpack("5i")
        label = reader.take(32).split(b"\0", 1)[0].decode("big5", "replace")
        materials.append((*values, label))
    if reader.count(400000) != nf:
        raise ContentError("模型面标志数量不匹配。")
    reader.take(nf * 4)
    frames = reader.count(10000)
    if not frames:
        raise ContentError("模型没有有效姿态。")
    matrices = [reader.unpack("16f") for _ in groups]
    meshes, all_positions, assigned, anchors = [], [None] * nv, set(), {}
    for group, matrix in zip(groups, matrices):
        flags, count, first, end, faces, face_first, face_end = group
        if first + count > nv or face_first + faces > nf or end != first + count or face_end != face_first + faces:
            raise ContentError("模型分组范围无效。")
        points = []
        for index, vertex in enumerate(vertices[first:first + count], first):
            if index in assigned:
                raise ContentError("模型顶点被重复分组。")
            assigned.add(index)
            x, y, z = vertex[:3]
            transformed = [matrix[4*r]*x + matrix[4*r+1]*y + matrix[4*r+2]*z + matrix[4*r+3] for r in range(3)]
            if not all(math.isfinite(v) and abs(v) < 1e7 for v in transformed):
                raise ContentError("模型分组包含无效坐标。")
            all_positions[index] = transformed
            points.append(transformed)
        if flags >> 24 and points:
            anchors[str(flags >> 24)] = {'matrix': list(matrix), 'points': points}
    if len(assigned) != nv:
        raise ContentError("模型存在未分组的顶点。")
    flat_indices = [i for triangle in triangles for i in triangle]
    for material_index, material in enumerate(materials):
        icount, istart, iend, matflags, texture, label = material
        if icount < 0 or istart < 0 or iend != istart + icount or iend > len(flat_indices) or icount % 3:
            raise ContentError("模型材质索引范围无效。")
        if matflags & 0xff000000:
            continue  # native renderer skips marked helpers/attachment geometry
        source_indices = flat_indices[istart:iend]
        if any(i >= nv for i in source_indices):
            raise ContentError("模型三角形索引越界。")
        used = sorted(set(source_indices))
        remap = {source: target for target, source in enumerate(used)}
        positions = [v for i in used for v in all_positions[i]]
        uvs = [v for i in used for v in vertices[i][7:9]]
        indices = [remap[i] for i in source_indices]
        if not all(math.isfinite(v) and abs(v) < 1e7 for v in (*positions, *uvs)):
            raise ContentError("模型包含无效坐标。")
        meshes.append({"positions": positions, "uvs": uvs, "indices": indices,
                       "texture": texture if 0 <= texture <= 99999 else None, "material": label,
                       "material_index": material_index, 'flags': matflags})
    if not meshes or not all_positions:
        raise ContentError("模型没有可见网格。")
    actual = [[min(p[a] for p in all_positions) for a in range(3)],
              [max(p[a] for p in all_positions) for a in range(3)]]
    # The stored culling box may conservatively include the origin/empty space (e.g. ITM00051).
    # Require containment, not identical extrema; frame the viewport from actual geometry.
    if frames == 1 and any(actual[0][a] < lo[a] - .05 or actual[1][a] > hi[a] + .05 for a in range(3)):
        raise ContentError("首帧网格超出模型头声明的包围盒，暂不预览该模型。")
    return {"file": path.name, "bounds": actual, "meshes": meshes, "frames": frames,
            "vertices": nv, "triangles": nf, "anchors": anchors, "declared_bounds": [list(lo), list(hi)]}


def item_model(client: Path, record: bytes, *, display: bool = False) -> dict:
    """Follow Object actor/selector, ACT frame and IDX file mappings to real geometry."""
    actor, mode = struct.unpack_from("<HH", record, 60)
    action = read_action(client, actor, mode if display else mode + 2)
    if action['kind'] != 0:
        raise ContentError("此穿戴外观使用人物贴图，请切换人物穿戴预览；单件展示可切换展示外观。")
    mesh = read_mesh(resolve_model(client, action["frame"]))
    return {**mesh, "action": action, "warnings": action["warnings"] +
            (["当前显示模型首帧，尚未播放模型动画。"] if mesh["frames"] > 1 else [])}


@lru_cache(maxsize=128)
def texture_png(client: Path, texture: int) -> bytes:
    """Decode TEX scanline runs as RGB555 and encode RGBA PNG using the standard library."""
    if not 0 <= texture <= 99999:
        raise ContentError("贴图编号无效。")
    raw = unpack_asset(client / "TEX" / f"ALL{texture:05d}.TEX")
    return image_png(raw)


def image_png(raw: bytes) -> bytes:
    """Decode bounded RGB555 scanlines shared by TEX and TSW into a transparent PNG."""
    reader = Reader(raw)
    marker, width, height, flags = reader.unpack("4H")
    if marker != 65535 or flags & 0x7fff != 16 or not (1 <= width <= 2048 and 1 <= height <= 2048):
        raise ContentError("不支持的 TEX 像素格式或尺寸。")
    pixels = bytearray()
    for _ in range(height):
        start = reader.offset
        row_size, = reader.unpack("H")
        end = start + (row_size & 0x7fff)
        if end > len(raw) or end <= reader.offset:
            raise ContentError("TEX 行长度无效。")
        row, x, ended = bytearray(width * 4), 0, False
        while reader.offset + 2 <= end:
            token, = reader.unpack("H")
            if not token:
                ended = True
                break
            if token & 0xc000 == 0xc000:
                x += token & 0x3fff
            elif token & 0xc000:
                raise ContentError("该 TEX 使用暂未支持的游程指令。")
            else:
                if x + token > width or reader.offset + token * 2 > end:
                    raise ContentError("TEX 像素游程越界。")
                for value in reader.unpack(f"{token}H"):
                    row[x*4:x*4+4] = bytes((((value >> 10) & 31) * 255 // 31,
                                           ((value >> 5) & 31) * 255 // 31,
                                           (value & 31) * 255 // 31, 255))
                    x += 1
            if x > width:
                raise ContentError("TEX 透明游程越界。")
        if not ended or reader.offset != end or x != width:
            raise ContentError("TEX 行没有完整覆盖图像。")
        pixels.extend(b"\0" + row)
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
