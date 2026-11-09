"""Compile frozen model documents to independent ACT/IDX/PLY/TEX resources."""

from copy import deepcopy
from pathlib import Path
import re
import struct

from .binary import ContentError,read_bytes,decompress,MAX_FILE_BYTES
from .files import sha256
from . import native_mesh,native_texture
from .model_workspace import load_bundle,actions,_program,image_pixels,bundle_image


def resource_relative(value):
    """Allow only the exact native resource namespaces owned by this compiler."""
    if value in ('client/Act/all_char.act','client/MODEL/ALL_CHAR.IDX','client/TEX/ALL_TEX.IDX') or re.fullmatch(r'client/(MODEL/CHR[0-9]{5}\.PLY|TEX/ALL[0-9]{5}\.TEX)',value):return value
    raise ContentError('模型补丁资源路径不在白名单。')


def resource_role(relative):
    """Map a validated resource to a typed, deterministic recovery role."""
    resource_relative(relative)
    return 'actor' if relative=='client/Act/all_char.act' else 'model:'+relative[7:]


def role_relative(role):
    """Resolve recovery roles without allowing paths supplied by a browser."""
    return resource_relative('client/'+role[6:]) if role.startswith('model:') else 'client/Act/all_char.act'


def _used_frames(raw):
    # Conservative scanning also reserves FR-like bytes in unknown programs.
    used=set()
    for actor in range(1,3000):
        pos=10+(actor-1)*12
        if pos+12>len(raw):raise ContentError('ACT 目录截断。')
        size,offset,tag,packed=struct.unpack_from('<IIHH',raw,pos)
        if not size:continue
        if offset<36010 or offset+(packed or size)>len(raw):raise ContentError('ACT 条目越界。')
        program=decompress(raw[offset:offset+packed],size) if packed else raw[offset:offset+size]
        start=0
        while (start:=program.find(b'FR',start))>=0:
            if start+4<=len(program):used.add(struct.unpack_from('<H',program,start+2)[0])
            start+=2
    return used


def compile_models(client,loaded,edits,*,bundles,source=None):
    """Allocate new independent resources, including mixed existing scale-only edits, deterministically."""
    from .creature_assets import build_variants,scaled_actor,ACT_PATH,SIZES
    original=read_bytes(client/'Act'/'all_char.act') if source is None else source
    simple=[e for e in edits if not e.get('model_ref')]
    resources,bindings=build_variants(client,loaded,simple,source=original)
    actor_output=bytearray(resources.get(ACT_PATH,{}).get('data',original))
    used_actors={struct.unpack_from('<H',r,60)[0] for table in (loaded.server,loaded.client) for r in table.records}|set(bindings.values())
    used_frames=_used_frames(original);occupied_models=set();occupied_textures=set()
    index_sources={};index_outputs={}
    def index(name,blob,signature):
        valid=(len(blob)>=64+32768*2 if signature==b'IDX_TEX\0' else len(blob)>=128+3000*4 and (len(blob)-128)%4==0)
        if blob[:8]!=signature or not valid:raise ContentError('原生资源索引格式无效。')
        if name in index_sources and index_sources[name]!=blob:raise ContentError('多个模型草稿的索引基线不同，请重新打开。')
        if name not in index_sources:index_sources[name]=blob;index_outputs[name]=bytearray(blob)
        return index_outputs[name]
    for edit in edits:
        reference=edit.get('model_ref')
        if not reference:continue
        folder=bundles(reference);bundle=load_bundle(folder,reference)
        if bundle['template_id']!=edit['appearance_id']:raise ContentError('模型草稿与外观模板不一致。')
        get=lambda name:read_bytes(folder/'sources'/name)
        if get('Act/all_char.act')!=original:raise ContentError('模型草稿 ACT 基线已变化，请重新打开模型保存。')
        model_index=index('client/MODEL/ALL_CHAR.IDX',get('MODEL/ALL_CHAR.IDX'),b'IDX_PLY\0')
        texture_index=index('client/TEX/ALL_TEX.IDX',get('TEX/ALL_TEX.IDX'),b'IDX_TEX\0')
        occupied_models.update(bundle['occupied_models']);occupied_textures.update(bundle['occupied_textures'])
        for name in bundle['sources']:
            if name.startswith('Act/'):used_frames.update(_used_frames(get(name)))
        available_slots=[]
        for i in bundle['available_model_slots']:
            if type(i) is not int or not 1<=i<3000:raise ContentError('模型空槽清单无效。')
            if i not in used_frames:available_slots.append(i)
        remap={};all_bounds=[]
        for doc in bundle['documents']:
            info=next((m for m in bundle['models'] if m['frame']==doc['source_frame']),None)
            if not info:raise ContentError('模型草稿缺少来源映射。')
            raw=get('MODEL/'+info['file'])
            if doc['source_sha256']!=sha256(raw):raise ContentError('模型草稿源哈希无效。')
            native=native_mesh.decode(raw);native_mesh.validate_document(doc,native)
            default=native_mesh.document(native,doc['source_frame'],doc['source_sha256'])
            # The opaque vertex word comes from native input, never from document edits.
            doc={**doc,'vertices':[{**v,'aux':native['vertices'][v['origin']][3]}
                                  if v['origin'] is not None else v for v in doc['vertices']]}
            if doc==default:
                remap[doc['source_frame']]=doc['source_frame']
                all_bounds.append([list(struct.unpack_from('<4f',native['header'],4)[:3]),list(struct.unpack_from('<4f',native['header'],20)[:3])])
                continue  # Unedited action models retain their original references and bytes.
            textures={}
            for i,material in enumerate(doc['materials']):
                if material['image'] is None and material['tint']==[1,1,1]:continue
                if material['image'] is not None:
                    w,h,pixels=image_pixels(bundle_image(folder,bundle['images'],material['image']))
                elif 0<=material['texture']<=99999:w,h,pixels=native_texture.rgba(get(f'TEX/ALL{material["texture"]:05d}.TEX'))
                else:raise ContentError('该材质没有可修改的来源贴图，请导入 PNG。')
                # Native 0x4c16f6 checks the u16 presence table at header+64+id*2.
                # 0x4c68a0 then loads ALL{decimal id}.TEX directly; this is not a PLY mapping table.
                slot=next((i for i in range(1,32768)
                           if i not in occupied_textures and struct.unpack_from('<H',texture_index,64+i*2)[0]==0),None)
                if slot is None:raise ContentError('没有空闲贴图资源编号。')
                occupied_textures.add(slot);struct.pack_into('<H',texture_index,64+slot*2,1)
                relative=f'client/TEX/ALL{slot:05d}.TEX';resources[relative]={'source':None,'data':native_texture.encode(w,h,pixels,material['tint'])};textures[i]=slot
            slot=next((i for i in available_slots if i not in used_frames),None)
            if slot is None:raise ContentError('没有空闲角色模型索引。')
            physical=next((i for i in range(1,100000) if i not in occupied_models),None)
            if physical is None:raise ContentError('没有空闲模型文件编号。')
            used_frames.add(slot);occupied_models.add(physical);struct.pack_into('<I',model_index,128+(slot-1)*4,physical)
            geometry={k:v for k,v in doc.items() if k!='materials'}
            if geometry=={k:v for k,v in default.items() if k!='materials'}:
                changed=deepcopy(native)
                for i,texture in textures.items():
                    material=bytearray(changed['materials'][i]);struct.pack_into('<i',material,16,texture);changed['materials'][i]=material
                data=native_mesh.encode(changed) if textures else raw
            else:data=native_mesh.bake(doc,native,textures)
            compiled=native_mesh.decode(data)
            all_bounds.append([list(struct.unpack_from('<4f',compiled['header'],4)[:3]),list(struct.unpack_from('<4f',compiled['header'],20)[:3])])
            resources[f'client/MODEL/CHR{physical:05d}.PLY']={'source':None,'data':data};remap[doc['source_frame']]=slot
        actor=next((i for i in range(1,3000) if i not in used_actors and struct.unpack_from('<I',actor_output,10+(i-1)*12)[0]==0),None)
        if actor is None:raise ContentError('没有空闲角色动作编号。')
        used_actors.add(actor)
        program=bytearray(scaled_actor(_program(original,bundle['actor']),edit['scale']))
        lo=[min(b[0][i] for b in all_bounds) for i in range(3)];hi=[max(b[1][i] for b in all_bounds) for i in range(3)]
        for entry in actions(program):
            for position,frame in entry['models']:
                if frame not in remap:raise ContentError('动作模型引用缺少独立变体。')
                struct.pack_into('<H',program,position,remap[frame])
            cursor=entry['start'];scale=[1,1,1];boxes=[]
            while cursor+2<=entry['start']+entry['length']:
                op=bytes(program[cursor:cursor+2]);cursor+=2
                if op==b'CS':scale=list(struct.unpack_from('<3f',program,cursor))
                if op==b'XB':boxes.append(cursor)
                cursor+=SIZES[op]
                if op in (b'VO',b'2O'):break
            for position in boxes:
                box=list(struct.unpack_from('<8f',program,position))
                for i in range(3):box[i]=min(box[i],lo[i]*scale[i],hi[i]*scale[i]);box[i+4]=max(box[i+4],lo[i]*scale[i],hi[i]*scale[i])
                struct.pack_into('<8f',program,position,*box)
        struct.pack_into('<IIHH',actor_output,10+(actor-1)*12,len(program),len(actor_output),actor,0);actor_output.extend(program)
        bindings[edit['target_id']]=actor
    resources[ACT_PATH]={'source':original,'data':bytes(actor_output)}
    for name,data in index_outputs.items():
        if bytes(data)!=index_sources[name]:resources[name]={'source':index_sources[name],'data':bytes(data)}
    if len(resources)>256 or sum(len(r['data']) for r in resources.values())>64*1024*1024 or any(len(r['data'])>MAX_FILE_BYTES for r in resources.values()):raise ContentError('模型发布资源超过安全上限，请分批发布。')
    return resources,bindings
