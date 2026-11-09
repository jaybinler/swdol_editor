"""Versioned model authoring sessions and immutable, self-contained resource bundles."""

import base64
import json
from pathlib import Path
import re
import struct
import uuid

from .binary import ContentError,read_bytes,MAX_FILE_BYTES
from .files import output_path,atomic_write,read_json,write_json,sha256,move_path
from .models import actor_data,resolve_model,read_action
from .creature_assets import SIZES
from . import native_mesh,native_texture

HASH=re.compile(r'[0-9a-f]{64}')
MODEL_NAME=re.compile(r'CHR([0-9]{5})\.PLY',re.I)
TEX_NAME=re.compile(r'ALL([0-9]{5})\.TEX',re.I)
ACT_FILES=('all_char.act','all_item.act','all_magic.act','all_sys.act','all_map1.act','all_map2.act')


def canonical(value):
    """Produce one deterministic, finite JSON representation for immutable content identities."""
    try:return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
    except (TypeError,ValueError) as exc:raise ContentError('模型文档包含不可序列化的数值。') from exc


def actions(program):
    """Walk complete native selectors; report real FR model and AP pose references."""
    if len(program)<2:raise ContentError('动作目录截断。')
    count=struct.unpack_from('<H',program)[0]
    if not 1<=count<=1024 or len(program)<2+count*6:raise ContentError('动作目录无效。')
    result=[]
    for selector in range(count):
        start,length=struct.unpack_from('<IH',program,2+selector*6)
        if not length:continue
        if start<2+count*6 or start+length>len(program):raise ContentError('动作范围无效。')
        cursor=start;kind=0;frame=None;poses=[];commands=[];ended=False
        while cursor+2<=start+length:
            op=bytes(program[cursor:cursor+2]);size=SIZES.get(op);position=cursor+2;cursor=position
            if size is None or cursor+size>start+length:raise ContentError(f'动作 {selector} 含未解析指令，不能完整编辑。')
            if op==b'PT':kind=program[cursor]
            if op==b'FR':
                frame=struct.unpack_from('<H',program,cursor)[0]
                if kind==0:commands.append((cursor,frame))
            if op==b'AP' and kind==0 and frame is not None:poses.append([frame,struct.unpack_from('<H',program,cursor)[0]])
            cursor+=size
            if op in (b'VO',b'2O'):ended=True;break
        if not ended:raise ContentError('动作缺少终止指令。')
        result.append(dict(selector=selector,start=start,length=length,models=commands,poses=poses))
    return result


def session_path(state,template):
    """Keep the editable session revision local to this immutable game-data workspace."""
    return output_path(state.root,Path('output/editor/model-sessions')/state.draft_path.parent.name/f'{template}.json')


def bundle_path(root,reference):
    """Resolve only content-addressed bundles inside tool-managed output."""
    if not isinstance(reference,str) or not HASH.fullmatch(reference):raise ContentError('模型草稿编号无效。')
    return output_path(root,Path('output/editor/model-bundles')/reference)


def bundle_image(path, images, key):
    """Find a compact image member by manifest order, accepting older hash-named members."""
    try:
        index = images.index(key)
    except ValueError as exc:
        raise ContentError('模型贴图未列入草稿清单。') from exc
    compact = path / 'images' / f'{index:03}.rgba'
    return compact if compact.exists() else path / 'images' / (key + '.rgba')


def load_bundle(path,reference):
    """Validate the immutable bundle identity and every embedded source/image before use."""
    bundle=read_json(path/'bundle.json')
    if sha256(canonical(bundle))!=reference or bundle.get('format')!='xyj-model-bundle/v1':raise ContentError('模型草稿清单校验失败。')
    if not isinstance(bundle.get('sources'),dict) or len(bundle['sources'])>256:raise ContentError('模型来源清单超限。')
    total=0
    for name,digest in bundle['sources'].items():
        if name not in (*('Act/'+n for n in ACT_FILES),'MODEL/ALL_CHAR.IDX','TEX/ALL_TEX.IDX') and not re.fullmatch(r'(MODEL/CHR[0-9]{5}\.PLY|TEX/ALL[0-9]{5}\.TEX)',name):raise ContentError('模型来源路径不在白名单。')
        raw=read_bytes(path/'sources'/name);total+=len(raw)
        if total>64*1024*1024 or sha256(raw)!=digest:raise ContentError('模型来源超限或已被修改。')
    images=bundle.get('images',[])
    if not isinstance(images,list) or len(images)>128:raise ContentError('模型贴图草稿数量超限。')
    for key in images:
        if not isinstance(key,str) or not HASH.fullmatch(key):raise ContentError('模型贴图草稿编号无效。')
        raw=read_bytes(bundle_image(path,images,key));total+=len(raw)
        if total>64*1024*1024 or sha256(raw)!=key:raise ContentError('模型贴图草稿超限或校验失败。')
    if len(set(images)) != len(images):raise ContentError('模型贴图草稿编号重复。')
    return bundle


def copy_bundle(source,destination,reference):
    """Copy only declared, hash-checked model inputs; never copy unrelated directory contents."""
    bundle=load_bundle(source,reference)
    for name in bundle['sources']:atomic_write(destination/'sources'/name,read_bytes(source/'sources'/name))
    images = bundle['images']
    for index, key in enumerate(images):
        atomic_write(destination / 'images' / f'{index:03}.rgba', read_bytes(bundle_image(source, images, key)))
    write_json(destination/'bundle.json',bundle)
    load_bundle(destination,reference)


def retain_bundle(root, source, reference):
    """Retain the editable project from a verified imported package before publishing it."""
    destination = bundle_path(root, reference)
    if destination.exists():
        load_bundle(destination, reference)
        return
    temporary = destination.with_name('.model-' + uuid.uuid4().hex)
    try:
        copy_bundle(source, temporary, reference)
        move_path(temporary, destination)
    except Exception:
        if temporary.exists():
            from .publication import remove_tree
            remove_tree(root, temporary, [destination.parent])
        raise


def _actor(state,template,reference=None):
    from .creature_editor import creature_record,integer
    integer(template,1,32767,'模型来源模板')
    row=creature_record(state.loaded,template)[0];actor=struct.unpack_from('<H',row,60)[0]
    if reference:
        folder=bundle_path(state.root,reference);bundle=load_bundle(folder,reference)
        if bundle['template_id']!=template:raise ContentError('模型项目来源模板不一致。')
        actor=integer(bundle['actor'],1,2999,'项目原始动作')
    if not 1<=actor<3000:raise ContentError('完整模型编辑仅支持原生角色动作命名空间。')
    program=actor_data(state.client,actor)
    if reference and program!=_program(read_bytes(folder/'sources/Act/all_char.act'),actor):
        raise ContentError('项目的原始动作已被外部修改；模型项目保留，请核对来源。')
    return actor,program,actions(program)


def open_document(state,template,reference=None,*,fresh=False):
    """Open either the exact monster draft model or the current source, with a CAS session revision."""
    path=session_path(state,template)
    session=read_json(path) if path.exists() else {'revision':0,'reference':None}
    if reference is None and not fresh:reference=session.get('reference')
    actor,program,entries=_actor(state,template,reference)
    if reference:
        folder=bundle_path(state.root,reference);bundle=load_bundle(folder,reference)
        if bundle['template_id']!=template:raise ContentError('模型草稿来源与所选外观不一致。')
        docs=bundle['documents']
        sources={f:read_bytes(folder/'sources'/f) for f in bundle['sources']}
        # Permit unrelated ACT/IDX additions while preserving every original model/texture.
        for name,blob in sources.items():
            if name.startswith('Act/') or name.endswith('.IDX'):continue
            directory,file=name.split('/')
            if read_bytes(state.client/directory/file)!=blob:
                raise ContentError('项目的原始模型或贴图已被外部修改；编辑项目仍保留。')
        for m in bundle['models']:
            if resolve_model(state.client,m['frame']).name!=m['file']:raise ContentError('项目原始模型的索引已变化。')
    else:
        docs=[];sources={}
        frames=sorted({frame for a in entries for _,frame in a['models']})
        if len(frames)>64:raise ContentError('该角色超过64个动作模型，暂不支持完整模型编辑。')
        for frame in frames:
            if not 1<=frame<3000:raise ContentError('该角色动作跨越未支持的模型命名空间。')
            p=resolve_model(state.client,frame);blob=read_bytes(p)
            native=native_mesh.decode(blob)
            doc=native_mesh.document(native,frame,sha256(blob));native_mesh.validate_document(doc,native)
            docs.append(doc)
            sources['MODEL/'+p.name]=blob
    if not docs:raise ContentError('该角色没有可编辑的三维动作模型。')
    previews=[]
    for doc in docs:
        frame=doc['source_frame']
        model_file=(resolve_model(state.client,frame).name if not reference else next(
            m['file'] for m in bundle['models'] if m['frame']==frame))
        native=native_mesh.decode(sources['MODEL/'+model_file])
        previews.append({'frame':frame,'file':model_file,'document':doc,'poses':native['poses'],
                         'bundle_reference':reference,
                         'material_flags':[struct.unpack_from('<I',m,12)[0] for m in native['materials']],
                         'morphs':[{'offsets':p['offsets'],'vertices':[v[:3] for v in p['vertices']]} for p in native['morphs']],
                         'source_groups':native['groups'],'source_vertices':[v[:3] for v in native['vertices']]})
    if len(canonical(previews))>MAX_FILE_BYTES:raise ContentError('完整动作预览数据超过安全上限。')
    for name in (*('Act/'+n for n in ACT_FILES),'MODEL/ALL_CHAR.IDX','TEX/ALL_TEX.IDX'):
        directory,file=name.split('/');sources[name]=read_bytes(state.client/directory/file)
    if not reference:
        for doc in docs:
            for material in doc['materials']:
                if 0<=material['texture']<=99999:
                    name=f'TEX/ALL{material["texture"]:05d}.TEX';sources[name]=read_bytes(state.client/'TEX'/name[4:])
    from .creature_editor import creature_record
    selector=bundle.get('selector',struct.unpack_from('<H',creature_record(state.loaded,template)[0],62)[0]) if reference else struct.unpack_from('<H',creature_record(state.loaded,template)[0],62)[0]
    selected=next((a for a in entries if a['selector']==selector and a['models']),None)
    return dict(template_id=template,actor=actor,revision=session['revision'],reference=reference,
                default_frame=selected['models'][0][1] if selected else docs[0]['source_frame'],
                actor_sha256=sha256(program),source_stamp=sha256(canonical({k:sha256(v) for k,v in sources.items()})),models=previews,
                actions=[dict(selector=a['selector'],poses=a['poses']) for a in entries],
                rig_kind='editor_rig_baked_to_native_group_morphs')


def _program(raw,actor):
    from .binary import decompress
    pos=10+(actor-1)*12
    if len(raw)<pos+12:raise ContentError('角色资源目录截断。')
    size,offset,tag,packed=struct.unpack_from('<IIHH',raw,pos)
    if tag!=actor or not size or offset<36010 or offset+(packed or size)>len(raw):raise ContentError('角色资源条目无效。')
    return decompress(raw[offset:offset+packed],size) if packed else raw[offset:offset+size]


def blob_path(root,key):
    """Resolve a bounded uploaded content identity without accepting a filename."""
    if not isinstance(key,str) or not HASH.fullmatch(key):raise ContentError('资源内容编号无效。')
    return output_path(root,Path('output/editor/model-blobs')/(key+'.bin'))


def upload(state,value):
    """Append one ordered base64 chunk; commit only after full SHA-256 and declared byte-count validation."""
    if set(value)!={'id','offset','total','data'}:raise ContentError('模型上传格式无效。')
    key=value['id'];dest=blob_path(state.root,key);offset=value['offset'];total=value['total']
    if type(offset) is not int or type(total) is not int or not 0<=offset<total<=20*1024*1024 or not isinstance(value['data'],str) or len(value['data'])>750000:raise ContentError('模型上传大小或位置无效。')
    try:data=base64.b64decode(value['data'],validate=True)
    except ValueError as exc:raise ContentError('资源分块编码无效。') from exc
    if not data or offset+len(data)>total:raise ContentError('模型上传分块越界。')
    if dest.exists():
        if dest.stat().st_size!=total or sha256(read_bytes(dest))!=key:raise ContentError('已上传资源不完整。')
        return dict(id=key,bytes=total,complete=True)
    part=dest.with_suffix('.part');part.parent.mkdir(parents=True,exist_ok=True)
    present=part.stat().st_size if part.exists() else 0
    if present!=offset:
        # A lost response may repeat an identical chunk; never append it twice.
        if present>=offset+len(data):
            with part.open('rb') as f:f.seek(offset);same=f.read(len(data))==data
            if same:return dict(id=key,bytes=present,complete=False)
        raise ContentError('上传位置已变化，请重新读取上传进度。')
    with part.open('ab') as stream:stream.write(data);stream.flush()
    if part.stat().st_size==total:
        if sha256(read_bytes(part))!=key:
            part.unlink();raise ContentError('上传完整哈希校验失败。')
        move_path(part,dest)
    return dict(id=key,bytes=offset+len(data),complete=dest.exists())


def image_pixels(path):
    """Read the browser-decoded RGBA interchange; revalidate all image limits on the server."""
    raw=read_bytes(path)
    if len(raw)<12:raise ContentError('导入贴图数据截断。')
    marker,width,height=struct.unpack_from('<4sII',raw)
    if marker!=b'RGBA':raise ContentError('导入贴图数据格式无效。')
    native_texture.validate_pixels(width,height,raw[12:])
    return width,height,raw[12:]


def preview_model(state,reference,appearance):
    """Show the exact saved document for the selected native action, never an unrelated first model."""
    folder=bundle_path(state.root,reference);bundle=load_bundle(folder,reference)
    if bundle['template_id']!=appearance:raise ContentError('模型草稿与外观来源不一致。')
    selector=bundle.get('selector',0)
    action=read_action(folder/'sources',bundle['actor'],selector)
    doc=next((d for d in bundle['documents'] if d['source_frame']==action.get('frame')),None)
    if doc is None:raise ContentError('当前预览动作没有对应编辑模型，请进入模型编辑器选择动作。')
    info=next(m for m in bundle['models'] if m['frame']==doc['source_frame'])
    source=native_mesh.decode(read_bytes(folder/'sources/MODEL'/info['file']))
    native_mesh.validate_document(doc,source)
    pose=action.get('pose',0)
    if not 0<=pose<len(source['poses']):raise ContentError('模型预览姿态越界。')
    points=native_mesh.posed_positions(doc,source,pose);vs={v['id']:v for v in doc['vertices']};meshes=[]
    for index,material in enumerate(doc['materials']):
        if struct.unpack_from('<I',source['materials'][index],12)[0]&0xff000000:continue
        faces=[f for f in doc['faces'] if f['material']==index]
        if not faces:continue
        ids=sorted({v for f in faces for v in f['vertices']});mapped={key:i for i,key in enumerate(ids)}
        texture_url=(f'/api/model-edit/texture?image={material["image"]}' if material['image'] else
                     f'/api/model-edit/texture?texture={material["texture"]}' if material['texture']>=0 else None)
        if texture_url:texture_url+=f'&tint={",".join(map(str,material["tint"]))}&reference={reference}'
        meshes.append(dict(positions=[x for key in ids for x in points[key]],uvs=[x for key in ids for x in vs[key]['uv']],
                           indices=[mapped[v] for f in faces for v in f['vertices']],texture=material['texture'] if material['texture']>=0 else None,
                           texture_url=texture_url,material=f'材质 {index+1}'))
    return dict(file=info['file'],bounds=[[min(p[i] for p in points.values()) for i in range(3)],
                                      [max(p[i] for p in points.values()) for i in range(3)]],
                meshes=meshes,vertices=len(doc['vertices']),triangles=len(doc['faces']),frames=len(source['poses']),
                action=action,warnings=['当前为已保存模型草稿的选定姿态；完整动作请进入模型编辑器查看。'])


def project_for(state,document,template):
    """Expose a published authoring project only while its exact actor program is still active."""
    from .creature_editor import creature_record
    projects=(document or {}).get('model_projects',{})
    if not isinstance(projects,dict):return None
    p=projects.get(str(template))
    if not isinstance(p,dict):return None
    try:
        if not HASH.fullmatch(p['reference']) or not HASH.fullmatch(p['program_sha256']):return None
        if type(p['appearance_id']) is not int or not 1<=p['appearance_id']<=32767:return None
        native_mesh._vector(p['scale'],3,'已发布比例',10000)
        if any(s<=0 for s in p['scale']):return None
        actor=struct.unpack_from('<H',creature_record(state.loaded,template)[0],60)[0]
        if actor!=p['actor'] or sha256(actor_data(state.client,actor))!=p['program_sha256']:return None
        if not (bundle_path(state.root,p['reference'])/'bundle.json').is_file():return None
        return p
    except (KeyError,TypeError,ContentError):return None


def selectors(edits,bundles):
    """Retain the project's selected native action even after its original template has been edited."""
    result={}
    for edit in edits:
        if not edit.get('model_ref'):continue
        reference=edit['model_ref'];folder=bundles(reference);bundle=read_json(folder/'bundle.json')
        if sha256(canonical(bundle))!=reference:raise ContentError('模型项目清单已变化。')
        selector=bundle.get('selector')
        if selector is not None:
            program=_program(read_bytes(folder/'sources/Act/all_char.act'),bundle['actor'])
            if type(selector) is not int or selector not in {a['selector'] for a in actions(program) if a['models']}:
                raise ContentError('模型项目动作入口无效。')
            result[edit['target_id']]=selector
    return result


def save_document(state,value):
    """Freeze source bytes and validated edits before returning a reference attachable to a monster draft."""
    if set(value)!={'template_id','revision','actor_sha256','source_stamp','base_ref','documents'}:raise ContentError('模型保存格式无效。')
    if value['base_ref'] is not None and (not isinstance(value['base_ref'],str) or not HASH.fullmatch(value['base_ref'])):raise ContentError('模型项目来源编号无效。')
    template=value['template_id'];actor,program,entries=_actor(state,template,value['base_ref'])
    if value['actor_sha256']!=sha256(program):raise ContentError('模型动作来源已变化，请重新打开。')
    session=session_path(state,template);old=read_json(session) if session.exists() else {'revision':0}
    if type(value['revision']) is not int or value['revision']!=old['revision']:raise ContentError('模型草稿已被另一窗口保存，请重新打开。')
    requested=value['documents']
    if not isinstance(requested,list) or not 1<=len(requested)<=64:raise ContentError('模型文档数量超限。')
    docs=[]
    for key in requested:
        raw=read_bytes(blob_path(state.root,key))
        if len(raw)>12*1024*1024:raise ContentError('模型文档超过大小上限。')
        try:docs.append(json.loads(raw))
        except (ValueError,UnicodeError) as exc:raise ContentError('模型文档编码无效。') from exc
    frames={frame for a in entries for _,frame in a['models']}
    if len(docs)!=len(frames) or {d.get('source_frame') for d in docs}!=frames:raise ContentError('必须保留该怪物全部动作引用的模型。')
    sources={};models=[];images={}
    base_folder=None
    original=None
    if value['base_ref']:
        base_folder=bundle_path(state.root,value['base_ref'])
        original=load_bundle(base_folder,value['base_ref'])
    for name in ('Act/all_char.act','MODEL/ALL_CHAR.IDX','TEX/ALL_TEX.IDX'):
        directory,file=name.split('/');sources[name]=read_bytes(state.client/directory/file)
    from .model_compiler import _used_frames
    used=set()
    for name in ACT_FILES:
        raw=read_bytes(state.client/'Act'/name);sources['Act/'+name]=raw;used.update(_used_frames(raw))
    # IDX pre-fills even unused slots. Redirect only slots unreferenced by all
    # six ACT packs, while retaining every original physical model file.
    available_slots=[slot for slot in range(2999,0,-1) if slot not in used][:64]
    for doc in docs:
        path=resolve_model(state.client,doc['source_frame']);raw=read_bytes(path)
        if sha256(raw)!=doc.get('source_sha256'):raise ContentError('模型基线已变化，请重新打开。')
        native=native_mesh.decode(raw);native_mesh.validate_document(doc,native)
        sources['MODEL/'+path.name]=raw;models.append(dict(frame=doc['source_frame'],file=path.name))
        for material in doc['materials']:
            texture=material['texture']
            if 0<=texture<=99999:sources[f'TEX/ALL{texture:05d}.TEX']=read_bytes(state.client/'TEX'/f'ALL{texture:05d}.TEX')
            key=material['image']
            if key and key not in images:
                path=bundle_image(base_folder,original['images'],key) if original and key in original['images'] else blob_path(state.root,key)
                image_pixels(path)
                images[key]=path
    if len(sources)>256 or len(images)>128 or sum(map(len,sources.values()))+sum(p.stat().st_size for p in images.values())>64*1024*1024:raise ContentError('模型来源总量超过保存上限。')
    if value['source_stamp']!=sha256(canonical({k:sha256(v) for k,v in sources.items()})):
        raise ContentError('打开模型后贴图、索引或动作来源已变化，编辑保留；请重新读取来源后核对。')
    from .creature_editor import creature_record
    selector=struct.unpack_from('<H',creature_record(state.loaded,template)[0],62)[0]
    if original:
        selector=original.get('selector',selector)
    bundle=dict(format='xyj-model-bundle/v1',template_id=template,actor=actor,selector=selector,documents=docs,models=models,
                sources={k:sha256(v) for k,v in sources.items()},images=sorted(images),
                available_model_slots=available_slots,
                occupied_models=sorted(int(m[1]) for p in (state.client/'MODEL').iterdir() if (m:=MODEL_NAME.fullmatch(p.name))),
                occupied_textures=sorted(int(m[1]) for p in (state.client/'TEX').iterdir() if (m:=TEX_NAME.fullmatch(p.name))))
    reference=sha256(canonical(bundle));dest=bundle_path(state.root,reference)
    if not dest.exists():
        temporary=dest.with_name('.model-'+uuid.uuid4().hex)
        try:
            for name,data in sources.items():atomic_write(temporary/'sources'/name,data)
            for index, key in enumerate(bundle['images']):
                atomic_write(temporary / 'images' / f'{index:03}.rgba', read_bytes(images[key]))
            write_json(temporary/'bundle.json',bundle);load_bundle(temporary,reference)
            move_path(temporary,dest)
        except Exception:
            import shutil
            if temporary.exists():shutil.rmtree(temporary)
            raise
    write_json(session,dict(revision=old['revision']+1,reference=reference))
    return dict(reference=reference,revision=old['revision']+1,models=len(docs),status='saved_not_published')
