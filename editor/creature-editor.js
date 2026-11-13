const $ = id => document.getElementById(id);
const keys = ['source_id','target_id','name','description','level','sex','respawn_seconds','appearance_id',
  'scale','attributes','drops','skills','reset_candidates','spawns'];

/** Extract a draft without carrying read-only metadata or mutating a catalog response. */
export function creatureOperation(detail) {
  const value = Object.fromEntries(keys.map(key => [key, structuredClone(detail[key])]));
  value.drops = value.drops.map(d => ({id: d.id, threshold: d.threshold}));
  if(detail.model_ref)value.model_ref=detail.model_ref;
  return value;
}

/** Quantize a requested percentage using the server-provided native probability table. */
export function nearestProbability(value, probabilities) {
  if (!Number.isFinite(value) || value < 0 || value > 100) throw new Error('掉率须为 0 至 100。');
  return probabilities.reduce((best, row) => Math.abs(row.percent-value) < Math.abs(best.percent-value) ? row : best);
}

/** Own NPC forms and drafts separately from item state; every game string is rendered as text. */
export class CreatureEditor {
  constructor({api, node, status, confirmDiscard, confirmPublish, canPublish = () => true, applied, getViewer,
               openProperties = () => $('npc-name').focus(), openPreview = () => {}}) {
    Object.assign(this, {api, node, status, confirmDiscard, confirmPublish, canPublish, applied, getViewer, openProperties, openPreview});
    this.dirty = false; this.ticket = 0; this.seedTicket = 0; this.previewTicket = 0; this.busy = false;
    $('npc-edit').addEventListener('click', () => this.begin(false));
    $('npc-clone').addEventListener('click', () => this.begin(true));
    const pointFields = new Set(['npc-point-map','npc-point-seed','npc-point-action','npc-point-count','npc-point-x','npc-point-y','npc-point-z','npc-point-dir']);
    const changed = event => {
      this.dirty = this.mode !== 'view';
      if(event.target.id==='npc-appearance' && Number(event.target.value)!==this.value?.appearance_id && this.value?.model_ref){delete this.value.model_ref;this.modelNote();}
      if (pointFields.has(event.target.id)) { this.pointDirty = true; this.pointPending(); }
    };
    $('npc-form').addEventListener('input', event => { changed(event); this.timing(); });
    $('npc-form').addEventListener('change', changed);
    $('npc-reset-point').addEventListener('click', () => { this.seedPosition(); this.pointDirty = false; this.pointPending(); });
    $('npc-form').addEventListener('submit', event => { event.preventDefault(); this.stage(); });
    $('npc-restore').addEventListener('click', () => this.restore());
    $('npc-view').addEventListener('click', () => this.view());
    $('npc-reload').addEventListener('click', () => this.reload());
    $('npc-export').addEventListener('click', () => this.export());
    $('npc-preview').addEventListener('click', () => this.preview());
    $('npc-model-edit').addEventListener('click',()=>this.editModel());
    $('npc-model-reset').addEventListener('click',()=>{delete this.value.model_ref;this.value.appearance_id=this.detail.source_id;$('npc-appearance').value=this.detail.source_id;['x','y','z'].forEach(a=>$(`npc-scale-${a}`).value=1);this.dirty=true;this.modelNote();});
    $('npc-point-map').addEventListener('change', () => this.seeds());
    $('npc-point-action').addEventListener('change', () => this.seeds());
    $('npc-point-seed').addEventListener('change', () => this.seedPosition());
    $('npc-add-point').addEventListener('click', () => this.addPoint());
  }

  /** Refuse selection changes while an async save runs; ask before discarding unsaved input. */
  async canSelect(id) {
    if (this.busy) return false;
    return !this.dirty || this.detail?.source_id === id || await this.confirmDiscard('切换怪物将放弃未暂存的表单输入；已暂存怪物草稿保留。');
  }

  /** Load one immutable template and optional authoring metadata, rejecting stale responses. */
  async select(id) {
    if (this.dirty && this.detail?.source_id === id) return false;
    const ticket = ++this.ticket;
    this.previewTicket++;
    this.loading = true;
    $('npc-edit').disabled = $('npc-clone').disabled = true;
    $('npc-fields').disabled = true;
    try {
      const [detail, meta] = await Promise.all([this.api(`/api/creature-edit/${id}`), this.meta ? Promise.resolve(this.meta) : this.api('/api/creature-editor')]);
      if (ticket !== this.ticket) return false;
      this.detail = detail; this.meta = meta; this.mode = 'view'; this.dirty = false; this.draftTarget = null; this.baseline = null;
      this.choices(); this.fill(creatureOperation(detail)); this.renderDrafts();
      $('npc-edit').disabled = $('npc-clone').disabled = !detail.editable || !meta.native_verified;
      $('npc-fields').disabled = true;
      $('npc-form-title').textContent = `查看怪物 #${id}`;
      $('npc-mode-note').textContent = detail.readonly_reason || `当前模板 #${id}；${detail.points.length} 个直接存档。新增地图变体会使用不同编号。`;
      this.error('');
      return true;
    } catch (error) { if (ticket === this.ticket) this.error(`模板 #${id} 读取失败：${error.message}。请重新选择或读取草稿。`); return false; }
    finally { if (ticket === this.ticket) this.loading = false; }
  }

  /** Populate native searchable option lists without interpreting names as HTML. */
  choices() {
    for (const [id, values] of [['npc-appearance-options',this.meta.appearances],['npc-item-options',this.meta.items],['npc-skill-options',this.meta.skills]]) {
      $(id).replaceChildren(...values.map(row => { const option = this.node('option'); option.value = row.id; option.label = row.name; return option; }));
    }
    $('npc-point-map').replaceChildren(...this.meta.maps.map(row => {
      const option = this.node('option', `${row.name}（${row.id}）`); option.value = row.id; return option;
    }));
  }

  /** Enter original-ID editing or an explicit new variant, keeping reset candidates visible. */
  async begin(clone) {
    if (!this.detail || this.busy || this.loading || this.dirty && !await this.confirmDiscard('此操作将放弃本次未暂存输入。')) return;
    let value = creatureOperation(this.detail);
    if (clone) {
      const occupied = new Set([...this.meta.availability.baseline_ids, ...this.meta.edits.map(e => e.target_id)]);
      const target = Array.from({length:32767}, (_,i) => i+1).find(id => !occupied.has(id) && !this.meta.availability.draft_ids.includes(id));
      if (!target) return this.error('没有可用的怪物编号。');
      value.target_id = target; value.reset_candidates = [-1,-1,-1,-1,-1]; value.spawns = [];
    } else value = this.meta.edits.find(e => e.target_id === value.target_id) || value;
    this.mode = clone ? 'clone' : 'edit'; this.fill(value); this.baseline = structuredClone(value); this.dirty = false;
    this.draftTarget = clone ? null : value.target_id;
    $('npc-fields').disabled = false; $('npc-target').readOnly = !clone;
    $('npc-form-title').textContent = clone ? `新增地图变体（来源 #${value.source_id}）` : `编辑怪物 #${value.target_id}`;
    $('npc-mode-note').textContent = clone ? '新编号与原怪物独立。默认保持自身刷新；请添加地图生成点。' : '模板属性作用于所有使用该编号的对象；不同地图独立间隔请使用新增地图变体。';
    $('npc-name').focus();
  }

  /** Populate all raw slots so empty, unknown and duplicate references survive an untouched save. */
  fill(value) {
    this.pointDirty = false; this.pointPending();
    this.value = structuredClone(value); this.pointChanges = structuredClone(value.spawns);
    this.modelNote();
    for (const [id,key] of [['npc-target','target_id'],['npc-name','name'],['npc-description','description'],['npc-level','level'],['npc-sex','sex'],['npc-respawn','respawn_seconds'],['npc-appearance','appearance_id']]) $(id).value = value[key];
    for (const [index,axis] of ['x','y','z'].entries()) $(`npc-scale-${axis}`).value = value.scale[index];
    const input = (id, value, {list, min=-32768, max=32767, step=1} = {}) => {
      const field = this.node('input'); Object.assign(field, {id, type:'number', min, max, step, value, required:true}); if (list) field.setAttribute('list',list); return field;
    };
    const label = (name, field) => { const el = this.node('label',name); el.append(field); return el; };
    $('npc-attributes').replaceChildren(...this.meta.attributes.map(row => label(row.name,input(`npc-attr-${row.key}`,value.attributes[row.key]))));
    $('npc-candidates').replaceChildren(...value.reset_candidates.map((id,i) => label(`候选 ${i+1}（-1 为自身）`,input(`npc-candidate-${i}`,id,{min:-1,list:'npc-appearance-options'}))));
    $('npc-skill-fields').replaceChildren(...value.skills.map((id,i) => label(`技能 ${i+1}`,input(`npc-skill-${i}`,id,{min:-1,list:'npc-skill-options'}))));
    $('npc-drop-fields').replaceChildren(...value.drops.map((drop,i) => {
      const row = this.node('div',undefined,'npc-field-grid');
      const item = input(`npc-drop-${i}`,drop.id,{min:-1,list:'npc-item-options'});
      const p = this.meta.probabilities.find(p => p.threshold === drop.threshold);
      const percent = input(`npc-percent-${i}`,p?.percent ?? this.detail.drops[i].percent,{min:0,max:100,step:'any'});
      percent.dataset.threshold = drop.threshold;
      const actual = this.node('span',`${Number(percent.value).toFixed(4)}%`,'property-note');
      percent.addEventListener('change', () => {
        try { const probability = nearestProbability(Number(percent.value),this.meta.probabilities); percent.dataset.threshold = probability.threshold; percent.value = probability.percent; actual.textContent = `${probability.percent.toFixed(4)}%`; this.error(''); }
        catch (error) { this.error(error.message); }
      });
      const rate = label('掉率（%）',percent); rate.append(actual); row.append(label(`槽 ${i+1} · 物品编号`,item),rate); return row;
    }));
    const first = this.detail.points[0];
    if (first && this.meta.maps.some(m => m.id === first.map_id)) $('npc-point-map').value = first.map_id;
    this.renderPoints(); this.seeds(); this.timing();
  }

  /** Read only the supported operation schema; server-side validation remains authoritative. */
  read() {
    const value = structuredClone(this.value);
    for (const [id,key] of [['npc-target','target_id'],['npc-level','level'],['npc-sex','sex'],['npc-respawn','respawn_seconds'],['npc-appearance','appearance_id']]) value[key] = Number($(id).value);
    value.name = $('npc-name').value; value.description = $('npc-description').value;
    value.scale = ['x','y','z'].map(axis => Number($(`npc-scale-${axis}`).value));
    value.attributes = Object.fromEntries(this.meta.attributes.map(row => [row.key,Number($(`npc-attr-${row.key}`).value)]));
    value.drops = value.drops.map((_,i) => ({id:Number($(`npc-drop-${i}`).value), threshold:Number($(`npc-percent-${i}`).dataset.threshold)}));
    value.skills = value.skills.map((_,i) => Number($(`npc-skill-${i}`).value));
    value.reset_candidates = value.reset_candidates.map((_,i) => Number($(`npc-candidate-${i}`).value));
    value.spawns = structuredClone(this.pointChanges); return value;
  }

  /** Open protected, full native model editing without staging or publishing unrelated form fields. */
  async editModel(){
    if(this.busy||this.loading||this.mode==='view')return;
    try{
      const appearance=Number($('npc-appearance').value);
      const direct=appearance===this.value.appearance_id?this.value.model_ref:null;
      const project=!direct&&appearance===this.detail.source_id?this.detail.model_project:null;
      const factors=['x','y','z'].map((a,i)=>Number($(`npc-scale-${a}`).value)*(project?.scale?.[i]||1));
      this.modelScale=factors;
      if(!this.modelEditor){const {ModelEditor}=await import('./model-editor.js');this.modelEditor=new ModelEditor({api:this.api,confirmDiscard:this.confirmDiscard,onSaved:(reference,source)=>{this.value.model_ref=reference;this.value.appearance_id=source;$('npc-appearance').value=source;['x','y','z'].forEach((a,i)=>$(`npc-scale-${a}`).value=this.modelScale[i]);this.dirty=true;this.modelNote();}});}
      await this.modelEditor.open(project?.appearance_id||appearance,direct||project?.reference||null);
    }catch(error){this.error(`模型编辑未打开：${error.message}`);}
  }

  /** Explain whether the form refers to an immutable model document awaiting monster staging. */
  modelNote(){
    if(!$('npc-model-state'))return;
    $('npc-model-state').textContent=this.value?.model_ref?'已绑定独立模型草稿；暂存怪物并导出后生效。':this.detail?.model_project?'已发布的模型项目可继续编辑，保留骨骼、权重和材质草稿。':'使用原有模型；可修改顶点、拓扑、骨骼和贴图，生成独立资源。';
    $('npc-model-reset').hidden=!this.value?.model_ref;
  }

  /** Show the native random range separately from the corpse stage and runtime tick assumptions. */
  timing() {
    const ticks = Number($('npc-respawn').value)*10;
    $('npc-timing').textContent = Number.isFinite(ticks) && ticks >= 7 ? `默认节拍下随机等待 ${(Math.floor(ticks*80/100)/10).toLocaleString()} 至 ${(Math.floor(ticks*119/100)/10).toLocaleString()} 秒，另有约 60 秒尸体阶段。` : '请输入有效基础间隔。';
  }

  /** Load bounded same-map point sources; old responses cannot replace a newer map selection. */
  async seeds() {
    if (!this.meta || !this.detail) return;
    const ticket = ++this.seedTicket;
    try {
      let rows;
      if ($('npc-point-action').value === 'add') {
        const data = await this.api(`/api/creature-seeds?map=${$('npc-point-map').value}&source=${this.detail.source_id}`); rows = data.points;
      } else rows = this.detail.points.map(p => ({...p,name:this.detail.name}));
      if (ticket !== this.seedTicket) return;
      this.seedRows = rows;
      $('npc-point-seed').replaceChildren(...rows.map(row => { const option = this.node('option',`${row.key} · ${row.name || this.detail.name} · 来源地图 ${row.map_id}`); option.value = row.key; return option; }));
      const own = rows.find(r => this.detail.points.some(p => p.key === r.key));
      if (own) $('npc-point-seed').value = own.key;
      this.seedPosition();
    } catch (error) { if (ticket === this.seedTicket) { this.seedRows = []; $('npc-point-seed').replaceChildren(); this.error(error.message); } }
  }

  /** Copy coordinates from the selected saved point, leaving all changes explicit in the point list. */
  seedPosition() {
    const point = this.seedRows?.find(r => r.key === $('npc-point-seed').value);
    if (point) for (const [i,axis] of ['x','y','z'].entries()) $(`npc-point-${axis}`).value = point.position[i];
    $('npc-point-source').textContent = point ? `来源地图 ${point.map_id}。${point.plots ? `继承脚本：自动 ${point.plots[0]}，接触 ${point.plots[1]}，范围 ${point.plots[2]}，死亡 ${point.plots[3]}。` : '脚本信息以原存档为准。'} 请确认目标坐标可通行，工具尚未校验地形。` : '请选择可用的存档来源。';
  }

  /** Add one bounded point operation to the form, before staging any database plan. */
  addPoint() {
    if (!this.seedRows?.length) return this.error('该地图没有可用存档来源，请先选择其他地图。');
    const action = $('npc-point-action').value;
    if (this.mode === 'clone' && action !== 'add') return this.error('新变体须新增自己的点，不能移动或删除来源怪物的点。');
    const point = {action,key:$('npc-point-seed').value,map_id:Number($('npc-point-map').value),
      position:['x','y','z'].map(axis => Number($(`npc-point-${axis}`).value)),
      count:Number($('npc-point-count').value),direction:Number($('npc-point-dir').value)};
    if (this.pointChanges.length >= 200) return this.error('点位修改最多 200 项。');
    this.pointChanges.push(point); this.dirty = true; this.pointDirty = false; this.pointPending(); this.renderPoints(); this.error('');
  }

  /** Render removable point changes without embedding opaque database rows in the browser. */
  renderPoints() {
    $('npc-point-changes').replaceChildren(...this.pointChanges.map((point,index) => {
      const row = this.node('div',undefined,'npc-draft-row');
      row.append(this.node('span',`${{add:'新增',update:'移动',remove:'移除'}[point.action]} · 地图 ${point.map_id} · ${point.position.join(' / ')} · ${point.count} 个`));
      const remove = this.node('button','移除修改'); remove.type = 'button';
      remove.addEventListener('click',() => { this.pointChanges.splice(index,1); this.dirty = true; this.renderPoints(); }); row.append(remove); return row;
    }));
  }

  /** Commit a normalized draft, preserving input on validation or network failure. */
  async stage() {
    if (this.busy || !this.meta || !this.mode || this.mode === 'view') return;
    if (this.pointDirty) { this.error('地图输入尚未加入点位修改，请先加入，或放弃本次点位输入。'); $('npc-add-point').focus(); return; }
    this.busy = true; $('npc-stage').disabled = true; $('npc-fields').disabled = true;
    try {
      const value = this.read();
      const edits = this.meta.edits.filter(e => e.target_id !== (this.draftTarget ?? value.target_id)); edits.push(value);
      const data = await this.api('/api/creature-draft',{revision:this.meta.revision,edits});
      Object.assign(this.meta,data); const saved = data.edits.find(e => e.target_id === value.target_id);
      this.draftTarget = value.target_id; this.baseline = structuredClone(saved); this.value = structuredClone(saved); this.dirty = false;
      this.renderDrafts(); this.error(''); this.status(`怪物 #${value.target_id} 已暂存，游戏文件尚未修改。`);
    } catch (error) { this.error(error.message); }
    finally { this.busy = false; $('npc-stage').disabled = false; $('npc-fields').disabled = this.mode === 'view'; }
  }

  /** Keep saved variants visible and reopen their original source independently of the current browser filter. */
  renderDrafts() {
    $('npc-draft-count').textContent = `${this.meta.edits.length} 项`;
    $('npc-export').disabled = !this.meta.edits.length || this.meta.export_mode !== 'automatic';
    $('npc-draft-list').replaceChildren(...this.meta.edits.map(edit => {
      const row = this.node('div',undefined,'npc-draft-row');
      const open = this.node('button',`#${edit.target_id} ${edit.name} · 来源 #${edit.source_id}`); open.type = 'button';
      open.addEventListener('click',async () => {
        if (this.busy || this.dirty && !await this.confirmDiscard('重开草稿会放弃当前未暂存输入。')) return;
        this.dirty = false;
        const ticket = this.ticket + 1;
        if (!await this.select(edit.source_id) || ticket !== this.ticket) return;
        this.mode = edit.target_id === edit.source_id ? 'edit' : 'clone';
        this.draftTarget = edit.target_id; this.baseline = structuredClone(edit); this.fill(edit);
        $('npc-fields').disabled = false; $('npc-target').readOnly = this.mode !== 'clone';
        $('npc-form-title').textContent = `怪物草稿 #${edit.target_id}（来源 #${edit.source_id}）`; this.dirty = false;
        this.openProperties();
      });
      const remove = this.node('button','移除'); remove.type = 'button';
      remove.addEventListener('click',async () => {
        if (this.busy || !await this.confirmDiscard(`移除 #${edit.target_id} 的草稿？已发布游戏数据不会改变。`)) return;
        this.busy = true;
        try { const data = await this.api('/api/creature-draft',{revision:this.meta.revision,edits:this.meta.edits.filter(e => e.target_id !== edit.target_id)}); Object.assign(this.meta,data); this.renderDrafts(); if (this.draftTarget === edit.target_id) { this.dirty = false; await this.select(edit.source_id); } }
        catch (error) { this.error(error.message); } finally { this.busy = false; }
      }); row.append(open,remove); return row;
    }));
  }

  /** Restore the form baseline only after the user's unsaved-input choice. */
  async restore() {
    if (this.busy || this.dirty && !await this.confirmDiscard('恢复表单将放弃未暂存修改。')) return;
    if (this.baseline) { this.fill(this.baseline); this.dirty = false; this.error(''); }
  }

  /** Return to immutable viewing without removing any saved operations. */
  async view() {
    if (this.busy || this.dirty && !await this.confirmDiscard('返回查看将放弃未暂存修改。')) return;
    this.dirty = false;
    this.draftTarget = null; if (this.detail) await this.select(this.detail.source_id);
  }

  /** Refresh revisions explicitly; never discard unsaved input on a background polling loop. */
  async reload() {
    if (this.busy || this.dirty && !await this.confirmDiscard('重新读取将放弃未暂存输入，已保存草稿保留。')) return;
    this.busy = true;
    $('npc-fields').disabled = true;
    try { this.meta = await this.api('/api/creature-editor'); this.dirty = false; this.renderDrafts(); if (this.detail && !await this.select(this.detail.source_id)) return; this.error(''); }
    catch (error) { this.error(error.message); }
    finally { this.busy = false; $('npc-fields').disabled = this.loading || this.mode === 'view'; }
  }

  /** Confirm stopped services, then publish paired files and database points through the saved targets. */
  async export() {
    if (this.busy) return;
    if (this.dirty) return this.error('有未暂存输入，请先暂存或恢复后导出。');
    if (!this.canPublish()) return this.error('道具表单有未暂存输入，请先处理后再发布怪物。');
    if (this.meta.export_mode !== 'automatic') return this.error('请重启编辑器服务以启用怪物自动发布。');
    this.busy = true; $('npc-export').disabled = true;
    try {
      if (!await this.confirmPublish(this.meta.edits)) return;
      const target = this.draftTarget ?? this.meta.edits.at(-1)?.target_id;
      this.status('正在备份并发布怪物文件与地图点位…');
      const requestId = crypto.randomUUID();
      let result;
      try { result = await this.api('/api/creature-export',{revision:this.meta.revision,server_stopped:true,request_id:requestId}); }
      catch (error) {
        try { result = await this.api(`/api/publication-result?request_id=${requestId}`); } catch { /* Keep the original failure if outcome lookup also fails. */ }
        if (result?.status !== 'applied') throw error;
      }
      if (result.status !== 'applied') throw new Error('编辑器服务尚未启用自动发布，请重启后重试。');
      const message = `已发布 ${result.operations} 个怪物、${result.point_changes} 项点位修改。最近一次备份：${result.backup_path}。${result.cleanup_warning || ''}游戏效果待重新加载后验收。`;
      $('npc-export-result').textContent = message; this.status(message); this.error(''); this.dirty = false;
      this.meta.edits = []; this.renderDrafts();
      try { sessionStorage.setItem('last-creature-apply-result',message); } catch { /* Publication remains committed without storage. */ }
      try { this.applied(target); } catch { this.error('发布已成功，页面未能自动刷新，请手动刷新查看新基线。'); }
    }
    catch (error) { this.error(error.message); }
    finally { this.busy = false; $('npc-export').disabled = !this.meta.edits.length || this.meta.export_mode !== 'automatic'; }
  }

  /** Distinguish typed map inputs from operations actually staged for publication. */
  pointPending() {
    $('npc-point-pending').hidden = $('npc-reset-point').hidden = !this.pointDirty;
  }

  /** Preview the current independent appearance parameters while rejecting old selection responses. */
  async preview() {
    const ticket = ++this.previewTicket;
    try {
      const value = this.read(); const model = await this.api('/api/creature-preview',{appearance_id:value.appearance_id,scale:value.scale,...(value.model_ref?{model_ref:value.model_ref}:{})});
      if (ticket !== this.previewTicket) return;
      const viewer = this.getViewer(); if (!viewer) throw new Error('三维视口不可用，请检查浏览器 WebGL。');
      this.openPreview();
      await viewer.show(model,message => { if (ticket === this.previewTicket) $('model-warnings').textContent = message; });
      if (ticket !== this.previewTicket) return;
      $('model-warnings').textContent = '当前表单外观预览；完整动作与游戏碰撞效果待验收。'; this.error('');
    } catch (error) { if (ticket === this.previewTicket) this.error(error.message); }
  }

  /** Surface a recoverable error without deleting form state. */
  error(message) { $('npc-error').textContent = message; }
}
