"""Bounded readers and writers for the observed game table containers."""

from dataclasses import dataclass, replace
from pathlib import Path
import struct

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_RAW_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 1_000_000


class ContentError(ValueError):
    """A file or requested edit is unsupported; original files remain untouched."""


def read_bytes(path: Path) -> bytes:
    """Read a bounded local file, converting filesystem failures into content errors."""
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise ContentError(f"无法读取文件：{path}（{exc.strerror}）") from exc
    if len(data) > MAX_FILE_BYTES:
        raise ContentError(f"文件超过 {MAX_FILE_BYTES} 字节限制：{path}")
    return data


def decompress(data: bytes, expected: int) -> bytes:
    """Decode one bounded LZO stream and require its exact declared output length."""
    if not 0 < expected <= MAX_RAW_BYTES:
        raise ContentError(f"不支持的解压长度：{expected}")
    try:
        import lzokay
    except ImportError as exc:
        raise ContentError("缺少 lzokay；请使用项目虚拟环境并安装 requirements.txt。") from exc
    try:
        raw = lzokay.decompress(data, expected)
    except Exception as exc:
        raise ContentError(f"LZO 解压失败：{exc}") from exc
    if len(raw) != expected:
        raise ContentError("解压长度与文件头声明不符。")
    return raw


def compress(raw: bytes) -> bytes:
    """Compress bounded data and verify a lossless decode before returning it."""
    if not 0 < len(raw) <= MAX_RAW_BYTES:
        raise ContentError("待压缩数据大小无效。")
    try:
        import lzokay
        packed = lzokay.compress(raw)
    except ImportError as exc:
        raise ContentError("缺少 lzokay；请安装 requirements.txt。") from exc
    except Exception as exc:
        raise ContentError(f"LZO 压缩失败：{exc}") from exc
    if decompress(packed, len(raw)) != raw:
        raise ContentError("LZO 压缩往返校验失败。")
    return packed


@dataclass(frozen=True)
class FixedTable:
    """Immutable fixed-width records plus preserved outer-container metadata."""

    flag: int
    label: bytes
    stride: int
    records: tuple[bytes, ...]

    def append(self, record: bytes) -> "FixedTable":
        """Return a new table with one record; never change the source table."""
        if len(record) != self.stride:
            raise ContentError("新增记录宽度不符。")
        return replace(self, records=(*self.records, record))

    def insert(self, index: int, record: bytes) -> "FixedTable":
        """Insert a record in a copy while preserving existing records' relative order."""
        if not 0 <= index <= len(self.records) or len(record) != self.stride:
            raise ContentError("新增记录的位置或宽度无效。")
        return replace(self, records=(*self.records[:index], record, *self.records[index:]))



def decode_table(blob: bytes) -> FixedTable:
    """Decode observed DBF fixed tables; reject maps and inconsistent dimensions."""
    if len(blob) < 32 or len(blob) > MAX_FILE_BYTES:
        raise ContentError("DBF 文件长度无效。")
    magic, raw_size, packed_size, flag, label = struct.unpack_from("<4sIII16s", blob)
    if magic != b"DBF\0" or flag not in (0, 1):
        raise ContentError("不支持的 DBF 标记或标志位。")
    if packed_size != len(blob) - 32:
        raise ContentError("DBF 压缩长度不符，文件可能已截断。")
    raw = decompress(blob[32:], raw_size)
    if len(raw) < 8:
        raise ContentError("DBF 记录头缺失。")
    count, stride = struct.unpack_from("<II", raw)
    if count > MAX_RECORDS or stride == 0 or 8 + count * stride != len(raw):
        raise ContentError("不是受支持的固定记录表；禁止将地图当作 Object 表。")
    return FixedTable(flag, label, stride, tuple(
        raw[8 + i * stride:8 + (i + 1) * stride] for i in range(count)
    ))


def encode_table(table: FixedTable) -> bytes:
    """Encode a new container while preserving every unchanged record and header flag."""
    if table.flag not in (0, 1) or len(table.label) != 16 or not table.stride:
        raise ContentError("DBF 元数据无效。")
    if len(table.records) > MAX_RECORDS or any(len(r) != table.stride for r in table.records):
        raise ContentError("DBF 记录数量或宽度无效。")
    raw_size = 8 + len(table.records) * table.stride
    if raw_size > MAX_RAW_BYTES:
        raise ContentError("DBF 数据超过大小限制。")
    raw = struct.pack("<II", len(table.records), table.stride) + b"".join(table.records)
    packed = compress(raw)
    blob = struct.pack("<4sIII16s", b"DBF\0", len(raw), len(packed), table.flag, table.label) + packed
    if len(blob) > MAX_FILE_BYTES:
        raise ContentError("生成的 DBF 超过大小限制。")
    return blob
