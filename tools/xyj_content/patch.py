"""Build and verify staged clone-only patches; there is deliberately no deploy command."""

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import struct
import tempfile

from .binary import ContentError, FixedTable, decode_table, encode_table, read_bytes
from .catalog import Inputs, Loaded, load_inputs, source_hashes
from .files import atomic_write, output_path, read_json, sha256, write_json
from .language import decode_language, encode_language
from .objects import clone_record, encode_fixed_text, find_unique, view_record, without_text

PAYLOAD_PATHS = {
    "server_object": "server/qingfeng/Object.dbf",
    "client_object": "client/Main/Object.dbf",
    "client_language": "client/Main/Language.DBF",
}


@dataclass(frozen=True)
class CloneSpec:
    """An explicit clone operation; unverified attribute/model edits are unsupported."""

    template_id: int
    new_id: int
    name: str
    description: str

    @classmethod
    def from_dict(cls, value: dict) -> "CloneSpec":
        """Validate strict spec keys, integer types, ID range and game text lengths."""
        if set(value) != {"version", "template_id", "new_id", "name", "description"}:
            raise ContentError("规格只允许 version、template_id、new_id、name、description。")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ContentError("不支持的规格版本。")
        for key in ("template_id", "new_id"):
            if type(value[key]) is not int or not 1 <= value[key] <= 32767:
                raise ContentError(f"{key} 必须为 1 至 32767 的整数。")
        for key in ("name", "description"):
            if not isinstance(value[key], str) or not value[key].strip():
                raise ContentError(f"{key} 必须是非空文本。")
        encode_fixed_text(value["name"], 16)
        encode_fixed_text(value["description"], 200)
        return cls(value["template_id"], value["new_id"], value["name"], value["description"])

    def to_dict(self) -> dict:
        """Serialize a reproducible operation for the offline manifest."""
        return {"version": 1, "template_id": self.template_id, "new_id": self.new_id,
                "name": self.name, "description": self.description}


def insertion_position(table: FixedTable, type_code: int) -> int:
    """Insert after the last matching type, following the existing added-ID-317 layout."""
    indices = [i for i, r in enumerate(table.records) if struct.unpack_from("<H", r, 8)[0] == type_code]
    if not indices:
        raise ContentError("源表没有可用的同分类插入位置。")
    return indices[-1] + 1


def prepare_tables(loaded: Loaded, spec: CloneSpec) -> tuple[dict, dict]:
    """Generate in-memory outputs only when the two template records demonstrably agree."""
    si, sr = find_unique(loaded.server, spec.template_id)
    ci, cr = find_unique(loaded.client, spec.template_id)
    for table in (loaded.server, loaded.client):
        if any(struct.unpack_from("<I", record, 4)[0] == spec.new_id for record in table.records):
            raise ContentError(f"新物件 ID {spec.new_id} 已存在，拒绝覆盖。")
    sv, cv = view_record(sr, si), view_record(cr, ci, loaded.language)
    if sv.type_code != 2 or cv.type_code != 2:
        raise ContentError("当前克隆器只支持已观察到的分类码 2 模板。")
    if without_text(sr) != without_text(cr) or sv.name != cv.name or sv.description != cv.description:
        raise ContentError("两端模板的属性或文本不一致，须先人工确认版本。")
    name_index = len(loaded.language.entries)
    language = loaded.language.append_texts(spec.name, spec.description)
    new_server = clone_record(sr, spec.new_id, spec.name, spec.description)
    new_client = clone_record(cr, spec.new_id, spec.name, spec.description, (name_index, name_index + 1))
    server_position = insertion_position(loaded.server, sv.type_code)
    client_position = insertion_position(loaded.client, cv.type_code)
    blobs = {
        "server_object": encode_table(loaded.server.insert(server_position, new_server)),
        "client_object": encode_table(loaded.client.insert(client_position, new_client)),
        "client_language": encode_language(language),
    }
    details = {
        "template_name": sv.name, "type_code": sv.type_code,
        "server_insert_row": server_position, "client_insert_row": client_position,
        "new_name_index": name_index, "new_description_index": name_index + 1,
        "appearance_policy": "preserve_all_template_appearance_and_unknown_bytes",
        "insertion_policy": "after_last_existing_record_of_same_type",
        "baseline_client_only_ids": sorted(
            {struct.unpack_from('<I', r, 4)[0] for r in loaded.client.records}
            - {struct.unpack_from('<I', r, 4)[0] for r in loaded.server.records}),
    }
    return blobs, details


def relative_source(root: Path, path: Path) -> str:
    """Require manifest input paths to stay inside this workspace, even through links."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ContentError("补丁输入必须是项目内的本地副本。") from exc


def verify_patch(root: Path, requested: Path, *, source_overrides: dict[str, Path] | None = None) -> dict:
    """Check hashes, exact clone semantics, all original records and all original strings."""
    directory = output_path(root, requested)
    manifest = read_json(directory / "manifest.json")
    if manifest.get("format") != "xyj-content-patch/v1":
        raise ContentError("不是受支持的补丁清单。")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(PAYLOAD_PATHS):
        raise ContentError("补丁来源清单无效。")
    if source_overrides is not None and set(source_overrides) != set(PAYLOAD_PATHS):
        raise ContentError("验证基线必须完整包含三个原始文件。")
    source_paths = {}
    for key, entry in sources.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ContentError("补丁来源路径无效。")
        declared_path = (root / entry["path"]).resolve()
        if Path(entry["path"]).is_absolute() or not declared_path.is_relative_to(root.resolve()):
            raise ContentError("补丁来源路径越界。")
        path = (root / source_overrides[key]).resolve() if source_overrides is not None else declared_path
        if not path.is_relative_to(root.resolve()):
            raise ContentError("验证基线路径越界。")
        blob = read_bytes(path)
        if sha256(blob) != entry.get("sha256") or len(blob) != entry.get("bytes"):
            raise ContentError(f"源文件已变化：{entry['path']}；不能使用旧补丁基线。")
        source_paths[key] = path
    loaded = load_inputs(Inputs(source_paths["server_object"], source_paths["client_object"],
                                source_paths["client_language"]))
    spec_value = manifest.get("spec")
    if not isinstance(spec_value, dict):
        raise ContentError("补丁缺少规格对象。")
    spec = CloneSpec.from_dict(spec_value)
    expected, details = prepare_tables(loaded, spec)
    entries = manifest.get("files")
    if not isinstance(entries, dict) or set(entries) != set(PAYLOAD_PATHS):
        raise ContentError("补丁文件清单无效。")
    payloads = {}
    for key, relative in PAYLOAD_PATHS.items():
        entry = entries[key]
        if not isinstance(entry, dict) or entry.get("path") != relative:
            raise ContentError("补丁文件路径被更改或越界。")
        path = (directory / relative).resolve()
        if not path.is_relative_to(directory):
            raise ContentError("补丁文件链接越界。")
        blob = read_bytes(path)
        if sha256(blob) != entry.get("sha256") or len(blob) != entry.get("bytes"):
            raise ContentError(f"补丁文件校验失败：{relative}")
        payloads[key] = blob
    server = decode_table(payloads["server_object"])
    client = decode_table(payloads["client_object"])
    language = decode_language(payloads["client_language"])
    # Compare decoded values, not compressor output: multiple valid LZO encodings can exist.
    if server != decode_table(expected["server_object"]) or client != decode_table(expected["client_object"]):
        raise ContentError("补丁 Object 包含未批准的记录或字段更改。")
    if language != decode_language(expected["client_language"]):
        raise ContentError("补丁语言表包含未批准的原文本/元数据更改。")
    si, sr = find_unique(server, spec.new_id)
    ci, cr = find_unique(client, spec.new_id)
    if server.records[:si] + server.records[si + 1:] != loaded.server.records:
        raise ContentError("服务端原有记录未完整保留。")
    if client.records[:ci] + client.records[ci + 1:] != loaded.client.records:
        raise ContentError("客户端原有记录未完整保留。")
    if language.entries[:-2] != loaded.language.entries:
        raise ContentError("原有语言索引被修改。")
    sv, cv = view_record(sr, si), view_record(cr, ci, language)
    if (sv.name, sv.description) != (cv.name, cv.description):
        raise ContentError("新增物件两端文本不一致。")
    return {"status": "offline_validated_runtime_pending", "new_id": spec.new_id,
            "name": sv.name, "server_rows": len(server.records), "client_rows": len(client.records),
            "language_entries": len(language.entries), "all_existing_records_preserved": True,
            "all_existing_language_entries_preserved": True, "matching_new_text": True,
            "source_hashes_match": True, "appearance_bytes_preserved": True,
            "running_process_memory_verified": False, "gameplay_verified": False,
            "baseline_client_only_ids": details["baseline_client_only_ids"]}


def build_patch(root: Path, requested: Path, loaded: Loaded, spec: CloneSpec) -> dict:
    """Create a new offline patch directory, verify it, then publish that directory atomically."""
    destination = output_path(root, requested)
    if destination.exists():
        raise ContentError("补丁输出目录已存在；请选择新目录，禁止覆盖旧补丁。")
    blobs, details = prepare_tables(loaded, spec)
    sources = source_hashes(loaded)
    for key, path in zip(PAYLOAD_PATHS, (loaded.paths.server, loaded.paths.client, loaded.paths.language)):
        sources[key]["path"] = relative_source(root, path)
    manifest = {
        "format": "xyj-content-patch/v1", "status": "offline_validated_runtime_pending",
        "spec": spec.to_dict(), "sources": sources, "details": details,
        "files": {key: {"path": PAYLOAD_PATHS[key], "sha256": sha256(blob), "bytes": len(blob)}
                  for key, blob in blobs.items()},
        "release_requirements": ["运行中服务端 Object.dbf 的 SHA-256 必须与 server_object 来源匹配。",
                                 "客户端 Object 与 Language 必须作为一组更新。",
                                 "在线服务器和数据库尚未操作；游戏内验收尚未完成。",
                                 "补丁不自动提供物品；获取/发放方式需另行验证。"],
    }
    staging = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".xyj-build-", dir=destination.parent)).resolve()
        for key, blob in blobs.items():
            atomic_write(staging / PAYLOAD_PATHS[key], blob)
        write_json(staging / "manifest.json", manifest)
        result = verify_patch(root, staging)
        write_json(staging / "verification.json", result)
        atomic_write(staging / "运行前检查.txt", (
            "此补丁仅通过离线结构与字节校验，尚未通过游戏验收。\n"
            "不要直接覆盖正在运行的服务器或可用客户端。\n"
            "先核对运行端原文件 SHA-256、建立测试副本和恢复点，再安排运行验证。\n"
            "客户端 Main/Object.dbf 与 Main/Language.DBF 必须配套使用。\n"
            "本工具不部署、不重启、不修改数据库、不提供未经验证的 GM 命令。\n"
        ).encode("utf-8-sig"))
        if destination.exists():
            raise ContentError("输出目录在构建期间已被创建，拒绝覆盖。")
        os.rename(staging, destination)
        staging = None
        return result
    except OSError as exc:
        raise ContentError(f"补丁暂存/提交失败：{exc.strerror}") from exc
    finally:
        # Only remove this invocation's verified temporary directory under workspace/output.
        if staging is not None and staging.exists():
            allowed = (root.resolve() / "output").resolve()
            resolved = staging.resolve()
            if resolved != allowed and resolved.is_relative_to(allowed) and resolved.name.startswith(".xyj-build-"):
                shutil.rmtree(resolved)
