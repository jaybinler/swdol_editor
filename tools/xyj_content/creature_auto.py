"""Automatic NPC publication with shared latest backup, database recovery and editor-state synchronization."""

from datetime import datetime,timezone
from pathlib import Path
import json
import os
import tempfile
import uuid

from . import deployment as files
from .binary import ContentError,read_bytes,MAX_FILE_BYTES
from .catalog import Inputs,load_inputs
from .creature_editor import export_patch,verify_patch
from .creature_database import point_plan,schema,image,number,matches_snapshot,apply_points
from .creature_deployment import targets as creature_targets
from .database import connection_binding,read_npc_rows
from .files import output_path,atomic_write,read_json,write_json,sha256,move_path
from .patch import PAYLOAD_PATHS
from .publication import prepare,finish,check_legacy


def _snapshot(controller,loaded,directory,query):
    """Refresh saved NPC rows only when points changed; preserve valid offline script evidence otherwise."""
    old=controller.workspace
    manifest=read_json(old.npc_directory/'manifest.json')
    data=read_json(old.npc_directory/manifest['catalog_file'])
    if query is not None:
        data['instances']=read_npc_rows({},'',query=query)
        from .plot_maps import build_script_index
        from .settings import input_path
        maps=input_path(controller.root,controller.config['map_dir'])
        data['script_index'],_=build_script_index(data['instances'],loaded.server.records,maps)
    raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    digest=sha256(raw)
    manifest.update(catalog_sha256=digest,catalog_file='catalog-'+digest+'.json',
                    server_object_sha256=sha256(loaded.blobs[0]))
    manifest['source']={**manifest['source'],'creature_publication':{'database_rows_refreshed':query is not None,
                         'previous_server_sha256':old.hashes['server_object']}}
    if query is not None:
        manifest['source']['captured_at']=datetime.now(timezone.utc).isoformat()
    atomic_write(directory/manifest['catalog_file'],raw)
    write_json(directory/'manifest.json',manifest)


def _state(controller,targets,blobs,snapshot):
    """Build recoverable settings and one empty creature draft without changing the live controller."""
    from .settings import portable_path
    root=controller.root;old=controller.workspace
    config={**controller.config,'server_object':portable_path(root,targets['server_object']),
            'client_object':'','language':'','npc_snapshot':portable_path(root,snapshot)}
    if 'actor' in targets:
        config['act_dir']=''
    if any(k.startswith('model:') for k in targets):
        config['model_dir']='';config['tex_dir']=''
    hashes={k:sha256(blobs[k]) for k in PAYLOAD_PATHS}
    identity=json.dumps({'hashes':hashes,'paths':[str(targets[k].resolve()) for k in PAYLOAD_PATHS]},sort_keys=True)
    directory=output_path(root,Path('output/editor/workspaces')/sha256(identity.encode('utf-8')))
    draft=directory/'creatures.json'
    if directory != old.draft_path.parent:
        for name in ('draft.json','creatures.json'):
            if (directory/name).exists() and read_json(directory/name).get('edits'):
                raise ContentError('覆盖后工作区已有未完成草稿，请先处理以免覆盖。')
    excluded={p.resolve() for p in [old.loaded.paths.server,old.loaded.paths.client,old.loaded.paths.language,
                                  old.npc_directory/'manifest.json',*targets.values()]}
    old_manifest=read_json(old.npc_directory/'manifest.json')
    excluded.add((old.npc_directory/old_manifest['catalog_file']).resolve())
    fingerprints=[e for e in (controller.document or {}).get('validated_files',[])
                  if (root/e['path']).resolve() not in excluded]
    fingerprints += [{'path':portable_path(root,p),'sha256':sha256(blobs[k])} for k,p in targets.items()]
    manifest=read_json(snapshot/'manifest.json')
    fingerprints += [{'path':portable_path(root,p),'sha256':sha256(read_bytes(p))}
                     for p in (snapshot/'manifest.json',snapshot/manifest['catalog_file'])]
    document={**(controller.document or {}),'format':'xyj-settings/v1','config':config,
              'validated_files':fingerprints,'saved_at':datetime.now(timezone.utc).isoformat()}
    projects=dict((controller.document or {}).get('model_projects',{}))
    if projects or any(e.get('model_ref') for e in old.creature_edits):
        from .binary import decode_table
        from .model_workspace import project_for,bundle_path,load_bundle,_program
        import struct
        raw=blobs.get('actor') or read_bytes(targets['client_object'].parent.parent/'Act/all_char.act')
        rows={struct.unpack_from('<I',r,4)[0]:r for r in decode_table(blobs['server_object']).records}
        for edit in old.creature_edits:
            source_project=project_for(old,controller.document,edit['appearance_id'])
            if edit.get('model_ref'):
                bundle=load_bundle(bundle_path(root,edit['model_ref']),edit['model_ref'])
                project={'reference':edit['model_ref'],'appearance_id':bundle['template_id'],'scale':edit['scale']}
            elif source_project:
                project={**source_project,'scale':[a*b for a,b in zip(source_project['scale'],edit['scale'])]}
            else:
                projects.pop(str(edit['target_id']),None);continue
            actor=struct.unpack_from('<H',rows[edit['target_id']],60)[0]
            project.update(actor=actor,program_sha256=sha256(_program(raw,actor)))
            projects[str(edit['target_id'])]=project
        document['model_projects']=projects
    value={'format':'xyj-creature-draft/v1','sources':hashes,'revision':old.creature_revision+1,'edits':[]}
    payloads=[]
    if old.creature_draft_path!=draft:
        source_draft={'format':'xyj-creature-draft/v1','sources':old.hashes,'revision':old.creature_revision+1,'edits':[]}
        payloads.append(('source_draft',old.creature_draft_path,json.dumps(source_draft,ensure_ascii=False,indent=2).encode('utf-8')))
    payloads += [('settings',controller.path,json.dumps(document,ensure_ascii=False,indent=2).encode('utf-8')),
                 ('draft',draft,json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8'))]
    return config,document,payloads


def _record(folder,role,target,blob,restore_document=None,expected=None):
    """Persist a full file preimage before its eventual replacement; target is module-owned."""
    before=files._current(target)
    if expected is not None and target in expected and (sha256(before) if before is not None else None)!=expected[target]:
        raise ContentError('发布期间设置或草稿被其他程序修改，未覆盖这些输入。')
    entry={'role':role,'target':str(target),'before':sha256(before) if before is not None else None,
           'after':sha256(blob),'backup':files._backup_relative(role),
           'staged':str(target.parent/('.xyj-apply-'+uuid.uuid4().hex))}
    if before is not None:
        atomic_write(folder/entry['backup'],before)
        if read_bytes(folder/entry['backup']) != before:
            raise ContentError('本地备份读回失败。')
    if role=='settings' and restore_document is not None:
        entry.update(restore='state/restore-settings.json',restore_sha256=sha256(restore_document))
        atomic_write(folder/entry['restore'],restore_document)
    return entry


def _write(entry,blob):
    """Stage and compare the current target before replacing, then verify the complete result."""
    target=Path(entry['target']);staged=Path(entry['staged'])
    target.parent.mkdir(parents=True,exist_ok=True)
    files._stage_file(target,blob,staged)
    current=files._current(target)
    if (sha256(current) if current is not None else None) != entry['before']:
        raise ContentError('发布期间目标被外部修改。')
    files._replace_file(staged,target)
    if sha256(read_bytes(target)) != entry['after']:
        raise ContentError('覆盖后读回失败。')


def apply_workspace(controller,revision,*,server_stopped=False,query=None,request_id=None,package=None):
    """Publish exact current NPC drafts once; optional query injection is reserved for isolated integration tests."""
    root=controller.root;state=controller.workspace
    if server_stopped is not True:
        raise ContentError('请先停止相关游戏服务和客户端，再确认导出。')
    if state is None:
        raise ContentError('请先校验数据源设置。')
    state.load_creature_draft()
    if type(revision) is not int or revision != state.creature_revision:
        raise ContentError('怪物草稿或数据源已变化，请刷新页面。')
    if state.edits:
        raise ContentError('存在道具草稿，请先完成或移除后再发布怪物。')
    with files.deployment_lock(root):
        files._recover_locked(root)
        inputs=files.workspace_inputs(controller,creature=True,allow_package=package is not None)
        check_legacy(root)
        from .publication import restore_inputs
        restore_document=restore_inputs(root,controller.document)
        provided=Path(package).resolve() if package is not None else None
        if provided is not None:
            verification=verify_patch(provided);source_manifest=read_json(provided/'manifest.json')
            if ({k:v['sha256'] for k,v in source_manifest['sources'].items()}!=state.hashes or
                    source_manifest['edits']!=state.creature_edits):
                raise ContentError('原补丁与当前基线或草稿不一致，不能自动重建为另一份发布。')
            plan=read_json(provided/'npc-points.json')['operations']
        else:
            plan=point_plan(state.creature_edits,state.creatures())
        if sum(p['point']['count'] if p['point']['action']=='add' else 1 for p in plan)>500:
            raise ContentError('一次发布最多涉及 500 个 NPC 存档，请拆分草稿后发布。')
        if plan and controller.config.get('npc_mode') != 'database' and query is None:
            raise ContentError('含点位修改须在设置中校验数据库连接并保护保存密码。')
        if plan and query is None:
            if not (controller.document or {}).get('secret_file') and not os.environ.get('XYJ_MYSQL_PASSWORD'):
                raise ContentError('尚未保护保存数据库密码，请在设置中输入密码、勾选保存，再校验并保存。')
            from .creature_connection import database_query
            _,query=database_query(root,controller.document)
        if plan:
            columns=schema(query)
            journal_bytes=0
            for operation in plan:
                p=operation['point'];table,role=p['key'].split(':')
                row=image(query,table,int(role),columns[table])
                if not matches_snapshot(row,p['expected']) or number(row,'ObjectType') != 0:
                    raise ContentError('来源点位已变化，请重新校验数据源。')
                if any(number(row,k) not in (-1,0) for k in ('OwnerDBID','AccountID','LaborID')):
                    raise ContentError('有主存档不能作为普通刷新点修改。')
                count=p['count'] if p['action']=='add' else 1
                journal_bytes+=count*(4*len(json.dumps(row))+2048)
                if journal_bytes>MAX_FILE_BYTES//2:
                    raise ContentError('存档恢复资料预计超过安全上限，请拆分草稿。')
        else:
            query=None
        pending=output_path(root,Path('output/editor/pending'));pending.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=pending,prefix='c-',ignore_cleanup_errors=True) as temporary:
            exported=({'path':str(provided),'changes':source_manifest['changes'],'verification':verification} if provided is not None else
                      export_patch(root,state.loaded,state.creature_edits,catalog=state.creatures(),
                                   npc_directory=state.npc_directory,client=state.client,destination=Path(temporary)/'p'))
            package=Path(exported['path']);metadata=read_json(package/'manifest.json')
            from .model_workspace import load_bundle, retain_bundle
            from .settings import input_path
            for reference in {e['model_ref'] for e in state.creature_edits if e.get('model_ref')}:
                bundle=load_bundle(package/'model-bundles'/reference,reference)
                client_root=input_path(root,controller.config['client_root'])
                for name,digest in bundle['sources'].items():
                    if name.startswith('Act/') and sha256(read_bytes(client_root/name))!=digest:
                        raise ContentError('客户端动作资源已变化，无法保证模型索引未被其他动作引用。')
                retain_bundle(root, package/'model-bundles'/reference, reference)
            destinations=files.deployment_targets(root,controller.config)
            checked=creature_targets(root,controller.config,metadata)
            blobs={k:read_bytes(package/relative) for k,relative in PAYLOAD_PATHS.items()}
            from .model_compiler import resource_role
            for relative in metadata['resources']:
                role=resource_role(relative)
                destinations[role]=checked[relative];blobs[role]=read_bytes(package/relative)
            for key,path in destinations.items():
                relative=files._backup_relative(key)
                new_resource=relative in metadata['resources'] and metadata['resources'][relative].get('source_sha256') is None
                original=None if new_resource else read_bytes(package/'baseline'/relative)
                if files._current(path) != original:
                    raise ContentError('目标与补丁基线不一致，请重新校验数据源。')
            publication=prepare(root,package,request_id=request_id,result={
                'operations':len(exported['changes']),'point_changes':len(plan),
                'backup_path':str(files._journal(root)),'workspace_generation':controller.generation+1})
            loaded=load_inputs(Inputs(*(package/p for p in PAYLOAD_PATHS.values())))
            folder=files._journal(root);next_backup=folder.parent/('.next-'+uuid.uuid4().hex)
            entries=[_record(next_backup,k,p,blobs[k]) for k,p in destinations.items()]
            manifest={'format':'xyj-auto-deploy/v1','kind':'creature','root':str(root.resolve()),'phase':'prepared',
                      'entries':entries,'state_pending':True,'publication':publication,'changes':exported['changes'],
                      'database_binding':connection_binding(controller.config['database']).hex() if plan else None,
                      'verification':exported['verification']}
            write_json(next_backup/'manifest.json',manifest)
            if folder.exists():move_path(folder,folder.parent/'previous')
            move_path(next_backup,folder)
            try:
                manifest['phase']='applying';write_json(folder/'manifest.json',manifest)
                if plan:apply_points(query,plan,folder/'npc-journal.json')
                for entry in entries:
                    _write(entry,blobs[entry['role']])
                snapshot=output_path(root,Path('output/editor/applied-snapshots')/uuid.uuid4().hex)
                _snapshot(controller,loaded,snapshot,query)
                config,document,state_payloads=_state(controller,destinations,blobs,snapshot)
                state_entries=[_record(folder,k,p,b,restore_document,expected=inputs) for k,p,b in state_payloads]
                entries += state_entries
                from .publication import protect_inputs
                entries[-2]['inputs']=protect_inputs(root,folder,json.loads(restore_document) if restore_document else controller.document)
                manifest['state_pending']=False;write_json(folder/'manifest.json',manifest)
                files._validate_journal(root,folder)
                for entry,(_,_,blob) in zip(state_entries,state_payloads):_write(entry,blob)
                candidate=controller.factory(config);candidate.creatures();candidate.load_creature_draft()
                manifest['phase']='committed';write_json(folder/'manifest.json',manifest)
            except Exception as exc:
                files._rollback(root,folder,manifest,query=query)
                files._restore_previous(root,folder)
                raise ContentError('发布失败，已恢复文件与 NPC 存档，草稿保留：'+str(exc)) from exc
            finally:
                for entry in entries:
                    try:Path(entry['staged']).unlink(missing_ok=True)
                    except OSError:pass
        controller.workspace,controller.config,controller.document=candidate,config,document
        candidate.token=controller.token;controller.generation+=1;controller.pending=None;controller.setup_error=''
        from .resources import clear_resource_caches
        clear_resource_caches()
        warning=''
        try:
            warning=finish(root,publication)
            files._remove_local(root,folder.parent/'previous')
        except (ContentError,OSError) as exc:
            warning='发布已成功，历史清理待重试：'+str(exc)
        return {'status':'applied','operations':len(exported['changes']),'point_changes':len(plan),
                'backup_path':str(folder),'package_path':str(root/'output/editor/patches/latest'),
                'workspace_generation':controller.generation,'cleanup_warning':warning,
                'verification':{**exported['verification'],'database_readback_verified':bool(plan),'target_files_verified':True}}


def recovery_pending(root):
    """Describe a shared latest backup requiring explicit stopped-server recovery, without executing writes."""
    folder=files._journal(root)
    if not (folder/'manifest.json').exists():return False
    manifest=read_json(folder/'manifest.json')
    return manifest.get('kind')=='creature' and manifest.get('phase') not in ('committed','rolled_back')


def recover_workspace(controller,*,server_stopped=False,allow_committed=False):
    """Recover the self-contained latest set only after an explicit stopped-server confirmation."""
    if server_stopped is not True:raise ContentError('恢复前请停止相关游戏服务和客户端。')
    root=controller.root
    with files.deployment_lock(root):
        folder=files._journal(root);manifest=files._validate_journal(root,folder)
        if manifest.get('phase')=='rolled_back' or manifest.get('phase')=='committed' and not allow_committed:
            raise ContentError('没有未完成的发布需要恢复。')
        query=None
        if (folder/'npc-journal.json').exists():
            from .creature_connection import database_query
            document=read_json(controller.path)
            config,query=database_query(root,document)
            if connection_binding(config['database']).hex()!=manifest.get('database_binding'):
                raise ContentError('恢复的数据库连接与发布记录不同，已停止。')
        files._rollback(root,folder,manifest,query=query)
        files._restore_previous(root,folder)
        document=read_json(controller.path);config=document['config']
        candidate=controller.factory(config);candidate.creatures();candidate.token=controller.token
        controller.workspace,controller.config,controller.document=candidate,config,document
        controller.generation+=1;controller.setup_error='';controller.pending=None
        return {'status':'rolled_back'}
