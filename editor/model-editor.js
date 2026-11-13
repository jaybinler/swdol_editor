import {createElement} from 'react';
import {createRoot} from 'react-dom/client';
import {flushSync} from 'react-dom';
import {ModelStudio} from './model-studio.jsx';
import {ModelHistory,topology,removeBone,boneDelta,transform,validate,evaluate} from './model-operations.js';
import {ModelViewport} from './model-viewport.js';

const $=id=>document.getElementById(id);
const emptySelection=()=>({vertices:new Set(),edges:new Set(),faces:new Set(),bones:new Set()});
const stable=value=>JSON.stringify(value,(_key,v)=>v && typeof v==='object' && !Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b))):v);
const vector=(prefix,values)=>values.forEach((v,i)=>$(`${prefix}-${i}`).value=Number(v.toFixed(5)));
const readVector=prefix=>[0,1,2].map(i=>Number($(`${prefix}-${i}`).value));

/** Full in-workshop authoring controller; immutable model references attach to ordinary monster drafts. */
export class ModelEditor {
  constructor({api,confirmDiscard,onSaved}) {
    Object.assign(this,{api,confirmDiscard,onSaved});this.busy=false;this.mode='vertex';this.selection=emptySelection();
    this.dialog=document.createElement('dialog');this.dialog.id='model-studio';this.dialog.className='model-studio';this.dialog.setAttribute('aria-labelledby','model-studio-title');
    document.body.append(this.dialog);
    const root = createRoot(this.dialog);
    flushSync(() => root.render(createElement(ModelStudio)));
    this.bind();
  }
  get dirty(){return this.busy||this.models?.some(m=>m.history.dirty)||false;}
  get current(){return this.models?.[this.index];}
  get doc(){return this.scratch||this.current.history.value;}
  message(text,error=false){$('model-studio-message').textContent=text;$('model-studio-message').classList.toggle('error',error);}
  bind(){
    $('model-close').onclick=()=>this.close();this.dialog.addEventListener('cancel',e=>{e.preventDefault();this.close();});
    $('model-save').onclick=()=>this.save();$('model-undo').onclick=()=>{this.stop();this.current.history.undo();this.selection=emptySelection();this.refresh();};$('model-redo').onclick=()=>{this.stop();this.current.history.redo();this.selection=emptySelection();this.refresh();};
    $('model-modes').onclick=e=>{const b=e.target.closest('[data-mode]');if(b)this.setMode(b.dataset.mode);};
    $('model-transforms').onclick=e=>{const b=e.target.closest('[data-transform]');if(b){this.viewport.gizmo.setMode(b.dataset.transform);for(const button of $('model-transforms').children)button.setAttribute('aria-pressed',String(button===b));}};
    $('model-fit').onclick=()=>this.viewport.fit();$('model-all').onclick=()=>this.pick(this.mode,this.mode==='bone'?this.doc.bones.map(b=>b.id):this.mode==='edge'?[...this.viewport.edgeList].map(([id])=>id):this.mode==='face'?this.doc.faces.map(f=>f.id):this.doc.vertices.map(v=>v.id),false);
    $('model-none').onclick=()=>{this.selection=emptySelection();this.refresh();};
    $('model-asset').onchange=()=>this.activate(Number($('model-asset').value));
    $('model-action').onchange=()=>{this.stop();this.actionCursor=0;this.showActionPose();};
    $('model-play').onclick=()=>this.playing?this.stop():this.play();$('model-frame').oninput=()=>{this.stop();this.frame=Number($('model-frame').value);this.refresh();};
    $('model-wire').onchange=$('model-show-bones').onchange=()=>this.draw();
    $('model-apply-transform').onclick=()=>this.edit('变换选区',doc=>{const ids=this.vertexIds();if(!ids.size)throw Error('请先选择顶点、边或面。');const selected=doc.vertices.filter(v=>ids.has(v.id));const pivot=[0,1,2].map(i=>selected.reduce((n,v)=>n+v.position[i],0)/selected.length);const m=boneDelta({pivot,translation:readVector('model-move'),rotation:readVector('model-rotate'),scale:readVector('model-scale'),keys:{}},0);for(const v of selected)v.position=transform(m,v.position);});
    $('model-set-position').onclick=()=>this.edit('设置顶点坐标',doc=>{const ids=this.vertexIds(),vs=doc.vertices.filter(v=>ids.has(v.id));if(!vs.length)throw Error('请先选择顶点。');const p=readVector('model-position'),center=[0,1,2].map(i=>vs.reduce((s,v)=>s+v.position[i],0)/vs.length);for(const v of vs)v.position=v.position.map((x,i)=>x+p[i]-center[i]);});
    this.dialog.querySelectorAll('[data-operation]').forEach(b=>b.onclick=()=>this.operate(b.dataset.operation));$('model-extrude').onclick=()=>this.operate('extrude');
    $('model-set-uv').onclick=()=>this.edit('设置UV',doc=>{const ids=this.vertexIds();if(!ids.size)throw Error('请先选择顶点。');for(const v of doc.vertices)if(ids.has(v.id))v.uv=[Number($('model-u').value),Number($('model-v').value)];});
    $('model-bone').onchange=()=>this.pick('bone',[$('model-bone').value],false);$('model-add-bone').onclick=()=>this.addBone();$('model-remove-bone').onclick=()=>this.edit('删除骨骼',doc=>removeBone(doc,$('model-bone').value));
    $('model-set-bone').onclick=()=>this.setBone();$('model-pose-key').onchange=()=>{this.viewport.poseKey=$('model-pose-key').checked;this.inspectBone();};
    $('model-delete-key').onclick=()=>this.edit('移除姿态修改',doc=>{delete doc.bones.find(b=>b.id===$('model-bone').value).keys[String(this.frame)];});
    $('model-add-weight').onclick=()=>{if($('model-weights').children.length>=4)return this.message('最多绑定4根骨骼。',true);this.weightRow({bone:$('model-bone').value,value:0});};
    $('model-set-weights').onclick=()=>this.edit('绑定顶点权重',doc=>{const ids=this.vertexIds();if(!ids.size)throw Error('请先选择顶点。');const weights=[...$('model-weights').children].map(row=>({bone:row.querySelector('select').value,value:Number(row.querySelector('input').value)}));for(const v of doc.vertices)if(ids.has(v.id))v.weights=structuredClone(weights);});
    $('model-material').onchange=()=>this.inspectMaterial();$('model-assign-material').onclick=()=>this.edit('设置面材质',doc=>{if(!this.selection.faces.size)throw Error('请先选择面。');for(const f of doc.faces)if(this.selection.faces.has(f.id))f.material=Number($('model-material').value);});
    $('model-apply-tint').onclick=()=>this.edit('贴图调色',doc=>{const tint=[0,1,2].map(i=>Number($(`model-tint-${i}`).value));if(tint.some(x=>!Number.isFinite(x)||x<0||x>4))throw Error('颜色比例须为0至4。');doc.materials[Number($('model-material').value)].tint=tint;});
    $('model-restore-texture').onclick=()=>this.edit('恢复贴图',doc=>Object.assign(doc.materials[Number($('model-material').value)],{tint:[1,1,1],image:null}));
    $('model-png').onchange=()=>this.importPng();$('model-download-texture').onclick=()=>this.download();$('model-source').onclick=()=>this.fresh();
    this.dialog.addEventListener('keydown',e=>{if(e.target.matches('input,select,textarea')||this.busy)return;if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='z'){e.preventDefault();(e.shiftKey?$('model-redo'):$('model-undo')).click();}if(e.key==='Delete'){e.preventDefault();this.operate('delete');}});
  }
  option(select,value,label){const o=document.createElement('option');o.value=value;o.textContent=label;select.append(o);}
  async open(template,reference=null,fresh=false){
    if(this.busy)return;
    this.template=template;this.busy=true;
    try{
      this.data=await this.api(`/api/model-edit/open?template_id=${template}${reference?`&reference=${reference}`:''}${fresh?'&fresh=1':''}`);
      this.models=this.data.models.map(m=>({...m,history:new ModelHistory(m.document)}));this.reference=this.data.reference;this.selection=emptySelection();this.frame=0;
      $('model-studio-title').textContent=`怪物 #${template} · 模型编辑`;
      $('model-asset').replaceChildren();this.models.forEach((m,i)=>this.option($('model-asset'),i,m.file));
      $('model-action').replaceChildren();this.option($('model-action'),'', '全部姿态');for(const a of this.data.actions.filter(a=>a.poses.length))this.option($('model-action'),a.selector,`动作 ${a.selector} · ${a.poses.length} 个姿态`);
      if(!this.dialog.open)this.dialog.showModal();this.viewport?.dispose();
      this.viewport=new ModelViewport($('model-canvas'),{select:(...a)=>this.pick(...a),error:e=>this.message(e,true),transform:(doc,final)=>{this.scratch=doc;if(final){this.scratch=null;this.edit('拖动变换',next=>Object.assign(next,doc));}else this.draw();}});
      this.activate(Math.max(0,this.models.findIndex(m=>m.frame===this.data.default_frame)));this.message('已读取全部姿态。编辑骨骼将烘焙到原生分组与顶点姿态，原游戏资源保持独立。');$('model-close').focus();
    }catch(e){this.onError?.(e.message);if(this.dialog.open)this.message(e.message,true);else throw e;}
    finally{this.busy=false;this.enable();}
  }
  activate(index){this.stop();this.index=index;this.frame=0;this.scratch=null;this.selection=emptySelection();$('model-asset').value=index;this.viewport.setModel(this.current,this.doc,this.selection,this.mode);this.refresh();}
  setMode(mode){this.stop();this.mode=mode;for(const b of $('model-modes').children)b.setAttribute('aria-pressed',String(b.dataset.mode===mode));if(mode==='bone')$('model-bone-panel').open=true;this.refresh();}
  pick(mode,keys,add){this.stop();this.mode=mode;const field={vertex:'vertices',edge:'edges',face:'faces',bone:'bones'}[mode];if(!add)this.selection[field].clear();for(const key of keys){if(add&&this.selection[field].has(key))this.selection[field].delete(key);else this.selection[field].add(key);}if(mode==='bone'&&this.selection.bones.size>1)this.selection.bones=new Set([keys.at(-1)]);this.setMode(mode);}
  vertexIds(){return this.mode==='bone'?this.selection.vertices:this.viewport.selectedVertices();}
  edit(label,fn){if(this.busy||this.viewport?.dragging)return;this.stop();try{this.current.history.execute(label,doc=>{fn(doc);validate(doc);const groups=new Set(doc.vertices.map(v=>v.group));if(this.current.source_groups.some((g,i)=>g[1]&&!groups.has(i)))throw Error('不能清空原生分组的全部顶点；请保留绑定点或仅删除面。');const posed=evaluate(this.current,doc,this.frame);if([...posed.positions.values()].some(p=>p.some(v=>!Number.isFinite(v)||Math.abs(v)>1e7)))throw Error('变换超出原生坐标范围。');});this.scratch=null;this.refresh();this.message(`${label}完成，${this.dirty?'模型尚未保存':'模型已保存'}。`);}catch(e){this.scratch=null;this.refresh();this.message(e.message,true);}}
  operate(command){const selection={vertices:new Set(this.mode==='vertex'?this.selection.vertices:[]),edges:new Set(this.mode==='edge'?this.selection.edges:[]),faces:new Set(this.mode==='face'?this.selection.faces:[])};this.edit('拓扑修改',doc=>topology(doc,command,selection,{distance:$('model-extrude-distance').value,material:Number($('model-material').value)}));}
  draw(){if(this.current)this.viewport.update(this.doc,this.frame,this.selection,this.mode,$('model-wire').checked,$('model-show-bones').checked);}
  refresh(){
    if(!this.current)return;
    const doc=this.doc,valid=new Set(doc.vertices.map(v=>v.id));this.selection.vertices=new Set([...this.selection.vertices].filter(v=>valid.has(v)));
    this.selection.faces=new Set([...this.selection.faces].filter(id=>doc.faces.some(f=>f.id===id)));this.selection.bones=new Set([...this.selection.bones].filter(id=>doc.bones.some(b=>b.id===id)));
    $('model-counts').textContent=`${doc.vertices.length} 顶点 · ${doc.faces.length} 三角面 · ${this.current.poses.length} 姿态`;
    $('model-frame').max=this.current.poses.length-1;$('model-frame').value=this.frame;$('model-frame-number').textContent=`${this.frame+1} / ${this.current.poses.length}`;
    this.draw();const ids=this.vertexIds(),vs=doc.vertices.filter(v=>ids.has(v.id));$('model-selection').textContent=`已选 ${vs.length} 顶点、${this.selection.faces.size} 面、${this.selection.bones.size} 骨骼`;
    if(vs.length){vector('model-position',[0,1,2].map(i=>vs.reduce((s,v)=>s+v.position[i],0)/vs.length));$('model-u').value=vs[0].uv[0];$('model-v').value=vs[0].uv[1];}
    const bone=$('model-bone').value||[...this.selection.bones][0]||doc.bones[0].id;
    $('model-bone').replaceChildren();for(const b of doc.bones)this.option($('model-bone'),b.id,b.name);
    $('model-bone').value=[...this.selection.bones][0]||bone;if(!$('model-bone').value)$('model-bone').value=doc.bones[0].id;
    $('model-bone-list').replaceChildren(...doc.bones.map(b=>{const button=document.createElement('button');button.textContent=b.name;button.style.paddingLeft=b.parent?'22px':'10px';button.setAttribute('aria-pressed',String(this.selection.bones.has(b.id)));button.onclick=()=>this.pick('bone',[b.id],false);return button;}));
    this.inspectBone();$('model-weights').replaceChildren();if(vs.length)for(const w of vs[0].weights)this.weightRow(w);
    const mat=$('model-material').value;$('model-material').replaceChildren();doc.materials.forEach((m,i)=>this.option($('model-material'),i,`材质 ${i+1}${m.texture>=0?` · 贴图 ${m.texture}`:' · 无来源贴图'}`));
    $('model-material').value=mat||String(Math.max(0,doc.materials.findIndex(m=>m.texture>=0)));this.inspectMaterial();this.enable();
  }
  inspectBone(){if(!this.current)return;const b=this.doc.bones.find(b=>b.id===$('model-bone').value);if(!b)return;
    $('model-bone-name').value=b.name;$('model-parent').replaceChildren();this.option($('model-parent'),'','无父级');for(const other of this.doc.bones)if(other.id!==b.id)this.option($('model-parent'),other.id,other.name);$('model-parent').value=b.parent||'';
    $('model-source-group').replaceChildren();this.option($('model-source-group'),-1,'静态／由父级驱动');this.current.source_groups.forEach((_,i)=>this.option($('model-source-group'),i,`原生分组 ${i}`));$('model-source-group').value=b.source_group;
    vector('model-pivot',b.pivot);const v=$('model-pose-key').checked?(b.keys[String(this.frame)]||b):b;vector('model-bone-position',v.translation);vector('model-bone-rotation',v.rotation);vector('model-bone-scale',v.scale);
  }
  setBone(){this.edit('编辑骨骼',doc=>{const b=doc.bones.find(b=>b.id===$('model-bone').value);b.name=$('model-bone-name').value;b.parent=$('model-parent').value||null;b.source_group=Number($('model-source-group').value);b.pivot=readVector('model-pivot');const values={translation:readVector('model-bone-position'),rotation:readVector('model-bone-rotation'),scale:readVector('model-bone-scale')};if($('model-pose-key').checked)b.keys[String(this.frame)]=values;else Object.assign(b,values);});}
  addBone(){const key='b'+crypto.randomUUID().replaceAll('-','');this.edit('新增骨骼',doc=>{doc.bones.push({id:key,name:`编辑骨骼 ${doc.bones.length+1}`,parent:[...this.selection.bones][0]||null,source_group:-1,pivot:[0,0,0],translation:[0,0,0],rotation:[0,0,0],scale:[1,1,1],keys:{}});});if(this.doc.bones.some(b=>b.id===key))this.pick('bone',[key],false);}
  weightRow(w){const row=document.createElement('div');row.className='model-weight';const select=document.createElement('select');select.setAttribute('aria-label','绑定骨骼');for(const b of this.doc.bones)this.option(select,b.id,b.name);select.value=w.bone;const input=document.createElement('input');Object.assign(input,{type:'number',min:0,max:1,step:.05,value:w.value});input.setAttribute('aria-label','绑定权重');const remove=document.createElement('button');remove.textContent='移除';remove.onclick=()=>row.remove();row.append(select,input,remove);$('model-weights').append(row);}
  textureUrl(){const m=this.doc.materials[Number($('model-material').value)];let url=m?.image?`/api/model-edit/texture?image=${m.image}&tint=${m.tint.join(',')}`:m?.texture>=0?`/api/model-edit/texture?texture=${m.texture}&tint=${m.tint.join(',')}`:null;if(url&&this.reference)url+=`&reference=${this.reference}`;return url;}
  inspectMaterial(){if(!this.current)return;const m=this.doc.materials[Number($('model-material').value)];if(!m)return;m.tint.forEach((x,i)=>$(`model-tint-${i}`).value=x);const image=$('model-texture-preview'),url=this.textureUrl();image.hidden=!url;if(url)image.src=url;else image.removeAttribute('src');}
  enable(){for(const el of this.dialog.querySelectorAll('button,input,select'))el.disabled=this.busy;if(this.current&&!this.busy){$('model-undo').disabled=!this.current.history.undoStack.length;$('model-redo').disabled=!this.current.history.redoStack.length;}}
  stop(){this.playing=false;if(this.raf)cancelAnimationFrame(this.raf);this.raf=null;$('model-play').textContent='播放';$('model-play').setAttribute('aria-pressed','false');if(this.viewport)this.viewport.gizmo.enabled=true;this.enable();}
  showActionPose(){const a=this.data.actions.find(a=>String(a.selector)===$('model-action').value);if(!a?.poses.length){this.frame=(this.frame+1)%this.current.poses.length;this.draw();return;}
    const [frame,pose]=a.poses[this.actionCursor%a.poses.length],index=this.models.findIndex(m=>m.frame===frame);if(index<0)return;if(index!==this.index){const playing=this.playing;this.activate(index);this.playing=playing;if(playing)this.playControls();}
    if(pose>=this.current.poses.length)throw Error('动作姿态引用超出模型范围。');this.frame=pose;this.draw();
  }
  playControls(){$('model-play').textContent='暂停';$('model-play').setAttribute('aria-pressed','true');this.viewport.gizmo.enabled=false;for(const control of this.dialog.querySelectorAll('.model-inspector button,.model-inspector input,.model-inspector select,#model-save'))control.disabled=true;}
  play(){if(this.busy||this.viewport.dragging)return;this.playing=true;this.actionCursor=0;this.lastFrame=0;this.playControls();
    const tick=time=>{if(!this.playing)return;const fps=Math.min(60,Math.max(1,Number($('model-fps').value)||10));if(time-this.lastFrame>=1000/fps){this.lastFrame=time;try{this.showActionPose();this.actionCursor++;$('model-frame').value=this.frame;$('model-frame-number').textContent=`${this.frame+1} / ${this.current.poses.length}`;}catch(e){this.stop();this.message(e.message,true);return;}}this.raf=requestAnimationFrame(tick);};this.raf=requestAnimationFrame(tick);
  }
  async upload(bytes){const hash=await crypto.subtle.digest('SHA-256',bytes),key=[...new Uint8Array(hash)].map(b=>b.toString(16).padStart(2,'0')).join('');let progress=await this.api(`/api/model-edit/blob-status?id=${key}`),offset=progress.bytes;
    while(!progress.complete&&offset<bytes.length){const part=bytes.slice(offset,offset+400000);let binary='';for(let i=0;i<part.length;i+=8192)binary+=String.fromCharCode(...part.slice(i,i+8192));
      try{progress=await this.api('/api/model-edit/upload',{id:key,offset,total:bytes.length,data:btoa(binary)});}catch(e){progress=await this.api(`/api/model-edit/blob-status?id=${key}`);if(progress.bytes<=offset&&!progress.complete)throw e;}
      offset=progress.bytes;this.message(`保存资源 ${Math.round(offset/bytes.length*100)}%`);
    }if(!progress.complete)throw Error('资源上传未完成，请重试。');return key;
  }
  async importPng(){const file=$('model-png').files[0];if(!file||this.busy)return;const selected=Number($('model-material').value);this.stop();this.busy=true;this.enable();let bitmap;
    try{if(file.size>20*1024*1024||file.size<24)throw Error('PNG文件须在20 MiB以内。');const header=new Uint8Array(await file.slice(0,24).arrayBuffer()),view=new DataView(header.buffer);if(header.slice(0,8).join()!=='137,80,78,71,13,10,26,10'||view.getUint32(16)>2048||view.getUint32(20)>2048)throw Error('仅支持不超过2048×2048的PNG。');
      bitmap=await createImageBitmap(file);const canvas=document.createElement('canvas');canvas.width=bitmap.width;canvas.height=bitmap.height;const context=canvas.getContext('2d',{willReadFrequently:true});context.drawImage(bitmap,0,0);const pixels=context.getImageData(0,0,bitmap.width,bitmap.height).data,bytes=new Uint8Array(pixels.length+12),v=new DataView(bytes.buffer);bytes.set([82,71,66,65]);v.setUint32(4,bitmap.width,true);v.setUint32(8,bitmap.height,true);bytes.set(pixels,12);
      const key=await this.upload(bytes);this.current.history.execute('导入PNG',doc=>doc.materials[selected].image=key);this.message('PNG已导入，预览显示实际游戏量化结果；模型尚未保存。');
    }catch(e){this.message(e.message,true);}finally{bitmap?.close();this.busy=false;$('model-png').value='';this.refresh();}
  }
  async download(){try{const url=this.textureUrl();if(!url)throw Error('该材质没有贴图。');const response=await fetch(url);if(!response.ok)throw Error('PNG导出失败。');const blob=await response.blob(),link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download=`怪物${this.template}-材质${Number($('model-material').value)+1}.png`;link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);}catch(e){this.message(e.message,true);}}
  async save(){if(this.busy||this.viewport.dragging)return;this.stop();this.busy=true;this.enable();
    try{const documents=[];for(const m of this.models)documents.push(await this.upload(new TextEncoder().encode(JSON.stringify(m.history.value))));
      let result;try{result=await this.api('/api/model-edit/save',{template_id:this.template,revision:this.data.revision,actor_sha256:this.data.actor_sha256,source_stamp:this.data.source_stamp,base_ref:this.reference,documents});}
      catch(e){const recovered=await this.api(`/api/model-edit/open?template_id=${this.template}`);if(recovered.revision!==this.data.revision+1||stable(recovered.models.map(m=>m.document))!==stable(this.models.map(m=>m.history.value)))throw e;result={reference:recovered.reference,revision:recovered.revision};}
      this.data.revision=result.revision;this.reference=result.reference;for(const m of this.models){m.history.markSaved();m.bundle_reference=result.reference;}this.onSaved(result.reference,this.template);this.message('模型草稿已保存。返回怪物后点击“暂存怪物”，再导出并覆盖双端。');
    }catch(e){this.message(`保存失败：${e.message} 当前编辑仍保留。`,true);}finally{this.busy=false;this.refresh();}
  }
  async fresh(){if(this.busy||!await this.confirmDiscard('重新读取会放弃当前模型编辑，已保存资源仍保留。'))return;await this.open(this.template,null,true);}
  async close(){if(this.busy||this.viewport?.dragging)return this.message('请等待当前操作完成。',true);if(this.dirty&&!await this.confirmDiscard('返回将放弃未保存的模型修改；已保存的模型草稿保留。'))return;this.stop();this.viewport?.dispose();this.viewport=null;this.models=null;this.dialog.close();}
}
