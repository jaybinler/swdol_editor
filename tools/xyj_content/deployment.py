"""Baseline-bound paired item and acquisition-script deployment with one durable rollback set."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re
import shutil
import struct
import tempfile
import uuid

from .binary import ContentError, read_bytes
from .catalog import Inputs, load_inputs
from .editor import export_editor_patch
from .files import atomic_write, output_path, read_json, sha256, write_json,move_path
from .patch import PAYLOAD_PATHS

BACKUP_PATHS = {**PAYLOAD_PATHS, 'settings': 'state/settings.json', 'draft': 'state/draft.json'}
BACKUP_PATHS['actor'] = 'client/Act/all_char.act'
BACKUP_PATHS['source_draft'] = 'state/source-draft.json'


def _backup_relative(role: str) -> str:
    from .acquisition import SCRIPT_NAME
    if role in BACKUP_PATHS:
        return BACKUP_PATHS[role]
    if role.startswith('model:'):
        from .model_compiler import role_relative
        return role_relative(role)
    if role.startswith('script:') and SCRIPT_NAME.fullmatch(role[7:]):
        return 'server/qingfeng/ASM/' + role[7:]
    raise ContentError('备份文件角色无效。')


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


def workspace_inputs(controller,*,creature=False,allow_package=False):
    """Reject a stale process before publication and freeze inputs whose concurrent changes must be preserved."""
    state=controller.workspace
    if controller.document is not None and (not controller.path.exists() or read_json(controller.path)!=controller.document):
        raise ContentError('另一编辑器已更新数据源，请重启当前编辑器后再发布。')
    path=state.creature_draft_path if creature else state.draft_path
    edits=state.creature_edits if creature else state.edits
    revision=state.creature_revision if creature else state.revision
    if path.exists():
        current=read_json(path)
        if current.get('revision')!=revision or current.get('edits')!=edits and not (allow_package and not current.get('edits')):
            raise ContentError('草稿已由另一编辑器修改，请重启当前编辑器并核对草稿。')
    elif edits and not allow_package:
        legacy=controller.root/'output/editor/draft.json'
        if creature or not legacy.exists() or read_json(legacy).get('edits')!=edits:
            raise ContentError('草稿文件已变化，请重新读取并暂存。')
    return {p:sha256(raw) if (raw:=_current(p)) is not None else None for p in (controller.path,path)}


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
    move_path(staged,target,replace=True)


def _journal(root: Path):
    return output_path(root, Path('output/editor/backups/latest'))


def _validate_journal(root: Path, folder: Path) -> dict:
    manifest = read_json(folder / 'manifest.json')
    if manifest.get('format') != 'xyj-auto-deploy/v1' or manifest.get('root') != str(root.resolve()):
        raise ContentError('自动覆盖备份记录无效，请保留备份并检查。')
    if 'publication' in manifest:
        publication=manifest['publication']
        if (not isinstance(publication,dict) or not re.fullmatch('[0-9a-f]{64}',publication.get('key','')) or
                not re.fullmatch('[0-9a-f]{64}',publication.get('manifest_sha256','')) or
                Path(publication['staged']).parent != root.resolve()/'output/editor/patches' or
                not re.fullmatch(r'\.next-[0-9a-f]{32}',Path(publication['staged']).name)):
            raise ContentError('发布回执或补丁暂存索引无效。')
        if publication.get('request_id') and str(uuid.UUID(publication['request_id']))!=publication['request_id']:
            raise ContentError('发布请求回执索引无效。')
    entries = manifest.get('entries', [])
    if not isinstance(entries,list) or any(not isinstance(entry,dict) for entry in entries):
        raise ContentError('自动覆盖备份文件清单无效。')
    roles = [entry.get('role', '') for entry in entries]
    creature = manifest.get('kind') == 'creature'
    partial = creature and manifest.get('state_pending') is True
    scripts = roles[3:] if partial else roles[3:-2]
    if (roles[:3] != list(PAYLOAD_PATHS) or not partial and roles[-2:] != ['settings', 'draft'] or
            len(roles) != len(set(roles)) or len(scripts) > (258 if creature else 65) or
            any(role!='source_draft' and (role != 'actor' and not role.startswith('model:') if creature else not role.startswith('script:')) for role in scripts)):
        raise ContentError('自动覆盖备份文件清单无效。')
    for index, entry in enumerate(entries):
        target = Path(entry['target'])
        if not target.is_absolute() or entry['backup'] != _backup_relative(entry['role']):
            raise ContentError('自动覆盖备份路径无效。')
        if entry['role'].startswith('script:'):
            expected = Path(entries[0]['target']).parent / 'ASM' / entry['role'][7:]
            if target.resolve() != expected.resolve():
                raise ContentError('脚本恢复位置必须在所选服务端的 ASM 目录。')
        if entry['role'] == 'settings' and target != root.resolve() / 'output/editor/settings.json':
            raise ContentError('设置恢复位置无效。')
        if entry['role'] == 'actor' and target.resolve() != (Path(entries[1]['target']).parent.parent/'Act/all_char.act').resolve():
            raise ContentError('角色动作恢复位置无效。')
        if entry['role'].startswith('model:'):
            from .model_compiler import role_relative
            client=Path(entries[1]['target']).parent.parent.resolve()
            expected=client/role_relative(entry['role'])[7:]
            if target.resolve()!=expected.resolve() or not target.resolve().is_relative_to(client):
                raise ContentError('模型恢复位置必须位于所选客户端目录。')
            if entry['before'] is None and expected.suffix.upper() not in ('.PLY','.TEX'):
                raise ContentError('索引文件缺少原始备份。')
        if entry['role'] in ('draft','source_draft') and (not target.is_relative_to(root.resolve() / 'output/editor/workspaces') or target.name != ('creatures.json' if creature else 'draft.json')):
            raise ContentError('草稿恢复位置无效。')
        if entry['before'] is not None and sha256(read_bytes(folder / entry['backup'])) != entry['before']:
            raise ContentError('覆盖前备份校验失败，已停止自动恢复。')
        if entry.get('restore') and (entry['role']!='settings' or entry['restore']!='state/restore-settings.json' or
                                    sha256(read_bytes(folder/entry['restore']))!=entry.get('restore_sha256')):
            raise ContentError('恢复配置的迁移副本无效。')
        for source in entry.get('inputs',[]):
            if (entry['role']!='settings' or output_path(root,Path(source['target']))!=Path(source['target']).absolute() or
                    not re.fullmatch(r'inputs/[0-9a-f]{16}/data',source['backup']) or
                    sha256(read_bytes(folder/source['backup']))!=source['sha256']):
                raise ContentError('恢复输入记录或校验无效。')
    return manifest


def _rollback(root: Path, folder: Path, manifest: dict, query=None):
    if (folder/'npc-journal.json').exists() and query is None:
        raise ContentError('怪物发布尚未完成；请停止相关游戏服务后，使用“恢复上次发布”恢复数据库和文件。')
    failures = []
    committed=manifest['phase']=='committed'
    for entry in reversed(manifest['entries']):
        target = Path(entry['target'])
        try:
            current = _current(target)
            digest = sha256(current) if current is not None else None
            relocated=committed and bool(entry.get('restore'))
            if committed and entry.get('inputs'):
                from .publication import restore_protected_inputs
                restore_protected_inputs(root,folder,entry['inputs'])
            restored_hash=entry['restore_sha256'] if relocated else entry['before']
            if digest == restored_hash:
                continue
            if digest not in (entry['before'],entry['after']):
                raise ContentError('文件又被外部修改，未覆盖该外部修改')
            if entry['before'] is None:
                if entry['role'] not in ('draft', 'source_draft', 'settings') and not entry['role'].startswith('model:'):
                    raise ContentError('缺少游戏文件的原始备份')
                target.unlink()
            else:
                staged = _stage_file(target, read_bytes(folder / (entry['restore'] if relocated else entry['backup'])))
                try:
                    _replace_file(staged, target)
                finally:
                    staged.unlink(missing_ok=True)
            restored = _current(target)
            if (sha256(restored) if restored is not None else None) != restored_hash:
                raise ContentError('回滚后核验失败')
        except (ContentError, OSError) as exc:
            failures.append(f'{target}：{exc}')
    if (folder/'npc-journal.json').exists():
        try:
            from .creature_database import rollback_points
            rollback_points(query,folder/'npc-journal.json')
        except Exception as exc:
            failures.append('NPC 数据库：'+str(exc))
    manifest['phase'] = 'rollback_required' if failures else 'rolled_back'
    manifest['recovery_errors'] = failures
    write_json(folder / 'manifest.json', manifest)
    if failures:
        raise ContentError('覆盖未完成，以下文件无法自动恢复；备份已保留：' + '；'.join(failures))
    if manifest.get('publication'):
        from .publication import discard
        discard(root,manifest['publication'])


def _restore_previous(root: Path, folder: Path):
    """After a fully verified rollback, restore the preceding successful backup instead of discarding it."""
    from .publication import remove_tree
    previous=folder.parent/'previous'
    if previous.exists():
        _validate_journal(root,previous)
        remove_tree(root,folder,[folder.parent])
        move_path(previous,folder)


def _recover_locked(root: Path):
    folder = _journal(root)
    previous = folder.parent / 'previous'
    if not folder.exists() and previous.exists():
        move_path(previous, folder)
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
        if manifest['phase']=='committed' and manifest.get('publication'):
            from .publication import finish
            finish(root,manifest['publication'])
        if manifest['phase']=='rolled_back' and previous.exists():
            _restore_previous(root,folder)
        _remove_local(root, previous)
    for unused in folder.parent.glob('.next-*') if folder.parent.exists() else ():
        _remove_local(root, unused)


def recover_deployment(root: Path):
    """Recover an interrupted write before settings fingerprints or drafts are loaded on startup."""
    folder = output_path(root, Path('output/editor/backups'))
    if folder.exists():
        with deployment_lock(root):
            _recover_locked(root)


def _snapshot_for_items(controller, loaded, directory: Path, *, script_directory: Path | None = None) -> Path:
    """Rebind after proving NPC changes match the planned drops; refresh changed script evidence."""
    from .creatures import CreatureCatalog
    old = controller.workspace
    nonitems = lambda table: tuple(record for record in table.records if struct.unpack_from('<H', record, 8)[0] != 2)
    from .acquisition import apply_drop_edits
    expected, drop_changes = apply_drop_edits(old.loaded, old.loaded.server, old.edits)
    if nonitems(expected) != nonitems(loaded.server):
        raise ContentError('NPC模板含获取配置之外的变化，不能沿用当前快照。')
    CreatureCatalog(controller.root, old.loaded, client=old.client, directory=old.npc_directory)
    manifest = read_json(old.npc_directory / 'manifest.json')
    raw = read_bytes(old.npc_directory / manifest['catalog_file'])
    if script_directory is not None and manifest['format'] == 'xyj-npc-snapshot/v3':
        from .plot_maps import build_script_index
        data = json.loads(raw)
        data['script_index'], _ = build_script_index(data['instances'], loaded.server.records, script_directory)
        raw = json.dumps(data, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        manifest['catalog_sha256'] = sha256(raw)
        manifest['catalog_file'] = f'catalog-{sha256(raw)}.json'
    directory.mkdir(parents=True, exist_ok=False)
    atomic_write(directory / manifest['catalog_file'], raw)
    manifest['server_object_sha256'] = sha256(loaded.blobs[0])
    manifest['source'] = {**manifest['source'], 'item_patch_rebase': {
        'previous_server_sha256': old.hashes['server_object'], 'all_nonitem_records_unchanged': not drop_changes,
        'npc_inventory_updates': drop_changes,
        'script_index_refreshed': script_directory is not None and manifest['format'] == 'xyj-npc-snapshot/v3'}}
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
    state.load_creature_draft()
    if state.creature_edits:
        raise ContentError('存在怪物草稿，先导出并处理怪物草稿后再发布道具，避免基线切换隐藏草稿。')
    targets = deployment_targets(root, controller.config)
    for key, original in zip(PAYLOAD_PATHS, state.loaded.blobs):
        if read_bytes(targets[key]) != original:
            raise ContentError(f'覆盖目标与编辑基线不一致，请重新校验数据源：{targets[key]}')
    with deployment_lock(root):
        _recover_locked(root)
        inputs=workspace_inputs(controller)
        from .publication import prepare as prepare_publication, finish as finish_publication, check_legacy
        check_legacy(root)
        from .publication import restore_inputs
        restore_document=restore_inputs(root,controller.document)
        pending = output_path(root, Path('output/editor/pending'))
        pending.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=pending, prefix='apply-',ignore_cleanup_errors=True) as temporary:
            from .acquisition import has_acquisition, NATIVE_SHA256
            acquisition = state.acquisition() if has_acquisition(state.edits) else None
            exported = export_editor_patch(Path(temporary), state.loaded, state.edits, acquisition=acquisition)
            package = Path(exported['path'])
            publication = prepare_publication(root,package)
            blobs = {key: read_bytes(package / relative) for key, relative in PAYLOAD_PATHS.items()}
            script_meta = read_json(package / 'manifest.json').get('scripts', {})
            script_directory = None
            if script_meta:
                actual_directory = targets['server_object'].parent
                if sha256(read_bytes(actual_directory / 'NewLocalServer.exe')) != NATIVE_SHA256:
                    raise ContentError('覆盖目标服务端程序版本未核验。')
                # A refreshed map index must describe the target's complete script set.
                for filename, original in acquisition.scripts.items():
                    if read_bytes(actual_directory / 'ASM' / filename) != original:
                        raise ContentError(f'目标脚本与读取基线不一致：{filename}')
                script_directory = Path(temporary) / 'script-input'
                atomic_write(script_directory / 'NewLocalServer.exe', read_bytes(actual_directory / 'NewLocalServer.exe'))
                for filename, original in acquisition.scripts.items():
                    atomic_write(script_directory / 'ASM' / filename, original)
                for filename, entry in sorted(script_meta.items()):
                    role = 'script:' + filename
                    targets[role] = (actual_directory / 'ASM' / filename).resolve()
                    blobs[role] = read_bytes(package / entry['path'])
                    atomic_write(script_directory / 'ASM' / filename, blobs[role])
            loaded = load_inputs(Inputs(*(package / relative for relative in PAYLOAD_PATHS.values())))
            snapshot = output_path(root, Path('output/editor/applied-snapshots') / uuid.uuid4().hex)
            _snapshot_for_items(controller, loaded, snapshot, script_directory=script_directory)
        config = {**controller.config, 'server_object': portable_path(root, targets['server_object']),
                  'client_object': '', 'language': '', 'npc_snapshot': portable_path(root, snapshot)}
        if script_meta:
            config['map_dir'] = portable_path(root, targets['server_object'].parent)
        hashes = {key: sha256(blobs[key]) for key in PAYLOAD_PATHS}
        identity = json.dumps({'hashes': hashes, 'paths': [str(targets[key].resolve()) for key in PAYLOAD_PATHS]}, sort_keys=True)
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
        fingerprints += [{'path': portable_path(root, path), 'sha256': sha256(blobs[key])} for key, path in targets.items()]
        if script_meta:
            fingerprints += [{'path': portable_path(root, targets['server_object'].parent / 'NewLocalServer.exe'),
                              'sha256': NATIVE_SHA256}]
        new_manifest = read_json(snapshot / 'manifest.json')
        fingerprints += [{'path': portable_path(root, p), 'sha256': sha256(read_bytes(p))}
                         for p in (snapshot / 'manifest.json', snapshot / new_manifest['catalog_file'])]
        document = {**old_doc, 'format': 'xyj-settings/v1', 'config': config,
                    'validated_files': fingerprints, 'saved_at': datetime.now(timezone.utc).isoformat()}
        new_draft = {'format': 'xyj-editor-draft/v1', 'sources': hashes, 'revision': state.revision + 1, 'edits': []}
        payloads = [(key, targets[key], blobs[key]) for key in targets]
        if state.draft_path!=draft:
            source_draft={'format':'xyj-editor-draft/v1','sources':state.hashes,'revision':state.revision+1,'edits':[]}
            payloads.append(('source_draft',state.draft_path,json.dumps(source_draft,ensure_ascii=False,indent=2).encode('utf-8')))
        payloads += [('settings', controller.path, json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')),
                     ('draft', draft, json.dumps(new_draft, ensure_ascii=False, indent=2).encode('utf-8'))]
        folder = _journal(root)
        next_backup = folder.parent / ('.next-' + uuid.uuid4().hex)
        next_backup.mkdir(parents=True)
        entries = []
        for index, (role, target, blob) in enumerate(payloads):
            original = _current(target)
            if target in inputs and (sha256(original) if original is not None else None)!=inputs[target]:
                raise ContentError('发布准备期间设置或草稿被修改，未覆盖这些输入。')
            if role in PAYLOAD_PATHS and original != state.loaded.blobs[list(PAYLOAD_PATHS).index(role)]:
                raise ContentError(f'覆盖前文件发生变化：{target}')
            if role.startswith('script:') and (original is None or sha256(original) != script_meta[role[7:]]['source_sha256']):
                raise ContentError(f'覆盖前脚本发生变化：{target}')
            entry = {'role': role, 'target': str(target), 'before': sha256(original) if original is not None else None,
                     'after': sha256(blob), 'backup': _backup_relative(role),
                     'staged': str(target.parent / ('.xyj-apply-' + uuid.uuid4().hex))}
            if original is not None:
                atomic_write(next_backup / entry['backup'], original)
                if read_bytes(next_backup / entry['backup']) != original:
                    raise ContentError('本地备份校验失败，未覆盖目标文件。')
            if role=='settings' and restore_document is not None:
                entry.update(restore='state/restore-settings.json',restore_sha256=sha256(restore_document))
                atomic_write(next_backup/entry['restore'],restore_document)
            if role=='settings':
                from .publication import protect_inputs
                entry['inputs']=protect_inputs(root,next_backup,json.loads(restore_document) if restore_document else controller.document)
            entries.append(entry)
        manifest = {'format': 'xyj-auto-deploy/v1', 'root': str(root.resolve()), 'phase': 'prepared',
                    'created_at': datetime.now(timezone.utc).isoformat(), 'entries': entries,
                    'changes': exported['changes'], 'verification': exported['verification'], 'publication': publication}
        write_json(next_backup / 'manifest.json', manifest)
        _validate_journal(root, next_backup)
        if folder.exists():
            move_path(folder, folder.parent / 'previous')
        move_path(next_backup, folder)
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
                _restore_previous(root, folder)
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
            cleanup_warning = finish_publication(root,publication)
            _remove_local(root, folder.parent / 'previous')
        except (OSError, ContentError):
            cleanup_warning = '发布已成功；旧备份或补丁暂未清理，将在下次启动时重试。'
        return {'status': 'applied', 'backup_path': str(folder), 'targets': {k: str(v) for k, v in targets.items()},
                'verification': {**exported['verification'], 'target_files_verified': True},
                'operations': len(exported['changes']), 'workspace_generation': controller.generation,
                'cleanup_warning': cleanup_warning}
