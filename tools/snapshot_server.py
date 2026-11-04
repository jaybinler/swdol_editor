"""Read selected static files from an explicit SMB share into a local research snapshot."""

from pathlib import Path
import argparse
import json
import sys

from xyj_content.binary import ContentError, decode_table, read_bytes
from xyj_content.files import atomic_write, output_path, sha256, write_json

STATIC_FILES = (
    "qingfeng/Object.dbf", "qingfeng/Table.TB", "qingfeng/Combine.TB", "qingfeng/Character.DBF",
    "qingfeng/NewLocalServer.exe", "qingfeng/MasterServerEx_GBK_NoIP.exe", "qingfeng/DataCenter.exe",
    "qingfeng/ChatServer.exe", "GMTools/Object.dbf", "JoyPark/JoyparkLoginServer.exe",
)
CONFIG_KEYS = {
    "qingfeng/MasterServer.ini": {"LanIP", "WanIP", "LoginServerIP", "FirstPort", "ClientVersion", "OBJECTFILE"},
    "qingfeng/BaseIni.ini": {"ServerIP", "ServerPort", "DataPath"},
    "qingfeng/dataCenter.ini": {"GameDB", "DBHost", "ObjectFilePath"},
}


def snapshot(root: Path, share: Path, requested: Path) -> dict:
    """Read only static files, double-check source stability, and persist no credentials or live DB files."""
    destination = output_path(root, requested)
    if destination.exists():
        raise ContentError("快照目录已存在，请选择新目录。")
    if not str(share).startswith("\\\\"):
        raise ContentError("请明确提供 UNC 共享目录。")
    results = []
    for relative in STATIC_FILES:
        remote = share / relative
        blob = read_bytes(remote)
        digest = sha256(blob)
        # Files can change while a service is running: never accept a torn content snapshot.
        if sha256(read_bytes(remote)) != digest:
            raise ContentError(f"读取期间远端文件发生变化：{relative}")
        local_original = root / "XYJ_SEVVER" / relative
        local_digest = sha256(read_bytes(local_original)) if local_original.exists() else None
        entry = {"path": relative, "bytes": len(blob), "sha256": digest,
                 "local_original_sha256": local_digest, "matches_local_original": digest == local_digest}
        if relative.endswith("Object.dbf"):
            table = decode_table(blob)
            entry.update(records=len(table.records), stride=table.stride)
        results.append(entry)
        # Only gameplay static tables are copied; executable hashes are sufficient for this snapshot.
        if remote.suffix.lower() in (".dbf", ".tb"):
            atomic_write(destination / "server" / relative, blob)
    configs = {}
    for relative, allowed in CONFIG_KEYS.items():
        text = read_bytes(share / relative).decode("gb18030", errors="strict").replace("\r", "")
        values = {}
        for line in text.splitlines():
            line = line.split(";", 1)[0]
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() in allowed:
                values[key.strip()] = value.strip()
        configs[relative] = values
    # Re-read every static file before recording completion so changes invalidate this snapshot.
    for entry in results:
        if sha256(read_bytes(share / entry["path"])) != entry["sha256"]:
            raise ContentError(f"快照完成前远端版本发生变化：{entry['path']}")
    result = {"share": str(share), "kind": "read_only_static_file_snapshot",
              "remote_files_written": False, "database_files_read": False,
              "remote_process_memory_verified": False, "files": results, "configuration_allowlist": configs}
    write_json(destination / "snapshot.json", result)
    return result


def main() -> int:
    """Snapshot the user-specified share; return nonzero without claiming success on failure."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--share", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = snapshot(args.root.resolve(), args.share, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ContentError, OSError, UnicodeError) as exc:
        print(f"远端只读快照失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
