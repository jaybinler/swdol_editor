"""The verified subset of the 832-byte Object record; all other bytes are opaque."""

from dataclasses import dataclass
import struct

from .binary import ContentError, FixedTable
from .language import LanguageTable

OBJECT_STRIDE = 832
NAME_RANGE = (12, 28)
DESCRIPTION_RANGE = (600, 800)


def fixed_text(record: bytes, start: int, end: int) -> str:
    """Decode one bounded GBK C string; never replace undecodable bytes silently."""
    value = record[start:end]
    if b"\0" not in value:
        raise ContentError(f"字段 {start}:{end} 缺少文本结束符。")
    try:
        return value.split(b"\0", 1)[0].decode("gbk", errors="strict")
    except UnicodeError as exc:
        raise ContentError(f"字段 {start}:{end} 不是有效 GBK 文本。") from exc


def require_object_table(table: FixedTable) -> None:
    """Reject any fixed table whose observed record width is not the Object layout."""
    if table.stride != OBJECT_STRIDE:
        raise ContentError(f"Object 记录宽度必须为 {OBJECT_STRIDE}，实际为 {table.stride}。")


@dataclass(frozen=True)
class ObjectView:
    """A read-only projection; appearance word remains explicitly a candidate field."""

    row: int
    object_id: int
    type_code: int
    name: str
    description: str
    appearance_word_candidate: int
    name_index: int | None = None
    description_index: int | None = None


def view_record(record: bytes, row: int, language: LanguageTable | None = None) -> ObjectView:
    """Interpret server inline text or client language references without mutating data."""
    if len(record) != OBJECT_STRIDE:
        raise ContentError("Object 记录宽度无效。")
    object_id = struct.unpack_from("<I", record, 4)[0]
    type_code = struct.unpack_from("<H", record, 8)[0]
    appearance = struct.unpack_from("<H", record, 28)[0]
    if language is None:
        return ObjectView(row, object_id, type_code, fixed_text(record, *NAME_RANGE),
                          fixed_text(record, *DESCRIPTION_RANGE), appearance)
    name_id = struct.unpack_from("<I", record, NAME_RANGE[0])[0]
    description_id = struct.unpack_from("<I", record, DESCRIPTION_RANGE[0])[0]
    return ObjectView(row, object_id, type_code, language.text(name_id),
                      language.text(description_id), appearance, name_id, description_id)


def find_unique(table: FixedTable, object_id: int) -> tuple[int, bytes]:
    """Select exactly one nonzero ID; ambiguous IDs are rejected instead of overwritten."""
    require_object_table(table)
    matches = [(i, record) for i, record in enumerate(table.records)
               if struct.unpack_from("<I", record, 4)[0] == object_id]
    if object_id == 0 or len(matches) != 1:
        raise ContentError(f"物件 ID {object_id} 必须唯一且非零，找到 {len(matches)} 条。")
    return matches[0]


def without_text(record: bytes) -> bytes:
    """Return bytes outside the verified name/description storage regions for comparison."""
    return record[:12] + record[28:600] + record[800:]


def encode_fixed_text(text: str, width: int) -> bytes:
    """Encode a null-terminated GBK field, refusing truncation or unsupported characters."""
    if "\0" in text:
        raise ContentError("文本不能包含零字节。")
    try:
        value = text.encode("gbk", errors="strict")
    except UnicodeError as exc:
        raise ContentError("文本含游戏 GBK 编码不支持的字符。") from exc
    if len(value) >= width:
        raise ContentError(f"文本超过 {width - 1} 字节限制。")
    return value + bytes(width - len(value))


def clone_record(record: bytes, object_id: int, name: str, description: str,
                 indices: tuple[int, int] | None = None) -> bytes:
    """Clone only ID and text fields in memory; preserve stats and appearance bytes exactly."""
    if len(record) != OBJECT_STRIDE or not 1 <= object_id <= 32767:
        raise ContentError("测试物件 ID 必须在保守范围 1 至 32767；引擎真实上限仍待运行验证。")
    new = bytearray(record)
    struct.pack_into("<I", new, 4, object_id)
    if indices is None:
        new[12:28] = encode_fixed_text(name, 16)
        new[600:800] = encode_fixed_text(description, 200)
    else:
        # Preserve the client's unused text-region bytes rather than zeroing unknown padding.
        struct.pack_into("<I", new, 12, indices[0])
        struct.pack_into("<I", new, 600, indices[1])
    return bytes(new)
