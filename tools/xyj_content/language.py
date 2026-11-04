"""The RTS string container as read by the local client's 0x468dd0 loader."""

from dataclasses import dataclass, replace
import struct

from .binary import ContentError, MAX_FILE_BYTES, MAX_RAW_BYTES, compress, decompress

MAX_STRINGS = 100_000
MAX_STRING_BYTES = 65536


def transform(data: bytes, xor_byte: int, rotation: int, *, encode: bool) -> bytes:
    """Apply the client's per-byte rotate/XOR transform or its inverse in memory."""
    if not 0 <= xor_byte <= 255 or not 1 <= rotation <= 7:
        raise ContentError("RTS 变换参数无效。")
    output = bytearray(len(data))
    for i, value in enumerate(data):
        if encode:
            value ^= xor_byte
            value = ((value << rotation) | (value >> (8 - rotation))) & 255
        else:
            value = (((value >> rotation) | (value << (8 - rotation))) & 255) ^ xor_byte
        output[i] = value
        rotation = rotation + 1 if rotation < 7 else 1
    return bytes(output)


@dataclass(frozen=True)
class LanguageTable:
    """Lossless null-terminated strings and the original RTS encoding metadata."""

    metadata: int
    xor_byte: int
    rotation: int
    entries: tuple[bytes, ...]

    def text(self, index: int) -> str:
        """Resolve a zero-based string index with strict game-compatible GBK decoding."""
        if not 0 <= index < len(self.entries):
            raise ContentError(f"语言索引越界：{index}")
        try:
            return self.entries[index][:-1].decode("gbk", errors="strict")
        except UnicodeError as exc:
            raise ContentError(f"语言索引 {index} 不是有效 GBK 文本。") from exc

    def append_texts(self, *texts: str) -> "LanguageTable":
        """Append new GBK strings without changing any existing text or index."""
        added = []
        for text in texts:
            if "\0" in text:
                raise ContentError("文本不能包含零字节。")
            try:
                value = text.encode("gbk", errors="strict") + b"\0"
            except UnicodeError as exc:
                raise ContentError("文本含游戏 GBK 编码不支持的字符。") from exc
            if len(value) > MAX_STRING_BYTES:
                raise ContentError("语言文本过长。")
            added.append(value)
        return replace(self, entries=(*self.entries, *added))


def decode_language(blob: bytes) -> LanguageTable:
    """Decode RTS header, LZO, byte transform and the complete length/string layout."""
    if not 24 <= len(blob) <= MAX_FILE_BYTES:
        raise ContentError("RTS 文件长度无效。")
    magic, version, size, packed_size, metadata, count = struct.unpack_from("<4s5I", blob)
    if magic != b"RTS\0" or version != 1 or packed_size != len(blob) - 24:
        raise ContentError("RTS 文件头或压缩长度无效。")
    if not 0 < count <= MAX_STRINGS or size < 4 * count + count + 2:
        raise ContentError("RTS 字符串数量或数据长度无效。")
    encoded = decompress(blob[24:], size)
    xor_byte, rotation = encoded[-2:]
    raw = transform(encoded[:-2], xor_byte, rotation, encode=False)
    lengths = struct.unpack_from(f"<{count}I", raw)
    position = 4 * count
    entries = []
    for length in lengths:
        if not 1 <= length <= MAX_STRING_BYTES or position + length > len(raw):
            raise ContentError("RTS 字符串长度越界。")
        entry = raw[position:position + length]
        if entry[-1:] != b"\0" or b"\0" in entry[:-1]:
            raise ContentError("RTS 字符串零字节边界无效。")
        entries.append(entry)
        position += length
    if position != len(raw):
        raise ContentError("RTS 存在未解释的剩余数据；禁止不完整写回。")
    return LanguageTable(metadata, xor_byte, rotation, tuple(entries))


def encode_language(table: LanguageTable) -> bytes:
    """Create a separate RTS container, preserving all existing string byte values."""
    count = len(table.entries)
    if not 0 < count <= MAX_STRINGS:
        raise ContentError("RTS 字符串数量无效。")
    for entry in table.entries:
        if not 1 <= len(entry) <= MAX_STRING_BYTES or entry[-1:] != b"\0" or b"\0" in entry[:-1]:
            raise ContentError("RTS 字符串记录无效。")
    size = 4 * count + sum(map(len, table.entries)) + 2
    if size > MAX_RAW_BYTES:
        raise ContentError("RTS 数据超过大小限制。")
    raw = struct.pack(f"<{count}I", *(len(s) for s in table.entries)) + b"".join(table.entries)
    encoded = transform(raw, table.xor_byte, table.rotation, encode=True) + bytes((table.xor_byte, table.rotation))
    packed = compress(encoded)
    blob = struct.pack("<4s5I", b"RTS\0", 1, len(encoded), len(packed), table.metadata, count) + packed
    if len(blob) > MAX_FILE_BYTES:
        raise ContentError("生成的 RTS 超过大小限制。")
    return blob
