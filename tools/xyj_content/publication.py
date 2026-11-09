"""One global published package, bounded artifact cleanup, and lightweight replay receipts."""

from pathlib import Path
import json
import os
import re
import shutil
import stat
import uuid

from .binary import ContentError, read_bytes
from .files import output_path, read_json, write_json, sha256,move_path

STAMP = re.compile(r'\d{8}-\d{6}-[0-9a-f]{8}')


def _no_links(path):
    for directory,dirs,files in os.walk(path,followlinks=False):
        for entry in [Path(directory),*(Path(directory)/n for n in dirs+files)]:
            info=entry.lstat()
            if entry.is_symlink() or getattr(info,'st_file_attributes',0)&stat.FILE_ATTRIBUTE_REPARSE_POINT or not entry.resolve().is_relative_to(path):
                raise ContentError('历史目录包含链接或目录联接，未清理。')


def remove_tree(root, path, parents):
    """Delete a verified direct child only; reject every junction or link before walking its contents."""
    path = Path(path)
    expected = output_path(root, path)
    allowed = {output_path(root, Path(p)) for p in parents}
    if expected != path.absolute() or expected.parent not in allowed:
        raise ContentError('历史清理路径越界，已停止。')
    if not path.exists():
        return
    _no_links(expected)
    shutil.rmtree(expected)


def package_key(package):
    """Identify the reviewed export, preserving its intent timestamp while ignoring JSON whitespace."""
    manifest = read_json(Path(package)/'manifest.json')
    return sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False).encode('utf-8'))


def assert_unpublished(root,package):
    """Reject original packages already committed by either publisher, even before re-exporting from a new snapshot."""
    key=package_key(package)
    receipt=output_path(root,Path('output/editor/publications/receipts')/(key+'.json'))
    digest=sha256(read_bytes(Path(package)/'manifest.json'))
    legacy_receipt=output_path(root,Path('output/editor/publications/legacy')/(digest+'.json'))
    committed=(receipt.exists() and read_json(receipt).get('status')=='committed') or legacy_receipt.exists()
    latest=output_path(root,Path('output/editor/backups/latest/manifest.json'))
    if latest.exists():
        metadata=read_json(latest)
        committed |= metadata.get('phase')=='committed' and metadata.get('publication',{}).get('key')==key
    parent=output_path(root,Path('output/editor/creature-deployments'))
    for path in parent.glob('*/journal.json') if parent.exists() else ():
        journal=read_json(path)
        committed |= journal.get('phase')=='committed' and journal.get('manifest_sha256')==digest
    if committed:raise ContentError('此补丁已发布，请刷新页面；不能重复新增同一批怪物点位。')
    return key


def prepare(root, package, *, request_id=None, result=None):
    """Verify and stage a self-contained latest package before game writes; never prune an older version here."""
    from .editor import verify_editor_patch
    if request_id is not None and (not isinstance(request_id,str) or str(uuid.UUID(request_id))!=request_id):
        raise ContentError('发布请求编号无效。')
    verify_editor_patch(root, Path(package))
    key = assert_unpublished(root,package)
    parent = output_path(root, Path('output/editor/patches'))
    parent.mkdir(parents=True,exist_ok=True)
    staged = parent/('.next-'+uuid.uuid4().hex)
    try:
        shutil.copytree(package, staged)
        verify_editor_patch(root, staged)
    except Exception:
        remove_tree(root,staged,[parent])
        raise
    publication={'key':key,'staged':str(staged),'manifest_sha256':sha256(read_bytes(staged/'manifest.json'))}
    if request_id is not None:
        publication.update(request_id=request_id,result=result)
    return publication


def _referenced(root, directory):
    """Keep any artifact still used by saved source paths or fingerprints instead of breaking the workspace."""
    settings = output_path(root, Path('output/editor/settings.json'))
    if not settings.exists():
        return False
    doc = read_json(settings)
    from .settings import PATH_FIELDS
    paths = [doc.get('config',{}).get(k,'') for k,_,_ in PATH_FIELDS]
    paths += [e.get('path','') for e in doc.get('validated_files',[])]
    return any((Path(p) if Path(p).is_absolute() else root/str(p)).resolve().is_relative_to(directory)
               for p in paths if p)


def restore_inputs(root,document):
    """Detach old settings' required package inputs so the last backup can restore after history cleanup."""
    if not document:return None
    from .settings import PATH_FIELDS,input_path,portable_path
    def packaged(path):
        for name in ('patches','creature-patches'):
            parent=root/'output/editor'/name
            if path.is_relative_to(parent):
                parts=path.relative_to(parent).parts
                return bool(parts and (STAMP.fullmatch(parts[0]) or parts[0]=='latest'))
        return False
    client=input_path(root,document['config']['database']['client'])
    if packaged(client):
        raise ContentError('MySQL 客户端位于历史补丁包中，请在设置迁移客户端并重新保存密码。')
    copied={}
    def materialize(path):
        if path.is_dir():
            _no_links(path)
            hashes={p.relative_to(path).as_posix():sha256(read_bytes(p)) for p in path.rglob('*') if p.is_file()}
        else:hashes={'.':sha256(read_bytes(path))}
        identity=sha256(json.dumps({'path':str(path),'files':hashes},sort_keys=True).encode('utf-8'))[:16]
        target=output_path(root,Path('output/editor/baselines/inputs')/identity/path.name)
        if path.is_dir():shutil.copytree(path,target,dirs_exist_ok=True)
        else:
            from .files import atomic_write
            atomic_write(target,read_bytes(path))
        for name,digest in hashes.items():
            if sha256(read_bytes(target if name=='.' else target/name))!=digest:
                raise ContentError('恢复所需基线迁移校验失败。')
        copied[path]=target
        return portable_path(root,target)
    result={**document,'config':dict(document['config'])}
    for key,_,_ in PATH_FIELDS:
        text=document['config'].get(key,'')
        if text and packaged(path:=input_path(root,text)):
            result['config'][key]=materialize(path)
    fingerprints=[]
    for original in document.get('validated_files',[]):
        entry=dict(original);path=input_path(root,entry['path'])
        if packaged(path):
            if sha256(read_bytes(path))!=entry['sha256']:raise ContentError('历史输入已改变，不能生成可靠恢复点。')
            mapped=next((target/path.relative_to(source) for source,target in copied.items() if path.is_relative_to(source)),None)
            entry['path']=portable_path(root,mapped) if mapped is not None else materialize(path)
        fingerprints.append(entry)
    if not copied:return None
    result['validated_files']=fingerprints
    return json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8')


def protect_inputs(root,folder,document):
    """Embed managed snapshot/baseline input bytes in the latest set so recovery can reconstruct missing copies."""
    from .settings import input_path,PATH_FIELDS
    if not document:return []
    roots=[]
    for key,_,_ in PATH_FIELDS:
        text=document['config'].get(key,'')
        if not text:continue
        path=input_path(root,text)
        if path.is_relative_to(root/'output') and (key=='npc_snapshot' or path.is_relative_to(root/'output/editor/baselines/inputs')):
            roots.append(path)
    copies={}
    for path in roots:
        if path.is_dir():_no_links(path)
        for source in (path.rglob('*') if path.is_dir() else [path]):
            if not source.is_file():continue
            raw=read_bytes(source);digest=sha256(raw);relative='inputs/'+digest[:16]+'/data'
            from .files import atomic_write
            atomic_write(folder/relative,raw)
            copies[str(source)]={'target':str(source),'backup':relative,'sha256':digest}
    return list(copies.values())


def restore_protected_inputs(root,folder,copies):
    """Reconstruct only missing project-managed inputs; preserve any external changes instead of overwriting them."""
    from .files import atomic_write
    for entry in copies:
        target=Path(entry['target'])
        if output_path(root,target)!=target.absolute() or not re.fullmatch(r'inputs/[0-9a-f]{16}/data',entry['backup']):
            raise ContentError('恢复输入路径无效。')
        raw=read_bytes(folder/entry['backup'])
        if sha256(raw)!=entry['sha256']:raise ContentError('恢复输入备份校验失败。')
        if target.exists():
            if sha256(read_bytes(target))!=entry['sha256']:raise ContentError('恢复输入已被外部修改，未覆盖。')
        else:atomic_write(target,raw)


def finish(root, publication):
    """Finalize committed metadata and prune only known completed artifacts; return cleanup warnings separately."""
    from .editor import verify_editor_patch
    warnings = []
    receipt = output_path(root,Path('output/editor/publications/receipts')/(publication['key']+'.json'))
    write_json(receipt,{'format':'xyj-publication-receipt/v1','status':'committed',
                        'key':publication['key'],'manifest_sha256':publication['manifest_sha256']})
    if publication.get('request_id'):
        write_json(output_path(root,Path('output/editor/publications/requests')/(publication['request_id']+'.json')),
                   {**publication['result'],'status':'applied','publication_key':publication['key']})
    parent = output_path(root,Path('output/editor/patches'))
    staged = Path(publication['staged']); latest = parent/'latest'; previous = parent/'previous'
    if staged.exists():
        if staged.parent != parent or not re.fullmatch(r'\.next-[0-9a-f]{32}',staged.name):
            raise ContentError('最新补丁暂存目录无效。')
        verify_editor_patch(root,staged)
        if sha256(read_bytes(staged/'manifest.json')) != publication['manifest_sha256']:
            raise ContentError('最新补丁暂存内容变化。')
        if previous.exists():
            verify_editor_patch(root,previous)
            remove_tree(root,previous,[parent])
        if latest.exists():
            move_path(latest,previous)
        move_path(staged,latest)
    elif not latest.exists() or sha256(read_bytes(latest/'manifest.json')) != publication['manifest_sha256']:
        raise ContentError('已提交的最新补丁暂存丢失，请保留备份。')
    for folder in [parent,output_path(root,Path('output/editor/creature-patches'))]:
        for candidate in list(folder.iterdir()) if folder.exists() else ():
            if not candidate.is_dir() or candidate == latest or not (STAMP.fullmatch(candidate.name) or candidate.name == 'previous' or re.fullmatch(r'\.next-[0-9a-f]{32}',candidate.name)):
                continue
            try:
                verify_editor_patch(root,candidate)
                if _referenced(root,candidate):
                    raise ContentError('历史补丁仍被当前数据源引用，需迁移数据源后清理。')
                remove_tree(root,candidate,[folder])
            except (ContentError,OSError) as exc:
                warnings.append(str(exc))
    legacy = output_path(root,Path('output/editor/creature-deployments'))
    for candidate in list(legacy.iterdir()) if legacy.exists() else ():
        if not candidate.is_dir() or not re.fullmatch('[0-9a-f]{32}',candidate.name):
            continue
        try:
            journal = read_json(candidate/'journal.json')
            if (journal.get('format') != 'xyj-creature-deploy/v1' or journal.get('phase') not in ('committed','rolled_back') or
                    not isinstance(journal.get('manifest_sha256'),str) or not re.fullmatch('[0-9a-f]{64}',journal['manifest_sha256'])):
                raise ContentError('历史怪物发布未完成，保留恢复目录：'+str(candidate))
            summary = {'format':journal['format'],'phase':journal['phase'],'manifest_sha256':journal['manifest_sha256']}
            write_json(output_path(root,Path('output/editor/publications/history')/(candidate.name+'.json')),summary)
            if journal['phase']=='committed':
                if not re.fullmatch('[0-9a-f]{64}',journal['manifest_sha256']):raise ContentError('旧发布哈希无效。')
                write_json(output_path(root,Path('output/editor/publications/legacy')/(journal['manifest_sha256']+'.json')),summary)
            remove_tree(root,candidate,[legacy])
        except (ContentError,OSError) as exc:
            warnings.append(str(exc))
    return '；'.join(warnings)


def check_legacy(root):
    """Block a new publication when an older NPC publisher still needs database recovery."""
    parent = output_path(root,Path('output/editor/creature-deployments'))
    for path in parent.glob('*/journal.json') if parent.exists() else ():
        if read_json(path).get('phase') not in ('committed','rolled_back'):
            raise ContentError('存在未完成怪物发布，请先停服恢复：'+str(path.parent))


def discard(root,publication):
    """Remove a failed attempt's private staged package only after game/database rollback has succeeded."""
    parent=output_path(root,Path('output/editor/patches'))
    staged=Path(publication['staged'])
    if staged.parent!=parent or not re.fullmatch(r'\.next-[0-9a-f]{32}',staged.name):
        raise ContentError('失败补丁清理路径无效。')
    remove_tree(root,staged,[parent])
    receipt=output_path(root,Path('output/editor/publications/receipts')/(publication['key']+'.json'))
    if receipt.exists():
        write_json(receipt,{'format':'xyj-publication-receipt/v1','status':'rolled_back','key':publication['key']})
    if publication.get('request_id'):
        write_json(output_path(root,Path('output/editor/publications/requests')/(publication['request_id']+'.json')),{'status':'rolled_back'})


def request_result(root,request_id):
    """Read a durable publication outcome after a lost HTTP response; an unknown result never authorizes a retry."""
    if not isinstance(request_id,str) or str(uuid.UUID(request_id))!=request_id:
        raise ContentError('发布请求编号无效。')
    path=output_path(root,Path('output/editor/publications/requests')/(request_id+'.json'))
    if path.exists():return read_json(path)
    latest=output_path(root,Path('output/editor/backups/latest/manifest.json'))
    if latest.exists():
        manifest=read_json(latest);publication=manifest.get('publication',{})
        if manifest.get('phase')=='committed' and publication.get('request_id')==request_id:
            return {**publication['result'],'status':'applied'}
    return {'status':'unknown'}
