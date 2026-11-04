"""Command-line entry points for local inspection, staged patches and source auditing."""

import argparse
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
import sys

from .binary import ContentError
from .catalog import Inputs, export_catalog, load_inputs
from .files import output_path, read_json, write_json
from .objects import view_record
from .patch import PAYLOAD_PATHS, CloneSpec, build_patch, verify_patch


def audit_originals(root: Path, inventory: Path) -> dict:
    """Hash only the two original game directories and compare with the research baseline."""
    baseline = read_json(inventory)
    if set(baseline.get("scope", [])) != {"XYJ_SEVVER", "swdol"}:
        raise ContentError("基线必须只包含 XYJ_SEVVER 与 swdol。")
    expected = set()
    changed = []
    for entry in baseline.get("files", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ContentError("文件基线条目无效。")
        relative = Path(entry["path"])
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root.resolve()) or relative.parts[0] not in baseline["scope"]:
            raise ContentError("文件基线路径越界。")
        expected.add(relative.as_posix())
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            if digest.hexdigest() != entry.get("sha256") or path.stat().st_size != entry.get("bytes"):
                changed.append(relative.as_posix())
        except OSError:
            changed.append(relative.as_posix())
    if not expected:
        raise ContentError("文件基线为空。")
    try:
        actual = {p.relative_to(root).as_posix() for scope in baseline["scope"]
                  for p in (root / scope).rglob("*") if p.is_file()}
    except OSError as exc:
        raise ContentError(f"目录清点失败：{exc.strerror}") from exc
    return {"original_files": len(expected), "all_original_sha256_unchanged": not changed,
            "changed_or_unreadable": changed, "added_in_original_directories": sorted(actual - expected),
            "missing_in_original_directories": sorted(expected - actual)}


def make_parser() -> argparse.ArgumentParser:
    """Define explicit read, build and verify operations; no remote/deployment operations exist."""
    parser = argparse.ArgumentParser(description="轩辕剑内容工具：只读目录与离线双端补丁")
    subs = parser.add_subparsers(dest="command", required=True)
    for command in ("catalog", "item", "build-clone", "verify-patch", "verify-originals"):
        sub = subs.add_parser(command)
        sub.add_argument("--root", type=Path, default=Path.cwd())
        if command in ("catalog", "item", "build-clone"):
            sub.add_argument("--server-object", type=Path)
            sub.add_argument("--client-object", type=Path)
            sub.add_argument("--language", type=Path)
        if command == "catalog":
            sub.add_argument("--out", type=Path, default=Path("output/catalog"))
        elif command == "item":
            sub.add_argument("--id", type=int, required=True)
        elif command == "build-clone":
            sub.add_argument("--spec", type=Path, required=True)
            sub.add_argument("--out", type=Path, required=True)
        elif command == "verify-patch":
            sub.add_argument("--patch", type=Path, required=True)
            sub.add_argument("--baseline-dir", type=Path, help="部署后使用包含 server/client 子目录的原文件备份")
        elif command == "verify-originals":
            sub.add_argument("--inventory", type=Path, default=Path("research/2026-10-28/inventory_sha256.json"))
            sub.add_argument("--out", type=Path, default=Path("output/original-audit"))
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run a local command and return a failure code for any invalid input or changed baseline."""
    args = make_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "verify-patch":
            overrides = {key: args.baseline_dir / relative for key, relative in PAYLOAD_PATHS.items()} if args.baseline_dir else None
            result = verify_patch(root, args.patch, source_overrides=overrides)
        elif args.command == "verify-originals":
            result = audit_originals(root, root / args.inventory)
            destination = output_path(root, args.out)
            write_json(destination / "verification.json", result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if (result["all_original_sha256_unchanged"] and not result["added_in_original_directories"]
                         and not result["missing_in_original_directories"]) else 2
        else:
            defaults = Inputs.from_root(root)
            loaded = load_inputs(Inputs(
                root / args.server_object if args.server_object else defaults.server,
                root / args.client_object if args.client_object else defaults.client,
                root / args.language if args.language else defaults.language,
            ))
            if args.command == "catalog":
                summary = export_catalog(root, args.out, loaded)
                result = {key: summary[key] for key in ("server_rows", "client_rows", "language_entries",
                          "compared_unique_nonzero_ids", "server_only_ids", "client_only_ids")}
                result["output"] = str(output_path(root, args.out))
            elif args.command == "build-clone":
                result = build_patch(root, args.out, loaded, CloneSpec.from_dict(read_json(root / args.spec)))
                result["output"] = str(output_path(root, args.out))
            else:
                result = {}
                for side, table, language in (("server", loaded.server, None),
                                              ("client", loaded.client, loaded.language)):
                    result[side] = [asdict(v) for i, r in enumerate(table.records)
                                    if (v := view_record(r, i, language)).object_id == args.id]
                if not result["server"] and not result["client"]:
                    raise ContentError(f"未找到物件 ID：{args.id}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ContentError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
