import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {TransformControls} from 'three/addons/controls/TransformControls.js';
import {evaluate,transform,inverse,boneDelta,edges} from './model-operations.js';

const matrix = values => new THREE.Matrix4().set(...values);
const row = m => [0,1,2,3].flatMap(r => [0,1,2,3].map(c => m.elements[c*4+r]));
/** Dedicated disposable editing viewport; native coordinates are reflected only at the scene boundary. */
export class ModelViewport {
  constructor(host,{select,transform:changed,error}) {
    Object.assign(this,{host,onSelect:select,onTransform:changed,error});
    this.renderer=new THREE.WebGLRenderer({antialias:true});this.renderer.setPixelRatio(Math.min(devicePixelRatio,2));
    this.renderer.setClearColor(0x11191b);this.renderer.outputColorSpace=THREE.SRGBColorSpace;host.append(this.renderer.domElement);
    this.renderer.domElement.setAttribute('aria-label','模型编辑视口；点击选择，Shift拖动框选，Ctrl多选，右键平移');
    this.scene=new THREE.Scene();this.camera=new THREE.PerspectiveCamera(40,1,.001,100000);
    this.controls=new OrbitControls(this.camera,this.renderer.domElement);this.controls.addEventListener('change',()=>this.render());
    this.scene.add(new THREE.HemisphereLight(0xffffff,0x66777a,2.4));const light=new THREE.DirectionalLight(0xffffff,2);light.position.set(4,7,6);this.scene.add(light);
    this.root=new THREE.Group();this.root.scale.z=-1;this.scene.add(this.root);
    this.target=new THREE.Object3D();this.scene.add(this.target);
    this.gizmo=new TransformControls(this.camera,this.renderer.domElement);this.scene.add(this.gizmo.getHelper());this.gizmo.setSize(.8);
    this.gizmo.addEventListener('dragging-changed',e=>this.drag(e.value));
    this.gizmo.addEventListener('objectChange',()=>this.change());this.gizmo.addEventListener('change',()=>this.render());
    this.box=document.createElement('div');this.box.className='model-selection-box';this.box.hidden=true;host.append(this.box);
    this.events=new AbortController();const options={signal:this.events.signal};
    this.renderer.domElement.addEventListener('pointerdown',e=>this.pointerDown(e),options);
    this.renderer.domElement.addEventListener('pointermove',e=>this.pointerMove(e),options);
    this.renderer.domElement.addEventListener('pointerup',e=>this.pointerUp(e),options);
    this.observer=new ResizeObserver(()=>this.resize());this.observer.observe(host);this.ray=new THREE.Raycaster();this.ticket=0;
  }
  resize(){const {width,height}=this.host.getBoundingClientRect();this.renderer.setSize(width,height,false);this.camera.aspect=width/Math.max(height,1);this.camera.updateProjectionMatrix();this.render();}
  render(){if(this.renderer)this.renderer.render(this.scene,this.camera);}
  /** Rebuild topology only on structural changes; release every replaced GPU object. */
  clearGeometry(){
    this.ticket++;this.gizmo.detach();const geometries=new Set(),materials=new Set(),textures=new Set();
    this.root.traverse(o=>{if(o.geometry)geometries.add(o.geometry);for(const m of Array.isArray(o.material)?o.material:[o.material])if(m){materials.add(m);if(m.map)textures.add(m.map);}});
    for(const x of textures)x.dispose();for(const x of materials)x.dispose();for(const x of geometries)x.dispose();this.root.clear();
  }
  /** Activate one full native model and fit once, leaving subsequent edits at the user's camera. */
  setModel(model,doc,selection,mode='vertex') {
    this.clearGeometry();this.model=model;this.doc=doc;this.selection=selection;this.mode=mode;this.frame=0;
    this.build();this.update(doc,0,selection,mode);this.fit();
  }
  build(){
    const doc=this.doc;
    this.visibleFaces=doc.faces.filter(f=>!(this.model.material_flags?.[f.material]&0xff000000));
    this.geometry=new THREE.BufferGeometry();this.geometry.setAttribute('position',new THREE.Float32BufferAttribute(new Float32Array(this.visibleFaces.length*9),3));
    this.geometry.setAttribute('color',new THREE.Float32BufferAttribute(new Float32Array(this.visibleFaces.length*9),3));
    const vs=new Map(doc.vertices.map(v=>[v.id,v]));this.geometry.setAttribute('uv',new THREE.Float32BufferAttribute(this.visibleFaces.flatMap(f=>f.vertices.flatMap(v=>vs.get(v).uv)),2));
    this.materials=doc.materials.map(()=>new THREE.MeshStandardMaterial({color:0xffffff,vertexColors:true,side:THREE.DoubleSide,roughness:.9,alphaTest:.5}));
    for(let i=0;i<this.visibleFaces.length;i++)this.geometry.addGroup(i*3,3,this.visibleFaces[i].material);
    this.mesh=new THREE.Mesh(this.geometry,this.materials);this.root.add(this.mesh);
    this.pointGeometry=new THREE.BufferGeometry();this.pointGeometry.setAttribute('position',new THREE.Float32BufferAttribute(new Float32Array(doc.vertices.length*3),3));
    this.pointGeometry.setAttribute('color',new THREE.Float32BufferAttribute(new Float32Array(doc.vertices.length*3),3));
    this.points=new THREE.Points(this.pointGeometry,new THREE.PointsMaterial({size:5,sizeAttenuation:false,vertexColors:true,depthTest:false}));this.points.renderOrder=2;this.root.add(this.points);
    this.edgeList=[...edges(doc)];this.edgeGeometry=new THREE.BufferGeometry();this.edgeGeometry.setAttribute('position',new THREE.Float32BufferAttribute(new Float32Array(this.edgeList.length*6),3));
    this.edgeGeometry.setAttribute('color',new THREE.Float32BufferAttribute(new Float32Array(this.edgeList.length*6),3));
    this.edgeLines=new THREE.LineSegments(this.edgeGeometry,new THREE.LineBasicMaterial({vertexColors:true}));this.root.add(this.edgeLines);
    this.boneGroup=new THREE.Group();this.root.add(this.boneGroup);this.boneObjects=[];
    const sphere=new THREE.SphereGeometry(.02,10,8);
    for(const b of doc.bones){const o=new THREE.Mesh(sphere,new THREE.MeshBasicMaterial({color:0xded09d,depthTest:false}));o.renderOrder=4;o.userData.bone=b.id;this.boneObjects.push(o);this.boneGroup.add(o);}
    this.boneLines=new THREE.LineSegments(new THREE.BufferGeometry(),new THREE.LineBasicMaterial({color:0xa9cbd0,depthTest:false}));this.boneGroup.add(this.boneLines);
    this.structure=doc.vertices.map(v=>v.id).join()+':'+doc.faces.map(f=>f.id+f.vertices.join()+f.material).join()+':'+doc.bones.map(b=>b.id).join();
  }
  /** Update poses, skinning, selection and encoded textures without rebuilding the renderer. */
  update(doc,frame,selection,mode=this.mode,wire=true,bones=true){
    this.doc=doc;this.frame=frame;this.selection=selection;this.mode=mode;
    const structure=doc.vertices.map(v=>v.id).join()+':'+doc.faces.map(f=>f.id+f.vertices.join()+f.material).join()+':'+doc.bones.map(b=>b.id).join();
    if(structure!==this.structure){this.clearGeometry();this.build();}
    this.evaluated=evaluate(this.model,doc,frame);const positions=this.evaluated.positions;
    const selected=this.selectedVertices();
    const vertices=new Map(doc.vertices.map(v=>[v.id,v]));
    this.visibleFaces.forEach((f,i)=>f.vertices.forEach((key,j)=>{this.geometry.attributes.position.setXYZ(i*3+j,...positions.get(key));this.geometry.attributes.uv.setXY(i*3+j,...vertices.get(key).uv);this.geometry.attributes.color.setXYZ(i*3+j,...(selection.faces.has(f.id)?[1,.77,.35]:[1,1,1]));}));
    this.geometry.attributes.uv.needsUpdate=true;
    this.geometry.attributes.position.needsUpdate=this.geometry.attributes.color.needsUpdate=true;this.geometry.computeVertexNormals();this.geometry.computeBoundingSphere();
    doc.vertices.forEach((v,i)=>{this.pointGeometry.attributes.position.setXYZ(i,...positions.get(v.id));this.pointGeometry.attributes.color.setXYZ(i,...(selected.has(v.id)?[1,.8,.35]:[.55,.8,.84]));});
    this.pointGeometry.attributes.position.needsUpdate=this.pointGeometry.attributes.color.needsUpdate=true;this.pointGeometry.computeBoundingSphere();
    this.points.visible=mode==='vertex';
    this.edgeList.forEach(([key,ids],i)=>ids.forEach((v,j)=>{this.edgeGeometry.attributes.position.setXYZ(i*2+j,...positions.get(v));this.edgeGeometry.attributes.color.setXYZ(i*2+j,...(selection.edges.has(key)?[1,.8,.35]:[.3,.45,.49]));}));
    this.edgeGeometry.attributes.position.needsUpdate=this.edgeGeometry.attributes.color.needsUpdate=true;this.edgeGeometry.computeBoundingSphere();this.edgeLines.visible=wire||mode==='edge';
    const joints=new Map(doc.bones.map(b=>[b.id,transform(this.evaluated.matrices.get(b.id),b.pivot)]));
    for(const o of this.boneObjects){o.position.set(...joints.get(o.userData.bone));o.scale.setScalar(this.radius||1);o.material.color.set(selection.bones.has(o.userData.bone)?0xffb755:0xcde4e7);}
    const lines=doc.bones.filter(b=>b.parent!==null).flatMap(b=>[...joints.get(b.id),...joints.get(b.parent)]);
    if(this.boneLines.geometry.attributes.position?.array.length!==lines.length){this.boneLines.geometry.dispose();this.boneLines.geometry=new THREE.BufferGeometry();this.boneLines.geometry.setAttribute('position',new THREE.Float32BufferAttribute(lines,3));}
    else{this.boneLines.geometry.attributes.position.array.set(lines);this.boneLines.geometry.attributes.position.needsUpdate=true;}
    this.boneGroup.visible=bones||mode==='bone';
    this.texture(doc);if(!this.dragging)this.attach();this.render();
  }
  texture(doc){
    const ticket=this.ticket;
    doc.materials.forEach((m,i)=>{
      let url=m.image?`/api/model-edit/texture?image=${m.image}&tint=${m.tint.join(',')}`:m.texture>=0?`/api/model-edit/texture?texture=${m.texture}&tint=${m.tint.join(',')}`:null;
      if(url&&this.model.bundle_reference)url+=`&reference=${this.model.bundle_reference}`;
      const material=this.materials[i];if(material.userData.url===url)return;material.userData.url=url;
      if(material.map){material.map.dispose();material.map=null;material.needsUpdate=true;}
      if(url)new THREE.TextureLoader().load(url,t=>{if(ticket!==this.ticket||material.userData.url!==url){t.dispose();return;}t.flipY=false;t.colorSpace=THREE.SRGBColorSpace;t.wrapS=t.wrapT=THREE.RepeatWrapping;t.magFilter=THREE.NearestFilter;material.map=t;material.needsUpdate=true;this.render();},undefined,()=>{if(ticket===this.ticket)this.error('贴图预览读取失败，请核对来源或重新导入PNG。');});
    });
  }
  /** Frame the complete visible geometry, maintaining the native handedness boundary. */
  fit(){
    if(!this.mesh)return;this.root.position.set(0,0,0);this.root.updateMatrixWorld(true);const box=new THREE.Box3().setFromObject(this.mesh),center=box.getCenter(new THREE.Vector3()),size=box.getSize(new THREE.Vector3());
    this.root.position.sub(center);this.radius=Math.max(size.x,size.y,size.z,.01);this.camera.position.set(this.radius*.7,this.radius*.3,this.radius*1.8);this.camera.near=this.radius/1000;this.camera.far=this.radius*100;this.camera.updateProjectionMatrix();this.controls.target.set(0,0,0);this.controls.update();this.resize();this.attach();
  }
  selectedVertices(){
    if(this.mode==='face')return new Set(this.doc.faces.filter(f=>this.selection.faces.has(f.id)).flatMap(f=>f.vertices));
    if(this.mode==='edge')return new Set(this.edgeList.filter(([key])=>this.selection.edges.has(key)).flatMap(([,v])=>v));
    return this.selection.vertices;
  }
  attach(){
    if(!this.doc||this.dragging)return;
    let points=[];
    if(this.mode==='bone')points=this.doc.bones.filter(b=>this.selection.bones.has(b.id)).map(b=>transform(this.evaluated.matrices.get(b.id),b.pivot));
    else points=[...this.selectedVertices()].map(v=>this.evaluated.positions.get(v)).filter(Boolean);
    if(!points.length){this.gizmo.detach();return;}
    const p=new THREE.Vector3(...[0,1,2].map(i=>points.reduce((n,p)=>n+p[i],0)/points.length));this.root.updateMatrixWorld(true);this.root.localToWorld(p);
    this.target.position.copy(p);this.target.quaternion.identity();this.target.scale.set(1,1,1);this.target.updateMatrixWorld(true);this.gizmo.attach(this.target);
  }
  /** Convert gizmo movement from scene space back to skinned rest-space or one authored bone transform. */
  drag(active){
    this.dragging=active;this.controls.enabled=!active;
    if(active){this.dragDoc=structuredClone(this.doc);this.dragMatrix=this.target.matrixWorld.clone();this.dragEvaluation=this.evaluated;}
    else if(this.scratch){this.onTransform(this.scratch,true);this.scratch=null;this.attach();}
  }
  change(){
    if(!this.dragging||!this.dragDoc)return;
    try{
      const doc=structuredClone(this.dragDoc);this.target.updateMatrixWorld(true);this.root.updateMatrixWorld(true);
      const delta=this.target.matrixWorld.clone().multiply(this.dragMatrix.clone().invert());
      const native=this.root.matrixWorld.clone().invert().multiply(delta).multiply(this.root.matrixWorld),d=row(native);
      if(this.mode==='bone'){
        const key=[...this.selection.bones][0],b=doc.bones.find(b=>b.id===key);if(!b)return;
        const old=matrix(this.dragEvaluation.matrices.get(key)),before=matrix(boneDelta(b,this.frame));
        const prefix=old.clone().multiply(before.clone().invert());const next=prefix.invert().multiply(native).multiply(old);
        const p=new THREE.Vector3(),q=new THREE.Quaternion(),s=new THREE.Vector3();next.decompose(p,q,s);const e=new THREE.Euler().setFromQuaternion(q,'ZYX');
        const values={translation:[0,1,2].map(i=>p.getComponent(i)-b.pivot[i]+[0,1,2].reduce((n,c)=>n+next.elements[c*4+i]*b.pivot[c],0)),rotation:[e.x,e.y,e.z].map(v=>v*180/Math.PI),scale:s.toArray()};
        if(this.poseKey)b.keys[String(this.frame)]=values;else Object.assign(b,values);
      }else{
        for(const v of doc.vertices)if(this.selectedVertices().has(v.id)){
          const desired=transform(d,this.dragEvaluation.positions.get(v.id));
          v.position=transform(inverse(this.dragEvaluation.blends.get(v.id)),desired).map((x,i)=>x-this.dragEvaluation.drifts.get(v.id)[i]);
        }
      }
      this.scratch=doc;this.onTransform(doc,false);
    }catch(e){this.error(e.message);}
  }
  pointerDown(e){
    if(e.button!==0||this.gizmo.axis||this.dragging)return;
    this.pointer={x:e.clientX,y:e.clientY,shift:e.shiftKey,add:e.ctrlKey||e.metaKey};
    if(e.shiftKey){this.controls.enabled=false;this.box.hidden=false;this.pointerMove(e);}
  }
  pointerMove(e){if(!this.pointer?.shift)return;const r=this.host.getBoundingClientRect(),p=this.pointer;Object.assign(this.box.style,{left:`${Math.min(e.clientX,p.x)-r.left}px`,top:`${Math.min(e.clientY,p.y)-r.top}px`,width:`${Math.abs(e.clientX-p.x)}px`,height:`${Math.abs(e.clientY-p.y)}px`});}
  /** Resolve only the current element mode, with explicit additive and rectangle selection. */
  pointerUp(e){
    const p=this.pointer;this.pointer=null;this.box.hidden=true;if(!p)return;this.controls.enabled=true;if(this.dragging||this.gizmo.axis)return;
    const r=this.host.getBoundingClientRect(),hits=[];
    if(p.shift){
      const inside = position => {const v=this.root.localToWorld(new THREE.Vector3(...position)).project(this.camera),x=r.left+(v.x+1)*r.width/2,y=r.top+(1-v.y)*r.height/2;return v.z>=-1&&v.z<=1&&x>=Math.min(p.x,e.clientX)&&x<=Math.max(p.x,e.clientX)&&y>=Math.min(p.y,e.clientY)&&y<=Math.max(p.y,e.clientY);};
      if(this.mode==='vertex')for(const [id,pos] of this.evaluated.positions)if(inside(pos))hits.push(id);
      if(this.mode==='face')for(const f of this.visibleFaces)if(inside([0,1,2].map(i=>f.vertices.reduce((n,v)=>n+this.evaluated.positions.get(v)[i],0)/3)))hits.push(f.id);
      if(this.mode==='edge')for(const [key,vs] of this.edgeList)if(inside([0,1,2].map(i=>vs.reduce((n,v)=>n+this.evaluated.positions.get(v)[i],0)/2)))hits.push(key);
      if(this.mode==='bone')for(const o of this.boneObjects)if(inside(o.position.toArray()))hits.push(o.userData.bone);
    }else if(Math.hypot(e.clientX-p.x,e.clientY-p.y)<5){
      this.ray.setFromCamera(new THREE.Vector2((e.clientX-r.left)/r.width*2-1,-(e.clientY-r.top)/r.height*2+1),this.camera);
      this.ray.params.Points.threshold=(this.radius||1)/70;this.ray.params.Line.threshold=(this.radius||1)/120;
      const objects=this.mode==='bone'?this.boneObjects:[this.mode==='vertex'?this.points:this.mode==='edge'?this.edgeLines:this.mesh];const hit=this.ray.intersectObjects(objects,false)[0];
      if(hit){const key=this.mode==='bone'?hit.object.userData.bone:this.mode==='vertex'?this.doc.vertices[hit.index]?.id:this.mode==='edge'?this.edgeList[Math.floor(hit.index/2)]?.[0]:this.visibleFaces[hit.faceIndex]?.id;if(key)hits.push(key);}
    }else return;
    this.onSelect(this.mode,hits,p.add);
  }
  /** Close the editor without retaining WebGL contexts, listeners, observers or pending texture callbacks. */
  dispose(){this.events.abort();this.observer.disconnect();this.gizmo.detach();this.gizmo.dispose();this.controls.dispose();this.clearGeometry();this.renderer.dispose();this.renderer.forceContextLoss();this.renderer.domElement.remove();this.renderer=null;this.box.remove();}
}
