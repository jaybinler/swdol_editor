"""Read-only preflight for a staged patch against the user's explicit server share."""

from pathlib import Path
import argparse
import json
import sys

from xyj_content.binary import ContentError, read_bytes
from xyj_content.files import output_path, read_json, sha256, write_json
from xyj_content.patch import verify_patch
from snapshot_server import STATIC_FILES


def check_server(root: Path, share: Path, patch: Path, snapshot: Path) -> dict:
    """Compare static server hashes only; do not stop processes, write SMB files or read databases."""
    if not str(share).startswith("\\\\"):
        raise ContentError("请提供明确的 UNC 共享目录。")
    patch_dir = output_path(root, patch)
    verify_patch(root, patch_dir)
    manifest = read_json(patch_dir / "manifest.json")
    evidence = read_json(root / snapshot)
    recorded_share = evidence.get("share", "")
    if (evidence.get("kind") != "read_only_static_file_snapshot" or not isinstance(recorded_share, str)
            or recorded_share.rstrip("\\/").casefold() != str(share).rstrip("\\/").casefold()):
        raise ContentError("共享目录与静态快照证据不匹配。")
    entries = evidence.get("files")
    if (not isinstance(entries, list) or len(entries) != len(STATIC_FILES)
            or {e.get("path") for e in entries if isinstance(e, dict)} != set(STATIC_FILES)):
        raise ContentError("静态快照文件集合无效。")
    results = []
    for entry in entries:
        relative = entry["path"]
        expected = manifest["sources"]["server_object"]["sha256"] if relative == "qingfeng/Object.dbf" else entry["sha256"]
        blob = read_bytes(share / relative)
        digest = sha256(blob)
        if sha256(read_bytes(share / relative)) != digest:
            raise ContentError(f"检查期间远端文件变化：{relative}")
        results.append({"path": relative, "expected_sha256": expected, "actual_sha256": digest,
                        "matches": digest == expected})
    return {"share": str(share), "remote_static_baseline_matches": all(e["matches"] for e in results),
            "files": results, "remote_files_written": False, "database_files_read": False,
            "running_process_memory_verified": False, "gameplay_verified": False}


def main() -> int:
    """Persist the local preflight report and return failure on any remote hash mismatch."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--share", type=Path, required=True)
    parser.add_argument("--patch", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        result = check_server(root, args.share, args.patch, args.snapshot)
        write_json(output_path(root, args.out) / "remote_preflight.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["remote_static_baseline_matches"] else 2
    except (ContentError, OSError, KeyError, TypeError) as exc:
        print(f"远端版本核对失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
