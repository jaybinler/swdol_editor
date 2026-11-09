/** Native-compatible row-major transforms and bounded, atomic model authoring commands. */
export const identity = () => [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1];
/** Multiply two native affine matrices. */
export const multiply = (a,b) => Array.from({length:16},(_,i) => Array.from({length:4},(_,k) => a[Math.floor(i/4)*4+k]*b[k*4+i%4]).reduce((x,y) => x+y,0));
/** Apply the native row-major position convention. */
export const transform = (m,p) => [0,1,2].map(r => m[r*4]*p[0]+m[r*4+1]*p[1]+m[r*4+2]*p[2]+m[r*4+3]);
/** Invert with pivoting and report singular bind transforms instead of producing NaNs. */
export function inverse(m) {
  const rows = [0,1,2,3].map(r => [...m.slice(r*4,r*4+4),...[0,1,2,3].map(c => +(r===c))]);
  for (let c=0;c<4;c++) {
    const p = [c,...Array.from({length:3-c},(_,i) => c+1+i)].reduce((p,r) => Math.abs(rows[r][c])>Math.abs(rows[p][c])?r:p,c);
    if (Math.abs(rows[p][c])<1e-10) throw Error('绑定矩阵不可逆。');
    [rows[p],rows[c]]=[rows[c],rows[p]]; const f=rows[c][c]; rows[c]=rows[c].map(x => x/f);
    for (let r=0;r<4;r++) if(r!==c) {const f=rows[r][c];rows[r]=rows[r].map((x,i) => x-f*rows[c][i]);}
  }
  return rows.flatMap(r => r.slice(4));
}
/** Build the same rest-world pivot transform used by the resource compiler. */
export function boneDelta(b,frame) {
  const v=b.keys[String(frame)] || b;
  const [x,y,z]=v.rotation.map(x => x*Math.PI/180), [cx,sx,cy,sy,cz,sz]=[Math.cos(x),Math.sin(x),Math.cos(y),Math.sin(y),Math.cos(z),Math.sin(z)];
  const rx=[1,0,0,0,0,cx,-sx,0,0,sx,cx,0,0,0,0,1],ry=[cy,0,sy,0,0,1,0,0,-sy,0,cy,0,0,0,0,1],rz=[cz,-sz,0,0,sz,cz,0,0,0,0,1,0,0,0,0,1];
  const s=identity();v.scale.forEach((x,i) => s[i*5]=x);
  const m=multiply(multiply(multiply(rz,ry),rx),s);
  for(let r=0;r<3;r++) m[r*4+3]=b.pivot[r]+v.translation[r]-[0,1,2].reduce((n,c) => n+m[r*4+c]*b.pivot[c],0);
  return m;
}
/** Evaluate all authoring bones and vertices using the original absolute animation matrices. */
export function evaluate(model,doc,frame) {
  const bones=new Map(doc.bones.map(b => [b.id,b])), original=new Map(), matrices=new Map();
  const originalMotion=(key,seen=new Set())=>{if(original.has(key))return original.get(key);const b=bones.get(key);if(!b||seen.has(key))throw Error('骨骼层级无效。');seen.add(key);const m=b.source_group<0?(b.parent!==null?originalMotion(b.parent,seen):identity()):multiply(model.poses[frame][b.source_group],inverse(model.poses[0][b.source_group]));original.set(key,m);return m;};
  for(const b of doc.bones)originalMotion(b.id);
  const visit = (key,seen=new Set()) => {
    if(matrices.has(key)) return matrices.get(key);
    const b=bones.get(key);if(!b || seen.has(key)) throw Error('骨骼父级失效或循环。');seen.add(key);
    let m=original.get(key);
    if(b.parent!==null) m=multiply(multiply(visit(b.parent,new Set(seen)),inverse(original.get(b.parent))),m);
    m=multiply(m,boneDelta(b,frame));matrices.set(key,m);return m;
  };
  for(const b of doc.bones) visit(b.id);
  const drifts=new Map(),positions=new Map(),blends=new Map();
  for(const v of doc.vertices) {
    const drift=[0,0,0], origins=v.morph_sources || (v.origin===null?[]:[{index:v.origin,value:1}]);
    for(const ref of origins) {
      const g=model.source_groups.findIndex(g => ref.index>=g[2] && ref.index<g[3]);
      const source = p => {const m=model.morphs[p],o=m.offsets[g];return o===4294967295?model.source_vertices[ref.index]:m.vertices[o+ref.index-model.source_groups[g][2]];};
      const a=source(frame),b=source(0),m=model.poses[0][g];
      const d=transform(m,a.map((x,i) => x-b[i]));for(let i=0;i<3;i++) drift[i]+=(d[i]-m[i*4+3])*ref.value;
    }
    const blend=Array(16).fill(0);
    for(const w of v.weights) matrices.get(w.bone).forEach((x,i) => blend[i]+=x*w.value);
    drifts.set(v.id,drift);blends.set(v.id,blend);positions.set(v.id,transform(blend,v.position.map((x,i) => x+drift[i])));
  }
  return {positions,matrices,drifts,blends};
}
/** Reject broken topology/rig state before a command reaches the undo stack. Server validation remains authoritative. */
export function validate(doc) {
  if(!doc.vertices.length || doc.vertices.length>65535 || !doc.faces.length || doc.faces.length>100000 || !doc.bones.length || doc.bones.length>256) throw Error('顶点、面或骨骼数量超限，不能删除全部模型。');
  for(const values of [doc.vertices,doc.faces,doc.bones]) if(new Set(values.map(v => v.id)).size!==values.length) throw Error('元素编号重复。');
  const vs=new Set(doc.vertices.map(v => v.id)),bs=new Map(doc.bones.map(b => [b.id,b]));
  for(const f of doc.faces) if(f.vertices.length!==3 || new Set(f.vertices).size!==3 || f.vertices.some(v => !vs.has(v))) throw Error('面包含失效或重复顶点。');
  for(const b of doc.bones) {
    const seen=new Set([b.id]);let p=b.parent;
    while(p!==null) {if(!bs.has(p)||seen.has(p)) throw Error('骨骼不能形成循环。');seen.add(p);p=bs.get(p).parent;}
    for(const values of [b,...Object.values(b.keys)]) if([...values.translation,...values.rotation,...values.scale].some(x => !Number.isFinite(x)) || values.scale.some(x => x<.05||x>20)) throw Error('骨骼变换数值无效。');
  }
  for(const v of doc.vertices) {
    if([...v.position,...v.uv].some(x => !Number.isFinite(x)||Math.abs(x)>1e6)) throw Error('顶点或UV数值无效。');
    if(!v.weights.length||v.weights.length>4||new Set(v.weights.map(w=>w.bone)).size!==v.weights.length||v.weights.some(w => !bs.has(w.bone)||!Number.isFinite(w.value)||w.value<=0)||Math.abs(v.weights.reduce((s,w)=>s+w.value,0)-1)>1e-5) throw Error('权重须绑定有效骨骼，最多4根，合计为1。');
  }
}
/** Atomic history: failed commands preserve document, redo stack and the saved reference. */
export class ModelHistory {
  constructor(doc) {this.value=structuredClone(doc);this.undoStack=[];this.redoStack=[];this.saved=JSON.stringify(doc);this.bytes=0;}
  execute(label,fn) {
    const next=structuredClone(this.value);fn(next);validate(next);
    const size=JSON.stringify(this.value).length*2;
    if(size>16*1024*1024)throw Error('文档超过单步撤销容量，请减少模型规模。');
    if(JSON.stringify(next)===JSON.stringify(this.value)) return false;
    this.undoStack.push({label,value:this.value,size});this.bytes+=size;
    while(this.undoStack.length>50||this.bytes>16*1024*1024) this.bytes-=this.undoStack.shift().size;
    this.value=next;this.redoStack=[];return true;
  }
  undo() {const x=this.undoStack.pop();if(!x)return;this.redoStack.push({label:x.label,value:this.value,size:x.size});this.bytes-=x.size;this.value=x.value;}
  redo() {const x=this.redoStack.pop();if(!x)return;this.undoStack.push({label:x.label,value:this.value,size:x.size});this.bytes+=x.size;this.value=x.value;}
  get dirty() {return JSON.stringify(this.value)!==this.saved;}
  markSaved() {this.saved=JSON.stringify(this.value);}
}
const id = prefix => prefix+crypto.randomUUID().replaceAll('-','');
const average = values => values[0].map((_,i)=>values.reduce((s,v)=>s+v[i],0)/values.length);
function interpolate(vertices) {
  const v=structuredClone(vertices[0]);v.id=id('v');v.origin=null;v.position=average(vertices.map(v=>v.position));v.uv=average(vertices.map(v=>v.uv));
  const weights=new Map(),origins=new Map();
  for(const source of vertices) {
    for(const w of source.weights) weights.set(w.bone,(weights.get(w.bone)||0)+w.value/vertices.length);
    for(const w of source.morph_sources||(source.origin===null?[]:[{index:source.origin,value:1}])) origins.set(w.index,(origins.get(w.index)||0)+w.value/vertices.length);
  }
  if(weights.size>4)throw Error('新顶点会超过4根骨骼绑定，请先简化权重。');
  if(origins.size>64)throw Error('新顶点的原动作插值来源过多。');
  v.weights=[...weights].map(([bone,value])=>({bone,value}));v.morph_sources=[...origins].map(([index,value])=>({index,value}));return v;
}
/** Return unique edges with stable endpoint identities. */
export function edges(doc) {
  const result=new Map();for(const f of doc.faces) for(let i=0;i<3;i++) {const v=[f.vertices[i],f.vertices[(i+1)%3]].sort();result.set(v.join('|'),v);}return result;
}
/** Apply one supported topology operation, maintaining faces, UVs, weights and morph interpolation. */
export function topology(doc,command,selection,options={}) {
  const vs=doc.vertices.filter(v=>selection.vertices.has(v.id)),fs=doc.faces.filter(f=>selection.faces.has(f.id));
  if(command==='vertex') {
    const v=vs.length?interpolate(vs):structuredClone(doc.vertices[0]);v.id=id('v');v.origin=null;v.position=vs.length?v.position:[0,0,0];doc.vertices.push(v);
  } else if(command==='face') {
    if(vs.length!==3)throw Error('新增面需要选中3个顶点。');doc.faces.push({id:id('f'),vertices:vs.map(v=>v.id),group:vs[0].group,material:options.material||0,flags:0});
  } else if(command==='delete') {
    if(selection.edges.size){const selected=[...selection.edges].map(key=>key.split('|'));doc.faces=doc.faces.filter(f=>!selected.some(pair=>pair.every(v=>f.vertices.includes(v))));}
    else if(selection.faces.size)doc.faces=doc.faces.filter(f=>!selection.faces.has(f.id));
    else {doc.vertices=doc.vertices.filter(v=>!selection.vertices.has(v.id));doc.faces=doc.faces.filter(f=>f.vertices.every(v=>!selection.vertices.has(v)));}
  } else if(command==='merge') {
    if(vs.length<2)throw Error('合并需要至少2个顶点。');
    const v=interpolate(vs),removed=new Set(vs.map(v=>v.id));doc.vertices=doc.vertices.filter(v=>!removed.has(v.id));doc.vertices.push(v);
    doc.faces=doc.faces.map(f=>({...f,vertices:f.vertices.map(x=>removed.has(x)?v.id:x)})).filter(f=>new Set(f.vertices).size===3);
  } else if(command==='split') {
    if(selection.edges.size) {
      for(const key of selection.edges) {
        const pair=edges(doc).get(key);if(!pair)continue;
        const v=interpolate(pair.map(i=>doc.vertices.find(v=>v.id===i)));doc.vertices.push(v);
        doc.faces=doc.faces.flatMap(f=>{
          for(let i=0;i<3;i++) {const [a,b,c]=[f.vertices[i],f.vertices[(i+1)%3],f.vertices[(i+2)%3]];
            if(pair.includes(a)&&pair.includes(b))return [{...f,vertices:[a,v.id,c]},{...f,id:id('f'),vertices:[v.id,b,c]}];}
          return [f];
        });
      }
    } else {
      if(!fs.length)throw Error('分割需要选择边或面。');
      const replacements=new Map();for(const f of fs){const v=interpolate(f.vertices.map(i=>doc.vertices.find(v=>v.id===i)));doc.vertices.push(v);replacements.set(f.id,[0,1,2].map(i=>({...f,id:i?id('f'):f.id,vertices:[f.vertices[i],f.vertices[(i+1)%3],v.id]})));}
      doc.faces=doc.faces.flatMap(f=>replacements.get(f.id)||[f]);
    }
  } else if(command==='extrude') {
    if(!fs.length)throw Error('挤出需要选择面。');
    const distance=Number(options.distance);if(!Number.isFinite(distance)||Math.abs(distance)>10000)throw Error('挤出距离无效。');
    const ids=new Set(fs.flatMap(f=>f.vertices)),map=new Map(),normal=[0,0,0],boundary=new Map();
    for(const f of fs){const [a,b,c]=f.vertices.map(i=>doc.vertices.find(v=>v.id===i).position),u=b.map((x,i)=>x-a[i]),v=c.map((x,i)=>x-a[i]);const n=[u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]];n.forEach((x,i)=>normal[i]+=x);
      for(let i=0;i<3;i++){const a=f.vertices[i],b=f.vertices[(i+1)%3],key=[a,b].sort().join('|');const prior=boundary.get(key);boundary.set(key,prior?{...prior,count:prior.count+1}:{a,b,count:1,face:f});}}
    const length=Math.hypot(...normal);if(length<1e-9)throw Error('选中面的挤出方向无法确定。');
    for(const key of ids){const v=structuredClone(doc.vertices.find(v=>v.id===key));v.id=id('v');v.position=v.position.map((x,i)=>x+normal[i]/length*distance);doc.vertices.push(v);map.set(key,v.id);}
    doc.faces=doc.faces.filter(f=>!selection.faces.has(f.id));
    for(const f of fs)doc.faces.push({...f,id:id('f'),vertices:f.vertices.map(v=>map.get(v))});
    for(const {a,b,count,face} of boundary.values())if(count===1){doc.faces.push({...face,id:id('f'),vertices:[a,b,map.get(b)]},{...face,id:id('f'),vertices:[a,map.get(b),map.get(a)]});}
  } else throw Error('未支持的拓扑操作。');
}
/** Remove a rig bone and consistently redirect children and vertex weights. */
export function removeBone(doc,key) {
  if(doc.bones.length===1)throw Error('至少保留1根编辑骨骼。');
  const b=doc.bones.find(b=>b.id===key);if(!b)return;const replacement=b.parent||doc.bones.find(b=>b.id!==key).id;
  doc.bones=doc.bones.filter(b=>b.id!==key);for(const child of doc.bones)if(child.parent===key)child.parent=b.parent;
  for(const v of doc.vertices){const weights=new Map();for(const w of v.weights){const id=w.bone===key?replacement:w.bone;weights.set(id,(weights.get(id)||0)+w.value);}v.weights=[...weights].map(([bone,value])=>({bone,value}));}
}
