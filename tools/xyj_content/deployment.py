"""Explicit, baseline-bound three-file deployment with one durable local rollback set."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import shutil
import struct
import tempfile
import uuid

from .binary import ContentError, read_bytes
from .catalog import Inputs, load_inputs
from .editor import export_editor_patch
from .files import atomic_write, output_path, read_json, sha256, write_json
from .patch import PAYLOAD_PATHS

BACKUP_PATHS = {**PAYLOAD_PATHS, 'settings': 'state/settings.json', 'draft': 'state/draft.json'}


def deployment_targets(root: Path, config: dict) -> dict:
    """Resolve only the saved deployment destination and the client's fixed Main pair."""
    from .settings import input_path, resolve_paths
    if not config.get('server_target'):
        raise ContentError('请在设置中填写并校验“服务端 Object.dbf 覆盖目标”。')
    client = input_path(root, config['client_root'])
    targets = dict(zip(PAYLOAD_PATHS, (input_path(root, config['server_target']),
                                     (client / 'Main/Object.dbf').resolve(),
                                     (client / 'Main/Language.DBF').resolve())))
    if targets['server_object'].name.lower() != 'object.dbf':
        raise ContentError('服务端覆盖目标必须是完整的 Object.dbf 文件路径。')
    source = resolve_paths(root, config)[0]
    if source.client != targets['client_object'] or source.language != targets['client_language']:
        raise ContentError('客户端单独指定路径须与客户端根目录的 Main/Object.dbf 和 Main/Language.DBF 一致。')
    paths = list(targets.values())
    managed = [output_path(root, Path('output/editor') / name) for name in
               ('backups', 'pending', 'workspaces', 'applied-snapshots', 'patches')]
    for index, path in enumerate(paths):
        if any(path.is_relative_to(directory) for directory in managed):
            raise ContentError('覆盖目标不能位于编辑器备份、草稿、历史补丁或临时目录中。')
        if not path.is_file():
            raise ContentError(f'覆盖目标文件不存在或不可访问：{path}')
        for other in paths[:index]:
            if path == other or os.path.samefile(path, other):
                raise ContentError('双端覆盖目标不能指向同一个文件。')
    return targets


@contextmanager
def deployment_lock(root: Path):
    """Serialize deployments and crash recovery across local editor processes."""
    path = output_path(root, Path('output/editor/backups/.lock'))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ContentError('另一编辑器正在覆盖或恢复文件，请稍后重试。') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _remove_local(root: Path, path: Path):
    allowed = output_path(root, Path('output/editor/backups'))
    resolved = path.resolve()
    if resolved.parent != allowed or not (path.name == 'previous' or path.name.startswith('.next-')):
        raise ContentError('备份清理路径无效。')
    if path.exists():
        shutil.rmtree(resolved)


def _current(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _stage_file(path: Path, blob: bytes, staged: Path | None = None) -> Path:
    """Flush a sibling file before replacement; supports local paths and authenticated UNC shares."""
    created = False
    try:
        stream = staged.open('xb') if staged is not None else tempfile.NamedTemporaryFile(
            dir=path.parent, prefix='.xyj-apply-', delete=False)
        created = True
        with stream:
            staged = Path(stream.name)
            stream.write(blob)
            stream.flush()
            os.fsync(stream.fileno())
        if read_bytes(staged) != blob:
            raise ContentError(f'写入暂存文件后校验失败：{path}')
        return staged
    except BaseException:
        if created and staged is not None:
            staged.unlink(missing_ok=True)
        raise


def _replace_file(staged: Path, target: Path):
    os.replace(staged, target)


def _journal(root: Path):
    return output_path(root, Path('output/editor/backups/latest'))


def _validate_journal(root: Path, folder: Path) -> dict:
    manifest = read_json(folder / 'manifest.json')
    if manifest.get('format') != 'xyj-auto-deploy/v1' or manifest.get('root') != str(root.resolve()):
        raise ContentError('自动覆盖备份记录无效，请保留备份并检查。')
    entries = manifest.get('entries', [])
    if [entry.get('role') for entry in entries] != [*PAYLOAD_PATHS, 'settings', 'draft']:
        raise ContentError('自动覆盖备份文件清单无效。')
    for index, entry in enumerate(entries):
        target = Path(entry['target'])
        if not target.is_absolute() or entry['backup'] != BACKUP_PATHS[entry['role']]:
            raise ContentError('自动覆盖备份路径无效。')
        if entry['role'] == 'settings' and target != root.resolve() / 'output/editor/settings.json':
            raise ContentError('设置恢复位置无效。')
        if entry['role'] == 'draft' and (not target.is_relative_to(root.resolve() / 'output/editor/workspaces') or target.name != 'draft.json'):
            raise ContentError('草稿恢复位置无效。')
        if entry['before'] is not None and sha256(read_bytes(folder / entry['backup'])) != entry['before']:
            raise ContentError('覆盖前备份校验失败，已停止自动恢复。')
    return manifest


def _rollback(root: Path, folder: Path, manifest: dict):
    failures = []
    for entry in reversed(manifest['entries']):
        target = Path(entry['target'])
        try:
            current = _current(target)
            digest = sha256(current) if current is not None else None
            if digest == entry['before']:
                continue
            if digest != entry['after']:
                raise ContentError('文件又被外部修改，未覆盖该外部修改')
            if entry['before'] is None:
                # Only editor state may have been newly created; game files must already exist.
                if entry['role'] not in ('draft', 'settings'):
                    raise ContentError('缺少游戏文件的原始备份')
                target.unlink()
            else:
                staged = _stage_file(target, read_bytes(folder / entry['backup']))
                try:
                    _replace_file(staged, target)
                finally:
                    staged.unlink(missing_ok=True)
            restored = _current(target)
            if (sha256(restored) if restored is not None else None) != entry['before']:
                raise ContentError('回滚后核验失败')
        except (ContentError, OSError) as exc:
            failures.append(f'{target}：{exc}')
    manifest['phase'] = 'rollback_required' if failures else 'rolled_back'
    manifest['recovery_errors'] = failures
    write_json(folder / 'manifest.json', manifest)
    if failures:
        raise ContentError('覆盖未完成，以下文件无法自动恢复；备份已保留：' + '；'.join(failures))


def _recover_locked(root: Path):
    folder = _journal(root)
    previous = folder.parent / 'previous'
    if not folder.exists() and previous.exists():
        os.rename(previous, folder)
    if folder.exists():
        manifest = _validate_journal(root, folder)
        if manifest['phase'] not in ('committed', 'rolled_back'):
            _rollback(root, folder, manifest)
        # Remove only the recorded sibling staging files after a crash.
        for entry in manifest['entries']:
            staged = Path(entry['staged']) if entry.get('staged') else None
            if staged is not None:
                if staged.parent != Path(entry['target']).parent or not staged.name.startswith('.xyj-apply-'):
                    raise ContentError('恢复记录中的暂存文件位置无效。')
                staged.unlink(missing_ok=True)
        _remove_local(root, previous)
    for unused in folder.parent.glob('.next-*') if folder.parent.exists() else ():
        _remove_local(root, unused)


def recover_deployment(root: Path):
    """Recover an interrupted write before settings fingerprints or drafts are loaded on startup."""
    folder = output_path(root, Path('output/editor/backups'))
    if folder.exists():
        with deployment_lock(root):
            _recover_locked(root)


def _snapshot_for_items(controller, loaded, directory: Path) -> Path:
    """Rebind an immutable NPC projection only after proving all non-item records unchanged."""
    from .creatures import CreatureCatalog
    old = controller.workspace
    nonitems = lambda table: tuple(record for record in table.records if struct.unpack_from('<H', record, 8)[0] != 2)
    if nonitems(old.loaded.server) != nonitems(loaded.server):
        raise ContentError('NPC或脚本模板发生变化，不能沿用当前快照。')
    old.creatures()  # Validate the existing projection before deriving another binding.
    manifest = read_json(old.npc_directory / 'manifest.json')
    raw = read_bytes(old.npc_directory / manifest['catalog_file'])
    directory.mkdir(parents=True, exist_ok=False)
    atomic_write(directory / manifest['catalog_file'], raw)
    manifest['server_object_sha256'] = sha256(loaded.blobs[0])
    manifest['source'] = {**manifest['source'], 'item_patch_rebase': {
        'previous_server_sha256': old.hashes['server_object'], 'all_nonitem_records_unchanged': True,
        'catalog_sha256_unchanged': manifest['catalog_sha256']}}
    write_json(directory / 'manifest.json', manifest)
    CreatureCatalog(controller.root, loaded, client=old.client, directory=directory)
    return directory


def apply_workspace(controller, revision: int) -> dict:
    """Back up and replace the explicit paired targets, committing editor state or rolling back."""
    from .settings import portable_path
    from .resources import clear_resource_caches
    root = controller.root
    state = controller.workspace
    if state is None or type(revision) is not int or revision != state.revision:
        raise ContentError('导出版本已过期，请刷新。')
    targets = deployment_targets(root, controller.config)
    for key, original in zip(PAYLOAD_PATHS, state.loaded.blobs):
        if read_bytes(targets[key]) != original:
            raise ContentError(f'覆盖目标与编辑基线不一致，请重新校验数据源：{targets[key]}')
    with deployment_lock(root):
        _recover_locked(root)
        pending = output_path(root, Path('output/editor/pending'))
        pending.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=pending, prefix='apply-') as temporary:
            exported = export_editor_patch(Path(temporary), state.loaded, state.edits)
            package = Path(exported['path'])
            blobs = {key: read_bytes(package / relative) for key, relative in PAYLOAD_PATHS.items()}
            loaded = load_inputs(Inputs(*(package / relative for relative in PAYLOAD_PATHS.values())))
            snapshot = output_path(root, Path('output/editor/applied-snapshots') / uuid.uuid4().hex)
            _snapshot_for_items(controller, loaded, snapshot)
        config = {**controller.config, 'server_object': portable_path(root, targets['server_object']),
                  'client_object': '', 'language': '', 'npc_snapshot': portable_path(root, snapshot)}
        hashes = {key: sha256(blob) for key, blob in blobs.items()}
        identity = json.dumps({'hashes': hashes, 'paths': [str(path.resolve()) for path in targets.values()]}, sort_keys=True)
        draft = output_path(root, Path('output/editor/workspaces') / sha256(identity.encode('utf-8')) / 'draft.json')
        if draft.exists() and read_json(draft).get('edits'):
            raise ContentError('覆盖后的基线已有未完成草稿，已停止以保留它；请先核对该工作区。')
        old_doc = controller.document or {}
        excluded = {str(path.resolve()) for path in (state.loaded.paths.server, state.loaded.paths.client,
                    state.loaded.paths.language, state.npc_directory / 'manifest.json')}
        excluded.update(str(path.resolve()) for path in targets.values())
        old_manifest = read_json(state.npc_directory / 'manifest.json')
        excluded.add(str((state.npc_directory / old_manifest['catalog_file']).resolve()))
        fingerprints = [entry for entry in old_doc.get('validated_files', [])
                        if str((root / entry['path']).resolve()) not in excluded]
        fingerprints += [{'path': portable_path(root, path), 'sha256': hashes[key]} for key, path in targets.items()]
        new_manifest = read_json(snapshot / 'manifest.json')
        fingerprints += [{'path': portable_path(root, p), 'sha256': sha256(read_bytes(p))}
                         for p in (snapshot / 'manifest.json', snapshot / new_manifest['catalog_file'])]
        document = {**old_doc, 'format': 'xyj-settings/v1', 'config': config,
                    'validated_files': fingerprints, 'saved_at': datetime.now(timezone.utc).isoformat()}
        new_draft = {'format': 'xyj-editor-draft/v1', 'sources': hashes, 'revision': state.revision + 1, 'edits': []}
        payloads = [(key, targets[key], blobs[key]) for key in PAYLOAD_PATHS]
        payloads += [('settings', controller.path, json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')),
                     ('draft', draft, json.dumps(new_draft, ensure_ascii=False, indent=2).encode('utf-8'))]
        folder = _journal(root)
        next_backup = folder.parent / ('.next-' + uuid.uuid4().hex)
        next_backup.mkdir(parents=True)
        entries = []
        for index, (role, target, blob) in enumerate(payloads):
            original = _current(target)
            if role in PAYLOAD_PATHS and original != state.loaded.blobs[list(PAYLOAD_PATHS).index(role)]:
                raise ContentError(f'覆盖前文件发生变化：{target}')
            entry = {'role': role, 'target': str(target), 'before': sha256(original) if original is not None else None,
                     'after': sha256(blob), 'backup': BACKUP_PATHS[role],
                     'staged': str(target.parent / ('.xyj-apply-' + uuid.uuid4().hex))}
            if original is not None:
                atomic_write(next_backup / entry['backup'], original)
                if read_bytes(next_backup / entry['backup']) != original:
                    raise ContentError('本地备份校验失败，未覆盖目标文件。')
            entries.append(entry)
        manifest = {'format': 'xyj-auto-deploy/v1', 'root': str(root.resolve()), 'phase': 'prepared',
                    'created_at': datetime.now(timezone.utc).isoformat(), 'entries': entries,
                    'changes': exported['changes'], 'verification': exported['verification']}
        write_json(next_backup / 'manifest.json', manifest)
        _validate_journal(root, next_backup)
        if folder.exists():
            os.rename(folder, folder.parent / 'previous')
        os.rename(next_backup, folder)
        candidate = None
        try:
            # Prepare every replacement before changing any target.
            for entry, (_, target, blob) in zip(entries, payloads):
                target.parent.mkdir(parents=True, exist_ok=True)
                _stage_file(target, blob, Path(entry['staged']))
            manifest['phase'] = 'applying'
            write_json(folder / 'manifest.json', manifest)
            for entry in entries:
                target = Path(entry['target'])
                original = _current(target)
                if (sha256(original) if original is not None else None) != entry['before']:
                    raise ContentError(f'写入前文件被外部修改：{target}')
                _replace_file(Path(entry['staged']), target)
                if sha256(read_bytes(target)) != entry['after']:
                    raise ContentError(f'覆盖后文件核验失败：{target}')
            candidate = controller.factory(config)
            candidate.creatures()
            manifest['phase'] = 'committed'
            write_json(folder / 'manifest.json', manifest)
        except Exception as exc:
            _rollback(root, folder, manifest)
            try:
                _remove_local(root, folder.parent / 'previous')
            except (OSError, ContentError):
                pass  # Retry startup cleanup; the current rollback set is verified.
            raise ContentError(f'覆盖失败，已恢复覆盖前文件，草稿保留：{exc}') from exc
        finally:
            for entry in entries:
                if entry.get('staged'):
                    try:
                        Path(entry['staged']).unlink(missing_ok=True)
                    except OSError:
                        pass  # Durable journal retains exact names for startup cleanup.
        controller.workspace, controller.config, controller.document = candidate, config, document
        candidate.token = controller.token
        controller.generation += 1
        controller.pending = None
        controller.setup_error = ''
        clear_resource_caches()
        # Rotation errors must not turn a committed write into an apparent failed deployment.
        cleanup_warning = ''
        try:
            _remove_local(root, folder.parent / 'previous')
        except (OSError, ContentError):
            cleanup_warning = '旧备份暂未清理，将在下次启动时重试。'
        return {'status': 'applied', 'backup_path': str(folder), 'targets': {k: str(v) for k, v in targets.items()},
                'verification': {**exported['verification'], 'target_files_verified': True},
                'operations': len(exported['changes']), 'workspace_generation': controller.generation,
                'cleanup_warning': cleanup_warning}
