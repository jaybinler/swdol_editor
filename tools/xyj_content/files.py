"""Confine generated artifacts to workspace/output and write individual files atomically."""

from pathlib import Path
import hashlib
import json
import os
import tempfile
import time

from .binary import ContentError


def sha256(data: bytes) -> str:
    """Return a content digest without storing or printing file contents."""
    return hashlib.sha256(data).hexdigest()


def retry_file_operation(operation):
    """Retry only failed Windows access/sharing operations briefly; never use this helper for SQL or network requests."""
    for attempt in range(5):
        try:
            return operation()
        except OSError as exc:
            if getattr(exc,'winerror',None) not in (5,32,33) or attempt==4:
                raise
            time.sleep(.05*(2**attempt))


def move_path(source: Path,target: Path,*,replace=False):
    """Commit one caller-validated path with bounded Windows sharing retries; preserve rename/replace semantics."""
    return retry_file_operation(lambda:(os.replace if replace else os.rename)(source,target))


def output_path(root: Path, requested: Path) -> Path:
    """Require an output below the real workspace/output, resolving links and junctions."""
    root = root.resolve()
    allowed = root / "output"
    if allowed.exists() and allowed.resolve() != allowed:
        raise ContentError("output 不能是指向其他位置的链接或目录联接。")
    candidate = (root / requested).resolve() if not requested.is_absolute() else requested.resolve()
    if candidate == allowed or not candidate.is_relative_to(allowed):
        raise ContentError("输出必须位于项目 output 的子目录内，禁止写入游戏原始目录。")
    return candidate


def atomic_write(path: Path, data: bytes) -> None:
    """Write a generated file by same-directory replacement; callers must confine its path."""
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".xyj-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        move_path(temporary,path,replace=True)
    except OSError as exc:
        raise ContentError(f"无法写入研究输出：{path}（{exc.strerror}）") from exc
    finally:
        if temporary is not None and temporary.exists():
            retry_file_operation(lambda:temporary.unlink(missing_ok=True))


def write_json(path: Path, value: object) -> None:
    """Atomically write an explicit UTF-8 JSON research artifact."""
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def read_json(path: Path) -> dict:
    """Load bounded JSON and require an object at the root."""
    from .binary import read_bytes
    try:
        value = json.loads(read_bytes(path).decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContentError(f"JSON 格式无效：{path}") from exc
    if not isinstance(value, dict):
        raise ContentError("JSON 根节点必须是对象。")
    return value
