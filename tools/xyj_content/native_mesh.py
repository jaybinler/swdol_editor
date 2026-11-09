"""Lossless native PLY documents and editor-owned rigs baked into native per-pose vertices.

The native renderer at 0x4cff00 selects a group's optional 40-byte vertex records
before applying its 64-byte pose matrix. No unverified native bone/weight fields
are invented: authoring bones are retained in the document and baked on export.
"""

from copy import deepcopy
import math
import struct

from .binary import ContentError, compress, decompress, MAX_RAW_BYTES
from .models import Reader

IDENTITY = [1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.]
DOCUMENT = 'xyj-model-document/v1'


def decode(blob):
    """Read every matrix and morph block, retaining unrecognized trailing bytes verbatim."""
    if len(blob) < 8:
        raise ContentError('模型封装截断。')
    size, packed = struct.unpack_from('<II', blob)
    if packed != len(blob)-8:
        raise ContentError('模型压缩长度不符。')
    raw = decompress(blob[8:], size)
    r = Reader(raw)
    header = r.take(56)
    if struct.unpack_from('<I', header)[0] != len(raw):
        raise ContentError('模型内部长度不符。')
    groups = [list(r.unpack('7I')) for _ in range(r.count(1024))]
    vertices = [list(r.unpack('3fI5fI')) for _ in range(r.count(65536))]
    faces = [list(r.unpack('3H')) for _ in range(r.count(400000))]
    materials = [r.take(52) for _ in range(r.count(1024))]
    flags = [r.unpack('I')[0] for _ in range(r.count(400000))]
    poses = [[list(r.unpack('16f')) for _ in groups] for _ in range(r.count(10000))]
    morphs = []
    for _ in range(r.count(10000)):
        offsets = list(r.unpack(f'{len(groups)}I'))
        values = [list(r.unpack('3fI5fI')) for _ in range(r.count(65536))]
        morphs.append({'offsets': offsets, 'vertices': values})
    value = dict(header=header, groups=groups, vertices=vertices, faces=faces,
                 materials=materials, flags=flags, poses=poses, morphs=morphs, tail=raw[r.offset:])
    validate_native(value)
    return value


def validate_native(mesh):
    """Require complete bounded ranges before any document or resource is exposed."""
    groups, vertices, faces = mesh['groups'], mesh['vertices'], mesh['faces']
    if not groups or not vertices or not mesh['poses'] or len(mesh['flags']) != len(faces):
        raise ContentError('模型缺少分组、顶点或完整姿态。')
    if len(mesh['poses']) != len(mesh['morphs']):
        raise ContentError('模型矩阵与顶点姿态数量不一致。')
    assigned = set()
    for group in groups:
        _, count, first, end, count_f, first_f, end_f = group
        if first+count != end or end > len(vertices) or first_f+count_f != end_f or end_f > len(faces):
            raise ContentError('模型分组范围无效。')
        if assigned.intersection(range(first,end)):
            raise ContentError('模型顶点分组重叠。')
        assigned.update(range(first,end))
    if len(assigned) != len(vertices) or any(i >= len(vertices) for f in faces for i in f):
        raise ContentError('模型顶点归属或面引用无效。')
    for pose, morph in zip(mesh['poses'],mesh['morphs']):
        if len(pose) != len(groups) or any(not math.isfinite(x) or abs(x)>1e7 for m in pose for x in m):
            raise ContentError('模型姿态矩阵无效。')
        for offset, group in zip(morph['offsets'],groups):
            if offset != 0xffffffff and offset+group[1] > len(morph['vertices']):
                raise ContentError('模型顶点姿态范围无效。')


def encode(mesh):
    """Serialize the full native structure and losslessly check its compressed envelope."""
    validate_native(mesh)
    out = bytearray(mesh['header'])
    def array(values, fmt):
        out.extend(struct.pack('<I',len(values)))
        for value in values:
            out.extend(struct.pack('<'+fmt,*value) if isinstance(value,(list,tuple)) else struct.pack('<'+fmt,value))
    array(mesh['groups'],'7I'); array(mesh['vertices'],'3fI5fI'); array(mesh['faces'],'3H')
    out.extend(struct.pack('<I',len(mesh['materials'])))
    for value in mesh['materials']: out.extend(value)
    array(mesh['flags'],'I')
    out.extend(struct.pack('<I',len(mesh['poses'])))
    for pose in mesh['poses']:
        for matrix in pose: out.extend(struct.pack('<16f',*matrix))
    out.extend(struct.pack('<I',len(mesh['morphs'])))
    for morph in mesh['morphs']:
        out.extend(struct.pack('<'+'I'*len(mesh['groups']),*morph['offsets']))
        array(morph['vertices'],'3fI5fI')
    out.extend(mesh['tail'])
    if len(out)>MAX_RAW_BYTES: raise ContentError('完整模型超过原生资源安全上限。')
    struct.pack_into('<I',out,0,len(out))
    packed = compress(bytes(out))
    return struct.pack('<II',len(out),len(packed))+packed


def multiply(a,b):
    """Multiply row-major matrices using the native position convention."""
    return [sum(a[r*4+k]*b[k*4+c] for k in range(4)) for r in range(4) for c in range(4)]


def transform(matrix,point):
    """Apply an affine native matrix to one finite point."""
    return [sum(matrix[r*4+k]*point[k] for k in range(3))+matrix[r*4+3] for r in range(3)]


def inverse(matrix):
    """Invert a full matrix with pivoting; reject singular native/authoring transforms."""
    rows=[[float(matrix[r*4+c]) for c in range(4)]+[float(r==c) for c in range(4)] for r in range(4)]
    for c in range(4):
        p=max(range(c,4),key=lambda r:abs(rows[r][c]))
        if abs(rows[p][c])<1e-10: raise ContentError('模型包含不可逆的绑定／姿态矩阵。')
        rows[p],rows[c]=rows[c],rows[p]
        factor=rows[c][c];rows[c]=[v/factor for v in rows[c]]
        for r in range(4):
            if r!=c:
                factor=rows[r][c];rows[r]=[a-factor*b for a,b in zip(rows[r],rows[c])]
    return [v for row in rows for v in row[4:]]


def vertex_at(mesh,pose,group,index):
    """Follow exactly the renderer's base-versus-morph offset selection."""
    first=mesh['groups'][group][2];offset=mesh['morphs'][pose]['offsets'][group]
    return mesh['vertices'][index] if offset==0xffffffff else mesh['morphs'][pose]['vertices'][offset+index-first]


def document(mesh,frame,digest):
    """Create stable editing identities without mistaking native groups for an original skeleton."""
    vertices=[];bones=[]
    for g,group in enumerate(mesh['groups']):
        matrix=mesh['poses'][0][g]
        bones.append(dict(id=f'b{g}',name=f'分组 {g}',parent=None,source_group=g,
                          pivot=[matrix[3],matrix[7],matrix[11]],translation=[0,0,0],rotation=[0,0,0],scale=[1,1,1],keys={}))
        for index in range(group[2],group[3]):
            v=vertex_at(mesh,0,g,index)
            vertices.append(dict(id=f'v{index}',group=g,position=transform(matrix,v[:3]),uv=v[7:9],
                                 color=v[9],aux=v[3],origin=index,weights=[dict(bone=f'b{g}',value=1)]))
    owners={i:g for g,group in enumerate(mesh['groups']) for i in range(group[5],group[6])}
    material_of={i:m for m,mat in enumerate(mesh['materials'])
                 for i in range(struct.unpack_from('<i',mat,4)[0]//3,struct.unpack_from('<i',mat,8)[0]//3)}
    faces=[dict(id=f'f{i}',vertices=[f'v{v}' for v in face],group=owners.get(i,0),
                material=material_of.get(i,0),flags=mesh['flags'][i]) for i,face in enumerate(mesh['faces'])]
    materials=[dict(id=f'm{i}',texture=struct.unpack_from('<i',m,16)[0],tint=[1,1,1],image=None)
               for i,m in enumerate(mesh['materials'])]
    return dict(format=DOCUMENT,source_frame=frame,source_sha256=digest,vertices=vertices,faces=faces,
                bones=bones,materials=materials)


def _vector(value,n,label,limit=1e6):
    if not isinstance(value,list) or len(value)!=n or any(type(x) not in (float,int) or not math.isfinite(x) or abs(x)>limit for x in value):
        raise ContentError(label+'包含无效数值。')


def validate_document(doc,source):
    """Validate graph identities, topology, weights and all animation keys before baking."""
    if not isinstance(doc,dict) or set(doc)!={'format','source_frame','source_sha256','vertices','faces','bones','materials'} or doc['format']!=DOCUMENT:
        raise ContentError('模型编辑文档格式无效。')
    if type(doc['source_frame']) is not int or not 1<=doc['source_frame']<3000 or not isinstance(doc['source_sha256'],str) or len(doc['source_sha256'])!=64 or any(c not in '0123456789abcdef' for c in doc['source_sha256']):
        raise ContentError('模型来源编号或哈希无效。')
    if not isinstance(doc['vertices'],list) or not 1<=len(doc['vertices'])<=65535 or not isinstance(doc['faces'],list) or not 1<=len(doc['faces'])<=100000:
        raise ContentError('模型顶点或面数量超限。')
    if len(doc['vertices'])*len(source['poses'])>500000:
        raise ContentError('烘焙总顶点姿态超过 500000，请减少拓扑规模。')
    if not isinstance(doc['bones'],list) or not 1<=len(doc['bones'])<=256 or not isinstance(doc['materials'],list) or len(doc['materials'])!=len(source['materials']):
        raise ContentError('骨骼或材质数量无效。')
    def identities(values):
        ids=[v.get('id') for v in values if isinstance(v,dict)]
        if len(ids)!=len(values) or any(not isinstance(i,str) or not 1<=len(i)<=64 for i in ids) or len(set(ids))!=len(ids):
            raise ContentError('模型元素标识缺失或重复。')
        return set(ids)
    vertex_ids=identities(doc['vertices']);bone_ids=identities(doc['bones']);identities(doc['faces']);identities(doc['materials'])
    bones={b['id']:b for b in doc['bones']}
    for b in doc['bones']:
        if set(b)!={'id','name','parent','source_group','pivot','translation','rotation','scale','keys'} or not isinstance(b['name'],str) or len(b['name'])>100:
            raise ContentError('骨骼编辑字段无效。')
        if type(b['source_group']) is not int or not -1<=b['source_group']<len(source['groups']):raise ContentError('骨骼动作来源无效。')
        seen={b['id']};parent=b['parent']
        while parent is not None:
            if parent not in bone_ids or parent in seen:raise ContentError('骨骼层级包含循环或失效父级。')
            seen.add(parent);parent=bones[parent]['parent']
        _vector(b['pivot'],3,'关节位置')
        if not isinstance(b['keys'],dict) or len(b['keys'])>len(source['poses']):raise ContentError('骨骼关键帧无效。')
        for frame,values in [(None,b),*b['keys'].items()]:
            if frame is not None and (not frame.isdecimal() or str(int(frame))!=frame or not 0<=int(frame)<len(source['poses']) or not isinstance(values,dict) or set(values)!={'translation','rotation','scale'}):
                raise ContentError('骨骼关键帧越界。')
            for key in ('translation','rotation','scale'):_vector(values[key],3,'骨骼变换')
            if any(not .05<=s<=20 for s in values['scale']):raise ContentError('骨骼比例须为 0.05 至 20。')
    for v in doc['vertices']:
        if set(v)-{'morph_sources'}!={'id','group','position','uv','color','aux','origin','weights'} or type(v['group']) is not int or not 0<=v['group']<len(source['groups']):raise ContentError('顶点分组无效。')
        _vector(v['position'],3,'顶点');_vector(v['uv'],2,'UV')
        if type(v['aux']) is not int or not 0<=v['aux']<=0xffffffff:raise ContentError('顶点附加原始字段无效。')
        if type(v['color']) is not int or not 0<=v['color']<=0xffffffff:raise ContentError('顶点颜色无效。')
        if v['origin'] is not None and (type(v['origin']) is not int or not 0<=v['origin']<len(source['vertices'])):raise ContentError('顶点来源无效。')
        if 'morph_sources' in v:
            refs=v['morph_sources']
            if not isinstance(refs,list) or len(refs)>64 or any(not isinstance(w,dict) or set(w)!={'index','value'} or type(w['index']) is not int or not 0<=w['index']<len(source['vertices']) or type(w['value']) not in (int,float) or not math.isfinite(w['value']) or not 0<w['value']<=1 for w in refs) or sum(w['value'] for w in refs)>1.00001:
                raise ContentError('顶点原动作插值来源无效。')
        if not isinstance(v['weights'],list) or not 1<=len(v['weights'])<=4:raise ContentError('每个顶点须绑定 1 至 4 根骨骼。')
        if any(not isinstance(w,dict) or set(w)!={'bone','value'} or w['bone'] not in bone_ids or type(w['value']) not in (int,float) or not math.isfinite(w['value']) or not 0<w['value']<=1 for w in v['weights']):raise ContentError('顶点骨骼权重无效。')
        if len({w['bone'] for w in v['weights']})!=len(v['weights']) or abs(sum(w['value'] for w in v['weights'])-1)>1e-5:raise ContentError('顶点权重必须唯一且合计为 1。')
    populated={v['group'] for v in doc['vertices']}
    if any(g[1] and i not in populated for i,g in enumerate(source['groups'])):
        raise ContentError('不能清空原生分组的全部顶点；请保留绑定点或仅删除面。')
    for f in doc['faces']:
        if set(f)!={'id','vertices','group','material','flags'} or not isinstance(f['vertices'],list) or len(f['vertices'])!=3 or len(set(f['vertices']))!=3 or any(i not in vertex_ids for i in f['vertices']):raise ContentError('面包含重复或失效顶点。')
        if type(f['group']) is not int or not 0<=f['group']<len(source['groups']) or type(f['material']) is not int or not 0<=f['material']<len(doc['materials']) or type(f['flags']) is not int or not 0<=f['flags']<=0xffffffff:raise ContentError('面归属无效。')
    for m,old in zip(doc['materials'],source['materials']):
        if set(m)!={'id','texture','tint','image'} or type(m['texture']) is not int or m['texture']!=struct.unpack_from('<i',old,16)[0]:raise ContentError('材质来源引用不能直接改写。')
        _vector(m['tint'],3,'贴图颜色',4)
        if any(v<0 for v in m['tint']):raise ContentError('贴图颜色不能为负。')
        if m['image'] is not None and (not isinstance(m['image'],str) or len(m['image'])!=64 or any(c not in '0123456789abcdef' for c in m['image'])):raise ContentError('贴图草稿引用无效。')


def delta(bone,frame):
    """Build an authoring transform about the explicit rest-world joint pivot; keys override the default."""
    values=bone['keys'].get(str(frame),bone)
    x,y,z=[math.radians(v) for v in values['rotation']]
    cx,sx,cy,sy,cz,sz=math.cos(x),math.sin(x),math.cos(y),math.sin(y),math.cos(z),math.sin(z)
    rx=[1,0,0,0,0,cx,-sx,0,0,sx,cx,0,0,0,0,1]
    ry=[cy,0,sy,0,0,1,0,0,-sy,0,cy,0,0,0,0,1]
    rz=[cz,-sz,0,0,sz,cz,0,0,0,0,1,0,0,0,0,1]
    scale=IDENTITY.copy()
    for i,s in enumerate(values['scale']):scale[i*5]=s
    m=multiply(multiply(multiply(rz,ry),rx),scale)
    p=bone['pivot']
    for r in range(3):m[r*4+3]=p[r]+values['translation'][r]-sum(m[r*4+c]*p[c] for c in range(3))
    return m


def skin_matrices(doc,source,frame):
    """Retarget native absolute group motion through the editor hierarchy without inventing native bones."""
    bones={b['id']:b for b in doc['bones']};original={};result={}
    def original_motion(key):
        if key in original:return original[key]
        b=bones[key];g=b['source_group']
        original[key]=(original_motion(b['parent']) if b['parent'] is not None else IDENTITY) if g<0 else multiply(source['poses'][frame][g],inverse(source['poses'][0][g]))
        return original[key]
    for key in bones:original_motion(key)
    def visit(key):
        if key in result:return result[key]
        b=bones[key];m=original[key]
        if b['parent'] is not None:m=multiply(multiply(visit(b['parent']),inverse(original[b['parent']])),m)
        result[key]=multiply(m,delta(b,frame));return result[key]
    for key in bones:visit(key)
    return result


def posed_positions(doc,source,frame,matrices=None):
    """Evaluate skinned rest-world vertices for both preview and native baking."""
    matrices=skin_matrices(doc,source,frame) if matrices is None else matrices;points={}
    for v in doc['vertices']:
        # Preserve original morph displacement, expressed in rest-world coordinates.
        point=v['position'].copy()
        for ref in v.get('morph_sources',[] if v['origin'] is None else [{'index':v['origin'],'value':1}]):
            g=next(i for i,x in enumerate(source['groups']) if x[2]<=ref['index']<x[3])
            a=vertex_at(source,frame,g,ref['index']);b=vertex_at(source,0,g,ref['index'])
            drift=transform(source['poses'][0][g],[a[i]-b[i] for i in range(3)])
            for i in range(3):point[i]+=(drift[i]-source['poses'][0][g][i*4+3])*ref['value']
        points[v['id']]=[sum(transform(matrices[w['bone']],point)[i]*w['value'] for w in v['weights']) for i in range(3)]
    return points


def bake(doc,source,textures=None):
    """Compile topology and arbitrary authoring weights into verified native per-pose vertex blocks."""
    validate_document(doc,source)
    mesh=deepcopy(source)
    vertices=sorted(doc['vertices'],key=lambda v:v['group']);indices={v['id']:i for i,v in enumerate(vertices)}
    faces=sorted(doc['faces'],key=lambda f:(f['group'],f['material']))
    # 0x4d1362 reads normals at 0x10/0x14/0x18. Keep the word at 0x0c opaque.
    origins=[v['origin'] if v['origin'] is not None else source['groups'][v['group']][2] for v in vertices]
    aux=[source['vertices'][i][3] if i<len(source['vertices']) else 0 for i in origins]
    mesh['vertices']=[[0.,0.,0.,a,0.,0.,0.,*v['uv'],v['color']] for a,v in zip(aux,vertices)]
    mesh['faces']=[[indices[v] for v in f['vertices']] for f in faces];mesh['flags']=[f['flags'] for f in faces]
    mesh['materials']=[]
    for i,f in enumerate(faces):
        if not i or (faces[i-1]['group'],faces[i-1]['material'])!=(f['group'],f['material']):
            mat=bytearray(source['materials'][f['material']]);mesh['materials'].append(mat)
            struct.pack_into('<3i',mat,0,3,i*3,i*3+3)
            if textures and f['material'] in textures:struct.pack_into('<i',mat,16,textures[f['material']])
        else:
            mat=mesh['materials'][-1];count,start,_=struct.unpack_from('<3i',mat);struct.pack_into('<3i',mat,0,count+3,start,(i+1)*3)
    if len(mesh['materials'])>1024:raise ContentError('拓扑分组后的材质段超过原生限制。')
    first=face_first=0
    for g,group in enumerate(mesh['groups']):
        count=sum(v['group']==g for v in vertices);nf=sum(f['group']==g for f in faces)
        group[1:]=[count,first,first+count,nf,face_first,face_first+nf];first+=count;face_first+=nf
    mesh['morphs']=[];mesh['poses']=[];bounds=[[],[],[]]
    for frame in range(len(source['poses'])):
        matrices=skin_matrices(doc,source,frame)
        world=posed_positions(doc,source,frame,matrices)
        if any(not math.isfinite(x) or abs(x)>1e7 for point in world.values() for x in point):
            raise ContentError('烘焙姿态包含无效坐标或变换溢出。')
        pose=[]
        for g in range(len(source['groups'])):
            key=f'b{g}'
            if key in matrices:motion=matrices[key]
            else:
                first=next((v for v in vertices if v['group']==g),None)
                motion=[sum(matrices[w['bone']][i]*w['value'] for w in first['weights']) for i in range(16)] if first else IDENTITY
            pose.append(multiply(motion,source['poses'][0][g]))
        mesh['poses'].append(pose)
        inverses=[inverse(m) if any(v['group']==g for v in vertices) else IDENTITY for g,m in enumerate(pose)]
        world_points=[world[v['id']] for v in vertices]
        local=[transform(inverses[v['group']],p) for v,p in zip(vertices,world_points)]
        normals=[[0.,0.,0.] for _ in vertices]
        for a,b,c in mesh['faces']:
            u=[world_points[b][i]-world_points[a][i] for i in range(3)];v=[world_points[c][i]-world_points[a][i] for i in range(3)]
            n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]
            for index in (a,b,c):normals[index]=[x+y for x,y in zip(normals[index],n)]
        data=[]
        for i,v in enumerate(vertices):
            matrix=pose[v['group']]
            n=[sum(matrix[r*4+c]*normals[i][r] for r in range(3)) for c in range(3)]
            length=math.sqrt(sum(x*x for x in n)) or 1
            data.append([*local[i],aux[i],*(x/length for x in n),*v['uv'],v['color']])
            for axis in range(3):bounds[axis].append(world[v['id']][axis])
        if not frame:mesh['vertices']=deepcopy(data)
        mesh['morphs'].append(dict(offsets=[g[2] for g in mesh['groups']],vertices=data))
    lo=[min(a) for a in bounds];hi=[max(a) for a in bounds];center=[(a+b)/2 for a,b in zip(lo,hi)]
    header=bytearray(mesh['header']);struct.pack_into('<3f',header,4,*lo);struct.pack_into('<3f',header,20,*hi)
    struct.pack_into('<3f',header,36,*center);struct.pack_into('<f',header,52,math.sqrt(sum((b-a)**2 for a,b in zip(lo,hi)))/2)
    mesh['header']=bytes(header)
    encoded=encode(mesh)
    decode(encoded)
    return encoded
