"""Clone complete native actor programs, retaining all poses and shared mesh data."""

import math
import struct

from .binary import ContentError, decompress, read_bytes, MAX_FILE_BYTES

SIZES = {b'PT': 1, b'AT': 2, b'XB': 32, b'FR': 2, b'AP': 2, b'OC': 8,
         b'CS': 12, b'VO': 0, b'e\0': 0, b'XF': 4, b'IS': 12, b'PS': 2, b'DE': 0,
         # SWDOL 0x49586b consumes one u16; 0x494901 handles the payload-free pose wait.
         b'VW': 2, b'2O': 0,
         **{struct.pack('<H', i): 0 for i in range(100, 107)}}
ACT_PATH = 'client/Act/all_char.act'


def scaled_actor(raw: bytes, factors: list) -> bytes:
    """Append modified action bodies; preserve the original program and every non-transform byte."""
    if (not isinstance(factors, list) or len(factors) != 3 or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or
                not math.isfinite(v) or not .25 <= v <= 4 for v in factors)):
        raise ContentError('角色缩放须为三个 0.25 至 4 的有限数值。')
    if len(raw) < 2:
        raise ContentError('角色动作头截断。')
    count = struct.unpack_from('<H', raw)[0]
    if not 1 <= count <= 1024 or 2 + count * 6 > len(raw):
        raise ContentError('角色动作目录无效。')
    output = bytearray(raw)
    for selector in range(count):
        start, length = struct.unpack_from('<IH', raw, 2 + selector * 6)
        if not length:
            continue
        if start < 2 + count * 6 or start + length > len(raw):
            raise ContentError('角色动作范围无效。')
        code, cursor, has_scale, ended = bytearray(raw[start:start+length]), 0, False, False
        while cursor + 2 <= len(code):
            op = bytes(code[cursor:cursor+2]); cursor += 2
            size = SIZES.get(op)
            if size is None or cursor + size > len(code):
                raise ContentError(f'动作 {selector} 含未解析指令，暂不能创建缩放变体。')
            if op == b'CS':
                values = struct.unpack_from('<3f', code, cursor)
                values = [v * s for v, s in zip(values, factors)]
                if not all(math.isfinite(v) and abs(v) < 10000 for v in values):
                    raise ContentError('动作缩放超出原生安全范围。')
                struct.pack_into('<3f', code, cursor, *values); has_scale = True
            elif op == b'XB':
                values = list(struct.unpack_from('<8f', code, cursor))
                for axis, factor in enumerate(factors):
                    values[axis] *= factor; values[axis+4] *= factor
                if not all(math.isfinite(value) and abs(value) < 1e7 for value in values):
                    raise ContentError('动作包围盒包含无效坐标。')
                struct.pack_into('<8f', code, cursor, *values)
            elif op in (b'VO', b'2O'):
                ended = True
                break
            cursor += size
        if not ended or len(code) > 65535:
            raise ContentError(f'角色动作 {selector} 缺少结束指令或超出长度限制。')
        if not has_scale:
            code[:0] = b'CS' + struct.pack('<3f', *factors)
        struct.pack_into('<IH', output, 2 + selector * 6, len(output), len(code))
        output.extend(code)
    return bytes(output)


def build_variants(client, loaded, edits, *, source=None, bundles=None):
    """Allocate only empty unreferenced actor slots; return resources and target-to-actor bindings."""
    if any(e.get('model_ref') for e in edits):
        if bundles is None:raise ContentError('模型编辑草稿缺少独立资源包。')
        from .model_compiler import compile_models
        return compile_models(client,loaded,edits,bundles=bundles,source=source)
    wanted = [e for e in edits if e['scale'] != [1, 1, 1]]
    if not wanted:
        return {}, {}
    raw = read_bytes(client / 'Act' / 'all_char.act') if source is None else source
    if len(raw) < 10 + 3000 * 12:
        raise ContentError('角色动作包目录不完整。')
    output, bindings = bytearray(raw), {}
    used = {struct.unpack_from('<H', record, 60)[0]
            for table in (loaded.server, loaded.client) for record in table.records}
    for edit in wanted:
        record = next(r for r in loaded.server.records if struct.unpack_from('<I', r, 4)[0] == edit['appearance_id'])
        actor, selector = struct.unpack_from('<2H', record, 60)
        if not 1 <= actor < 3000:
            raise ContentError('缩放变体仅支持角色 ACT 命名空间。')
        available = next((slot for slot in range(1, 3000) if slot not in used and
                          struct.unpack_from('<I', output, 10 + (slot-1)*12)[0] == 0), None)
        if available is None:
            raise ContentError('没有未占用的角色动作编号。')
        pos = 10 + (actor-1)*12
        size, offset, tag, packed = struct.unpack_from('<IIHH', raw, pos)
        if tag != actor or not size or offset < 10 + 3000*12 or offset + (packed or size) > len(raw):
            raise ContentError('来源角色动作索引无效。')
        program = decompress(raw[offset:offset+packed], size) if packed else raw[offset:offset+size]
        variant = scaled_actor(program, edit['scale'])
        struct.pack_into('<IIHH', output, 10 + (available-1)*12, len(variant), len(output), available, 0)
        output.extend(variant)
        if len(output) > MAX_FILE_BYTES:
            raise ContentError('独立外观动作包超过大小上限，请分批创建。')
        used.add(available); bindings[edit['target_id']] = available
    return {ACT_PATH: {'source': raw, 'data': bytes(output)}}, bindings
