"""Deploy the specifically approved ID-318 patch with fresh backups and failure recovery."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import json
import os
import sys
import uuid

from check_server_patch import check_server
from xyj_content.binary import ContentError, decode_table, read_bytes
from xyj_content.files import atomic_write, output_path, read_json, sha256, write_json
from xyj_content.language import decode_language
from xyj_content.objects import find_unique, view_record
from xyj_content.patch import PAYLOAD_PATHS, verify_patch

ORDER = ("client_language", "client_object", "server_object")


def stage_file(target: Path, data: bytes, token: str) -> Path:
    """Create a unique sibling file and flush/verify it without changing the target."""
    staging = target.with_name(f".{target.name}.xyj318-{token}.tmp")
    created = False
    try:
        with staging.open("xb") as stream:
            created = True
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if sha256(read_bytes(staging)) != sha256(data):
            raise ContentError(f"暂存文件校验失败：{target}")
        return staging
    except BaseException:
        if created:
            staging.unlink(missing_ok=True)
        raise


def verify_installed(payloads: dict[str, bytes], manifest: dict) -> dict:
    """Decode the actual installed bytes and check the newly added object on both sides."""
    for key, data in payloads.items():
        if sha256(data) != manifest["files"][key]["sha256"]:
            raise ContentError(f"已安装文件哈希不符：{key}")
    server = decode_table(payloads["server_object"])
    client = decode_table(payloads["client_object"])
    language = decode_language(payloads["client_language"])
    spec = manifest["spec"]
    si, sr = find_unique(server, spec["new_id"])
    ci, cr = find_unique(client, spec["new_id"])
    sv, cv = view_record(sr, si), view_record(cr, ci, language)
    if (sv.name, sv.description) != (spec["name"], spec["description"]) or (cv.name, cv.description) != (sv.name, sv.description):
        raise ContentError("实际安装后的新增物品文本不一致。")
    return {"new_id": spec["new_id"], "name": sv.name, "server_rows": len(server.records),
            "client_rows": len(client.records), "language_entries": len(language.entries),
            "installed_hashes_match": True, "installed_new_object_matches": True,
            "server_reload_verified": False, "gameplay_verified": False}


def deploy(root: Path, share: Path, patch: Path, requested: Path, *, remote_preflight: bool = True) -> dict:
    """Replace only the three approved data files; recover this transaction's changes on failure."""
    root = root.resolve()
    directory = output_path(root, patch)
    report_dir = output_path(root, requested)
    if report_dir.exists():
        raise ContentError("部署记录目录已存在，请使用新的目录。")
    manifest = read_json(directory / "manifest.json")
    if manifest.get("format") != "xyj-content-patch/v1" or manifest.get("spec", {}).get("new_id") != 318:
        raise ContentError("本部署器只支持此次已批准的 ID 318 补丁。")
    if set(manifest.get("files", {})) != set(PAYLOAD_PATHS) or set(manifest.get("sources", {})) != set(PAYLOAD_PATHS):
        raise ContentError("补丁文件集合不符。")
    targets = {"server_object": share / "qingfeng/Object.dbf",
               "client_object": root / "swdol/Main/Object.dbf",
               "client_language": root / "swdol/Main/Language.DBF"}
    payloads = {}
    for key, relative in PAYLOAD_PATHS.items():
        entry = manifest["files"][key]
        source = (directory / relative).resolve()
        if entry.get("path") != relative or not source.is_relative_to(directory):
            raise ContentError("补丁源路径越界。")
        payloads[key] = read_bytes(source)
        if sha256(payloads[key]) != entry.get("sha256") or len(payloads[key]) != entry.get("bytes"):
            raise ContentError(f"补丁源文件变化：{relative}")
    before = {key: read_bytes(path) for key, path in targets.items()}
    after_hashes = {key: manifest["files"][key]["sha256"] for key in targets}
    if all(sha256(before[key]) == after_hashes[key] for key in targets):
        verification = verify_installed(before, manifest)
        result = {"status": "already_deployed_pending_manual_start", "verification": verification,
                  "files_replaced_this_run": [], "targets": {k: str(v) for k, v in targets.items()}}
        write_json(report_dir / "deployment.json", result)
        return result
    for key, blob in before.items():
        entry = manifest["sources"][key]
        if sha256(blob) != entry.get("sha256") or len(blob) != entry.get("bytes"):
            raise ContentError(f"目标版本已变化或存在部分更新：{targets[key]}；停止部署。")
    verify_patch(root, directory)
    preflight = None
    if remote_preflight:
        preflight = check_server(root, share, directory, Path("output/remote-server-20261029/snapshot.json"))
        if not preflight["remote_static_baseline_matches"]:
            raise ContentError("远端关键文件版本已变化，停止部署。")
    # No destination is changed before all source and target hashes have been checked.
    report = {"status": "backed_up_preparing", "user_confirmed_both_sides_stopped": True,
              "manual_start_by_user": True, "manifest_sha256": sha256(read_bytes(directory / "manifest.json")),
              "targets": {k: str(v) for k, v in targets.items()},
              "before_sha256": {k: sha256(v) for k, v in before.items()}, "after_sha256": after_hashes,
              "files_replaced": [], "rollback": {}, "remote_preflight": preflight}
    for key, data in before.items():
        backup = report_dir / "before" / PAYLOAD_PATHS[key]
        atomic_write(backup, data)
        if sha256(read_bytes(backup)) != sha256(data):
            raise ContentError("本次部署备份校验失败。")
    write_json(report_dir / "deployment.json", report)
    staged = {}
    token = uuid.uuid4().hex[:16]
    attempted = []
    try:
        for key in ORDER:
            staged[key] = stage_file(targets[key], payloads[key], token)
        report["status"] = "staged_committing"
        write_json(report_dir / "deployment.json", report)
        for key in ORDER:
            if sha256(read_bytes(targets[key])) != sha256(before[key]):
                raise ContentError(f"提交前目标发生变化：{targets[key]}")
            report["pending_replace"] = key
            write_json(report_dir / "deployment.json", report)
            attempted.append(key)
            os.replace(staged[key], targets[key])
            if sha256(read_bytes(targets[key])) != after_hashes[key]:
                raise ContentError(f"提交后目标校验失败：{targets[key]}")
            report["files_replaced"].append(key)
            write_json(report_dir / "deployment.json", report)
        installed = {key: read_bytes(path) for key, path in targets.items()}
        report["verification"] = verify_installed(installed, manifest)
        report.pop("pending_replace", None)
        report["status"] = "deployed_pending_manual_start"
        report["database_modified"] = False
        report["processes_started_or_stopped_by_tool"] = False
        write_json(report_dir / "deployment.json", report)
        return report
    except (Exception, KeyboardInterrupt) as exc:
        report["error"] = str(exc)
        report["status"] = "failed_recovering"
        for key in reversed(attempted):
            try:
                digest = sha256(read_bytes(targets[key]))
                if digest == sha256(before[key]):
                    report["rollback"][key] = "already_original"
                    continue
                if digest != after_hashes[key]:
                    raise ContentError("目标出现第三方更改，拒绝覆盖该更改。")
                recovery = stage_file(targets[key], before[key], token + "-restore")
                try:
                    os.replace(recovery, targets[key])
                finally:
                    recovery.unlink(missing_ok=True)
                if sha256(read_bytes(targets[key])) != sha256(before[key]):
                    raise ContentError("恢复后的哈希不符。")
                report["rollback"][key] = "restored"
            except Exception as restore_error:
                report["rollback"][key] = f"manual_recovery_required: {restore_error}"
        complete = all(not value.startswith("manual_recovery_required") for value in report["rollback"].values())
        report["status"] = "failed_rolled_back" if complete else "failed_manual_recovery_required"
        write_json(report_dir / "deployment.json", report)
        raise ContentError(f"部署失败，状态 {report['status']}，详情：{report_dir / 'deployment.json'}") from exc
    finally:
        for staging in staged.values():
            staging.unlink(missing_ok=True)


def main() -> int:
    """Apply only after the explicit flags record the user's deployment/stop confirmation."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--share", type=Path, required=True, help="明确指定与静态快照证据匹配的 UNC 共享根目录")
    parser.add_argument("--patch", type=Path, default=Path("output/patch-live-baseline-318"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--apply", action="store_true", required=True)
    parser.add_argument("--confirmed-stopped", action="store_true", required=True)
    args = parser.parse_args()
    share_text = str(args.share).rstrip("\\/")
    if not share_text.startswith("\\\\") or len(share_text[2:].split("\\")) != 2 or any(
            part in ("", ".", "..") for part in share_text[2:].split("\\")):
        parser.error("请通过 --share 明确指定 UNC 共享根目录；目标还必须通过静态快照与基线核对。")
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    try:
        result = deploy(args.root, args.share, args.patch, args.out or Path(f"output/deployments/{stamp}-item318"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ContentError, OSError) as exc:
        print(f"部署未完成：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
