import {operationMode, targetIdError, replaceDraftOperation, displayProperties} from './item-workflow.js';

const $ = id => document.getElementById(id);
const state = {items: [], fields: [], edits: [], revision: 0, details: new Map(), selected: null,
  draftTarget: null, dirty: false, busy: false, loading: false, mode: 'view', formBaseline: null,
  choosingTemplate: false, baselineIds: new Set(), idsLoaded: false, ready: false,
  page: 0, ticket: 0, previewTicket: 0, previewMode: 'equipped', library: 'items'};
let viewer, creatures, settingsPanel, itemDropSources;

/** Change only the visible library; preserve the item form and its unsaved values in place. */
async function switchLibrary(library, templateId = null) {
  if (state.busy || state.loading || !creatures) return;
  if (state.library === library) {
    if (library === 'creatures' && templateId !== null) await creatures.openTemplate(templateId);
    return;
  }
  state.library = library;
  state.ticket++; state.previewTicket++;
  const npc = library === 'creatures';
  $('workspace-main').classList.toggle('creatures-layout', npc);
  $('item-library').hidden = $('properties').hidden = npc;
  $('creature-library').hidden = $('creature-properties').hidden = !npc;
  document.querySelector('.preview-settings').hidden = npc;
  document.querySelector('.draft-area').hidden = npc;
  $('item-drop-sources').hidden = npc;
  document.querySelector('.detail-icon').hidden = npc;
  document.querySelector('.eyebrow').hidden = npc;
  $('viewport').setAttribute('aria-label', npc ? 'NPC与怪物三维预览' : '道具三维预览');
  const skip = document.querySelector('.skip');
  skip.href = npc ? '#creature-properties' : '#properties';
  skip.textContent = npc ? '跳到NPC与怪物详情' : '跳到道具属性';
  $('library-items').setAttribute('aria-pressed', String(!npc));
  $('library-creatures').setAttribute('aria-pressed', String(npc));
  $('preview-scope-note').textContent = npc ? '模板静态首帧预览；动画、染色与动态特效尚未完整还原。' :
    '基础模型预览使用原始材质；装备姿态、染色和特效仍需与游戏内核对。';
  viewer?.clear();
  if (npc) {
    itemDropSources?.cancel();
    if (templateId === null) await creatures.open();
    else await creatures.openTemplate(templateId);
  }
  else {
    creatures.cancel();
    if (state.selected) {
      const item = state.selected;
      syncIdentity();
      $('slot-label').textContent = item.slots.map(s => s.label).join(' / ') || item.type_label;
      history.replaceState(null, '', `/?id=${item.id}`);
      status(state.dirty ? '已返回道具；未暂存的修改仍保留在表单中。' : `已返回 ${item.name}`);
      refreshDropSources();
      await loadPreview();
    }
  }
}

/** Use text nodes for all game data; no game-supplied string becomes HTML. */
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

/** Make a bounded local API call and surface server validation failures in the UI. */
async function api(path, body) {
  const headers = {'Content-Type': 'application/json', 'X-Editor-Token': state.token};
  if (state.workspaceGeneration !== undefined && state.workspaceGeneration !== null) headers['X-Workspace-Generation'] = String(state.workspaceGeneration);
  const response = await fetch(path, body === undefined ? {} : {method: 'POST',
    headers, body: JSON.stringify(body)});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || `请求失败：${response.status}`);
  return value;
}

function status(text, error = false) { $('status').textContent = text; $('status').classList.toggle('error', error); }

/** Protect unsaved input with a keyboard-accessible decision; Escape always retains the form. */
async function confirmDiscard(message) {
  const dialog = $('discard-dialog');
  if (dialog.open) return false;
  $('discard-message').textContent = message;
  dialog.returnValue = 'keep';
  return new Promise(resolve => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'discard'), {once: true});
    dialog.showModal();
  });
}
function setBusy(value) {
  state.busy = value;
  const blocked = value || state.loading;
  $('export').disabled = blocked || !state.edits.length;
  $('editor-fields').disabled = blocked || state.mode === 'view' || !state.selected?.editable;
  $('new-item').disabled = blocked || !state.ready;
  $('edit-item').disabled = blocked || !state.selected?.editable || state.mode === 'edit';
  $('clone-item').disabled = blocked || !state.selected?.editable;
  $('cancel-template').disabled = blocked;
  for (const button of document.querySelectorAll('#items button')) button.disabled = value;
  for (const button of document.querySelectorAll('[data-remove], [data-open-draft]')) button.disabled = blocked;
}

/** Keep the displayed identity tied to the form, while resource requests always use the template ID. */
function syncIdentity() {
  if (!state.selected) return;
  $('preview-title').textContent = $('item-name').value || state.selected.name;
  const target = Number($('target-id').value);
  $('id-tag').textContent = state.mode === 'create' ?
    (Number.isInteger(target) && target > 0 ? `新 ID ${target}` : '新 ID 待填写') : `ID ${state.selected.id}`;
}

/** Refresh copy and controls for an explicit view, edit or create operation. */
function syncWorkflow() {
  const creating = state.mode === 'create';
  const properties = $('properties');
  properties.dataset.mode = state.mode;
  // Keep editable fields first during a task, without moving the focused form on each keystroke.
  const first = state.mode === 'view' ? $('template-properties') : $('form');
  const second = state.mode === 'view' ? $('form') : $('template-properties');
  if (first.nextElementSibling !== second) properties.insertBefore(first, second);
  $('properties-title').textContent = creating ? '新增道具' : state.mode === 'edit' ? '编辑道具' : '道具属性';
  $('target-id-label').textContent = creating ? '新道具 ID' : '道具 ID';
  $('target-id').readOnly = !creating;
  $('suggest-id').hidden = !creating;
  $('form-actions').hidden = state.mode === 'view';
  $('stage').textContent = creating ? '暂存新增' : '暂存修改';
  $('workflow-note').textContent = creating ? '修改名称、说明和基础属性后暂存新增。图标和模型沿用模板。' :
    state.mode === 'edit' ? '正在编辑现有道具，原 ID 固定。暂存后可导出双端补丁。' :
    '当前为查看模式。点击“编辑道具”修改，或以此为模板新增。';
  $('template-source').hidden = !creating;
  if (creating) $('template-source').textContent = `模板：${state.selected.name}（ID ${state.selected.id}）· 使用原始基线，独立于模板的修改草稿。`;
  $('edit-state').textContent = !state.selected?.editable ? '只读' :
    state.dirty ? '未暂存' : state.draftTarget !== null ? '已暂存' : state.mode === 'edit' ? '编辑中' : '原始记录';
  updateIdHint();
  syncIdentity();
  refreshDisplayedProperties();
  refreshDropSources();
  setBusy(state.busy);
}

/** Query the displayed item ID; clones never inherit the source template's drop relations. */
function refreshDropSources() {
  if (!itemDropSources || state.library !== 'items') return;
  const item = state.selected;
  if (!item?.is_item) { itemDropSources.show({reason: '当前记录不是道具。'}); return; }
  if (state.mode !== 'create') { itemDropSources.show({id: item.id, draft: false}); return; }
  const id = Number($('target-id').value);
  const error = targetIdError(id, state.baselineIds, state.edits, state.draftTarget);
  if (error) itemDropSources.show({reason: error});
  else itemDropSources.show({id, draft: true});
}

/** Update the attribute summary from valid form integers without mutating the baseline or draft. */
function refreshDisplayedProperties() {
  const item = state.selected;
  if (!item) return;
  const values = Object.fromEntries(state.fields.map(field => {
    const input = $(`stat-${field.key}`);
    return [field.key, input.value !== '' && input.validity.valid ? input.valueAsNumber : undefined];
  }));
  renderTemplateProperties(displayProperties(item.template_properties, item.stats, values));
}

/** Show local ID conflicts immediately; the server remains authoritative when saving. */
function updateIdHint() {
  const error = state.mode === 'create' && state.idsLoaded ?
    targetIdError(Number($('target-id').value), state.baselineIds, state.edits, state.draftTarget) : '';
  $('target-id').setCustomValidity(error);
  $('id-hint').textContent = state.mode === 'create' ? (error || '空闲 ID 仅供建议，暂存时会再次校验。') : '编辑现有道具时 ID 固定。';
  $('id-hint').classList.toggle('error', Boolean(error));
}

/** Read current occupancy without reserving an ID or replacing the local form or draft. */
async function readIdAvailability() {
  const data = await api('/api/item-ids');
  if (state.workspaceGeneration !== undefined && data.workspace_generation !== state.workspaceGeneration) {
    throw new Error('数据源已切换，请刷新页面后新增。当前表单尚未保存。');
  }
  if (data.revision !== state.revision) throw new Error('草稿已在其他页面修改，请刷新后再新增。当前输入仍保留。');
  state.baselineIds = new Set(data.baseline_ids);
  state.idsLoaded = true;
  return data.suggested_id;
}

/** Reuse the searchable library for template selection; cancelling leaves the current form intact. */
function chooseTemplate(value) {
  if (state.busy || state.loading) return;
  state.choosingTemplate = value;
  $('template-picker').hidden = !value;
  $('template-error').textContent = '';
  $('new-item').textContent = value ? '正在选择模板' : '新增道具';
  renderItems();
  if (value) {
    $('template-picker-title').focus();
    $('template-picker').scrollIntoView({block: 'nearest'});
  }
}

/** Obtain another suggestion while preserving every other value, including an existing draft identity. */
async function suggestId() {
  if (state.busy || state.loading || state.mode !== 'create') return;
  setBusy(true);
  try {
    const id = await readIdAvailability();
    if (id === null) throw new Error('1 至 32767 内没有空闲 ID，请移除不需要的新增草稿后重试。');
    $('target-id').value = id;
    state.dirty = true;
    $('form-error').textContent = '';
    syncWorkflow();
  } catch (error) { $('form-error').textContent = error.message; status(error.message, true); }
  finally { setBusy(false); }
}
async function detail(id) {
  if (!state.details.has(id)) state.details.set(id, await api(`/api/item/${id}`));
  return state.details.get(id);
}

/** Render read-only template values, preserving zero and keeping instance bonuses separate. */
function renderTemplateProperties(properties) {
  const appendValue = (host, label, value) => {
    const row = node('div', undefined, 'property-row');
    const description = node('dd', value);
    row.append(node('dt', label), description);
    host.append(row);
  };
  for (const id of ['requirements', 'resistances', 'hidden-attributes']) $(id).replaceChildren();
  for (const field of properties.requirements) appendValue($('requirements'), field.label, field.text);
  for (const field of properties.resistances) {
    appendValue($('resistances'), field.label, field.value === null ? '不适用' : String(field.value));
  }
  for (const field of properties.hidden_attributes) {
    const text = field.source === 'instance_required' ? '需装备实例数据' :
      field.value === null ? '不适用' : `${field.value}${field.unit || ''}`;
    appendValue($('hidden-attributes'), field.label, text);
    if (field.display_source === 'evasion') {
      $('hidden-attributes').lastElementChild.lastElementChild.append(node('span', '（随回避率）', 'muted'));
    }
  }
  $('instance-note').textContent = properties.applicable ?
    '显示当前表单数值，不包含强化或附加词条。标记“随回避率”的御劲仅作显示联动，不写入御劲字段。' :
    '当前为非道具记录，这些道具属性不适用。';
  $('template-properties').setAttribute('aria-busy', 'false');
}

function renderItems() {
  const query = $('search').value.trim().toLowerCase();
  const category = $('category').value;
  const items = state.items.filter(item => (!$('only-editable').checked || item.is_item) &&
    (!state.choosingTemplate || (item.item_asset && item.id <= 32767)) &&
    (category === 'all' || item.categories.includes(category)) &&
    (!query || item.name.toLowerCase().includes(query) || String(item.id).includes(query)));
  const pages = Math.max(1, Math.ceil(items.length / 60));
  state.page = Math.min(state.page, pages - 1);
  $('item-count').textContent = `${items.length} 项`;
  $('items').replaceChildren();
  for (const item of items.slice(state.page * 60, (state.page + 1) * 60)) {
    const button = node('button', undefined, 'item');
    button.classList.toggle('active', item.id === state.selected?.id);
    button.setAttribute('aria-pressed', String(item.id === state.selected?.id));
    const icon = node('img');
    Object.assign(icon, {src: `/api/icon/${item.id}.png`, alt: '', loading: 'lazy', decoding: 'async', width: 40, height: 40});
    icon.className = 'list-icon';
    const markMissing = () => {
      const missing = node('span', '缺图', 'missing-icon');
      missing.setAttribute('role', 'img'); missing.setAttribute('aria-label', '该记录图标不可用');
      icon.replaceWith(missing);
    };
    icon.addEventListener('error', markMissing, {once: true});
    const text = node('span', undefined, 'item-text');
    text.append(node('span', item.name, 'item-name'), node('span', `#${item.id} · ${item.slots.map(s => s.label).join(' / ') || item.type_label}`, 'item-meta'));
    button.append(icon, text);
    if (!item.has_icon) { icon.removeAttribute('src'); markMissing(); }
    if (state.edits.some(edit => edit.source_id === item.id)) button.append(node('span', '草稿', 'draft-dot'));
    button.disabled = state.busy;
    button.addEventListener('click', () => select(item.id, null, state.choosingTemplate ? 'create' : 'view'));
    $('items').append(button);
  }
  if (!items.length) $('items').append(node('p', '没有匹配的道具。', 'muted'));
  $('page-info').textContent = `${state.page + 1} / ${pages}`;
  $('prev').disabled = state.page === 0;
  $('next').disabled = state.page + 1 >= pages;
}

function fillForm(edit, mode = 'view', targetId = null) {
  const item = state.selected;
  state.mode = mode;
  $('target-id').value = edit?.target_id ?? (mode === 'create' ? targetId ?? '' : item.id);
  $('item-name').value = edit?.name ?? item.name;
  $('description').value = edit?.description ?? item.description;
  for (const field of state.fields) $(`stat-${field.key}`).value = edit?.stats[field.key] ?? item.stats[field.key];
  state.dirty = mode === 'create' && !edit;
  state.draftTarget = edit?.target_id ?? null;
  $('form-error').textContent = '';
  state.formBaseline = {target_id: $('target-id').value, name: $('item-name').value,
    description: $('description').value,
    stats: Object.fromEntries(state.fields.map(field => [field.key, $(`stat-${field.key}`).value]))};
  $('appearance').textContent = `外观脚本 ${item.actor} / 模式 ${item.mode} / 装备动作 ${item.mode + 2}`;
  $('readonly-reason').textContent = item.readonly_reason;
  syncWorkflow();
}

/** Load a baseline or saved operation, discarding input only after an explicit confirmation. */
async function select(id, target = null, mode = 'view') {
  if (state.busy) return;
  if (state.library !== 'items') await switchLibrary('items');
  if (state.dirty && !await confirmDiscard('切换后将放弃当前表单中尚未暂存的内容。已暂存草稿保留。')) return;
  const ticket = ++state.ticket;
  state.previewTicket++;
  state.loading = true;
  setBusy(state.busy);
  status(`正在打开道具 ${id}…`);
  $('editor-fields').disabled = true;
  $('preview-mode').disabled = $('profile').disabled = true;
  $('template-properties').setAttribute('aria-busy', 'true');
  try {
    const item = await detail(id);
    if (ticket !== state.ticket) return;
    let suggested = null;
    if (mode === 'create' && target === null) {
      if (!item.editable) throw new Error(`无法使用该模板：${item.readonly_reason} 请另选道具。`);
      suggested = await readIdAvailability();
      if (ticket !== state.ticket) return;
      if (suggested === null) throw new Error('1 至 32767 内没有空闲 ID，请先整理新增草稿。');
    }
    if (target !== null && target !== id && !state.idsLoaded) {
      await readIdAvailability();
      if (ticket !== state.ticket) return;
    }
    state.selected = item;
    renderTemplateProperties(item.template_properties);
    const edit = mode === 'create' && target === null ? null : target === null ? state.edits.find(e => e.source_id === id && e.target_id === id) :
      state.edits.find(e => e.source_id === id && e.target_id === target);
    if (state.choosingTemplate) {
      state.choosingTemplate = false;
      $('template-picker').hidden = true;
      $('new-item').textContent = '新增道具';
    }
    fillForm(edit, target !== null && edit ? operationMode(edit) : mode, suggested);
    renderItems();
    syncIdentity();
    $('slot-label').textContent = item.slots.map(slot => slot.label).join(' / ') || item.type_label;
    const icon = $('detail-icon');
    icon.hidden = false; $('icon-error').hidden = true;
    icon.alt = `${item.name}的游戏图标`;
    icon.onerror = () => { if (state.selected?.id === id) { icon.hidden = true; $('icon-error').hidden = false; } };
    icon.src = `/api/icon/${id}.png`;
    $('preview-mode').disabled = false;
    $('preview-mode').value = item.type === 2 ? state.previewMode : 'single';
    history.replaceState(null, '', `/?id=${id}`);
    status(state.mode === 'create' ? `新增道具 · 模板 ${item.name}（${id}）· 暂存后生成独立记录` :
      `已打开 ${item.name} · 基线 ${item.id} · 修改仅保存至本地草稿`);
    state.loading = false;
    setBusy(state.busy);
    if (state.mode !== 'view') {
      $('properties').scrollTop = 0;
      $('properties-title').setAttribute('tabindex', '-1');
      $('properties-title').focus({preventScroll: true});
      $('properties').scrollIntoView({block: 'start'});
    }
    await loadPreview();
  } catch (error) { if (ticket === state.ticket) {
    status(error.message, true);
    if (state.choosingTemplate) $('template-error').textContent = error.message;
    $('template-properties').setAttribute('aria-busy', 'false');
  } } finally { if (ticket === state.ticket) {
    state.loading = false; setBusy(state.busy);
    $('preview-mode').disabled = !state.selected;
    $('profile').disabled = !state.selected || state.previewMode === 'single';
    $('template-properties').setAttribute('aria-busy', 'false');
  } }
}

/** Restore the entry snapshot or last saved operation without changing any persisted draft. */
async function restoreForm() {
  if (!state.formBaseline || state.busy || state.loading) return;
  if (state.dirty && !await confirmDiscard('恢复表单将放弃本次未暂存的输入，返回进入表单时或最近暂存的内容。')) return;
  const saved = state.formBaseline;
  $('target-id').value = saved.target_id;
  $('item-name').value = saved.name;
  $('description').value = saved.description;
  for (const field of state.fields) $(`stat-${field.key}`).value = saved.stats[field.key];
  state.dirty = state.mode === 'create' && state.draftTarget === null;
  $('form-error').textContent = '';
  syncWorkflow();
  status('已恢复表单，已暂存草稿保持原样。');
}

/** Change only preview state; preserve unsaved item attributes when switching modes or characters. */
async function loadPreview() {
  if (!state.selected || state.library !== 'items') return;
  const id = state.selected.id, ticket = ++state.previewTicket;
  const mode = $('preview-mode').value;
  $('profile').disabled = mode !== 'equipped';
  $('model-info').textContent = '正在读取外观…';
  $('model-warnings').textContent = '';
  $('model-message').textContent = '正在加载原始资源…';
  viewer?.clear();
  if (!viewer) { $('model-message').textContent = '三维渲染不可用，仍可查看图标和属性。'; return; }
  try {
    const model = await api(`/api/model/${id}?mode=${mode}&profile=${encodeURIComponent($('profile').value)}`);
    if (ticket !== state.previewTicket) return;
    await viewer.show(model, message => {
      if (ticket === state.previewTicket) $('model-warnings').textContent += ` ${message}`;
    });
    viewer.wireframe($('wireframe').checked);
    $('model-message').textContent = '';
    const label = mode === 'equipped' ? `人物穿戴 · ${model.profile_label}` : model.mode_label;
    $('model-info').textContent = `${label} · ${model.vertices} 顶点 / ${model.triangles} 三角面`;
    $('model-warnings').textContent = model.warnings.join(' ');
  } catch (error) {
    if (ticket !== state.previewTicket) return;
    $('model-message').textContent = error.message;
    $('model-info').textContent = '当前方式无法显示三维外观';
  }
}

async function renderDrafts() {
  const revision = state.revision;
  const entries = [];
  for (const edit of state.edits) {
    const before = await detail(edit.source_id);
    if (revision !== state.revision) return;
    const entry = node('div', undefined, 'draft-entry');
    const head = node('div', undefined, 'draft-entry-head');
    const open = node('button', `${edit.target_id === edit.source_id ? '编辑' : '新增'} #${edit.target_id} ${edit.name}`);
    open.dataset.openDraft = String(edit.target_id);
    open.disabled = state.busy || state.loading;
    open.addEventListener('click', () => select(edit.source_id, edit.target_id));
    const remove = node('button', '移除');
    remove.dataset.remove = String(edit.target_id);
    remove.setAttribute('aria-label', `从草稿移除 ${edit.name}`);
    remove.addEventListener('click', () => removeDraft(edit.target_id));
    head.append(open, remove); entry.append(head);
    if (edit.target_id !== edit.source_id) entry.append(node('p', `模板：${before.name}（ID ${edit.source_id}）`, 'muted'));
    const diffs = [];
    if (edit.target_id !== edit.source_id) diffs.push(['ID', edit.source_id, edit.target_id]);
    if (edit.name !== before.name) diffs.push(['名称', before.name, edit.name]);
    if (edit.description !== before.description) diffs.push(['说明', before.description, edit.description]);
    for (const field of state.fields) if (edit.stats[field.key] !== undefined && edit.stats[field.key] !== before.stats[field.key]) {
      diffs.push([field.label, before.stats[field.key], edit.stats[field.key]]);
    }
    for (const [label, old, value] of diffs) {
      const line = node('div', undefined, 'diff');
      line.append(node('span', `${label}：`), node('del', String(old)), node('span', ' → '), node('ins', String(value)));
      entry.append(line);
    }
    if (!diffs.length) entry.append(node('p', '没有字段变化', 'muted'));
    entries.push(entry);
  }
  if (revision !== state.revision) return;
  if (!entries.length) entries.push(node('p', '尚无草稿。编辑现有道具，或点击“新增道具”选择模板。', 'muted'));
  $('draft-list').replaceChildren(...entries);
  $('draft-count').textContent = `${state.edits.length} 个操作`;
  $('export').disabled = state.busy || state.loading || !state.edits.length;
}

async function saveEdits(edits) {
  const result = await api('/api/draft', {revision: state.revision, edits});
  state.edits = result.edits;
  state.revision = result.revision;
  $('export-result').textContent = '';
  renderItems();
  await renderDrafts();
}

async function stage(event) {
  event.preventDefault();
  if (state.busy || state.loading || state.mode === 'view' || !state.selected?.editable) return;
  updateIdHint();
  if (!$('form').reportValidity()) return;
  const item = state.selected;
  const stats = Object.fromEntries(state.fields.map(field => [field.key, Number($(`stat-${field.key}`).value)]));
  const edit = {source_id: item.id, target_id: state.mode === 'edit' ? item.id : Number($('target-id').value),
    name: $('item-name').value, description: $('description').value, stats};
  setBusy(true);
  $('form-error').textContent = '';
  try {
    await saveEdits(replaceDraftOperation(state.edits, edit, state.draftTarget));
    fillForm(edit, state.mode);
    status(`已暂存 ${edit.name}（ID ${edit.target_id}）· 草稿版本 ${state.revision}`);
  } catch (error) { $('form-error').textContent = error.message; status(error.message, true); }
  finally { setBusy(false); }
}

async function removeDraft(target) {
  if (state.busy || state.loading) return;
  if (state.draftTarget === target && state.dirty && !await confirmDiscard('移除此草稿也会放弃它尚未暂存的输入。其他草稿保留。')) return;
  setBusy(true);
  try {
    await saveEdits(state.edits.filter(e => e.target_id !== target));
    if (state.draftTarget === target) fillForm(null);
    else updateIdHint();
    status(`已从草稿移除 ID ${target}。`);
  } catch (error) { status(error.message, true); }
  finally { setBusy(false); }
}

async function exportPatch() {
  if (state.busy || state.loading) return;
  if (state.dirty) { status('表单尚未暂存，请先暂存修改后再导出。', true); $('stage').focus(); return; }
  setBusy(true); status('正在生成双端补丁并重新解码核验…');
  try {
    const result = await api('/api/export', {revision: state.revision});
    $('export-result').textContent = `已导出并通过离线校验：${result.path}。游戏内效果待验收。`;
    status(`补丁导出成功 · ${result.verification.operations} 个操作 · 原游戏文件未改动`);
  } catch (error) { status(error.message, true); }
  finally { setBusy(false); }
}

async function start() {
  try {
    const params = new URLSearchParams(location.search);
    const data = await api('/api/bootstrap');
    state.token = data.token;
    state.workspaceGeneration = data.workspace_generation;
    try {
      const {SettingsPanel} = await import('./settings.js');
      settingsPanel = new SettingsPanel({api, node,
        beforeApply: () => !state.dirty || confirm('道具表单有未暂存的修改。保存设置将重新载入页面，是否放弃这些表单修改？'),
        applied: () => { state.dirty = false; location.reload(); }});
    } catch {
      $('open-settings').disabled = true;
      $('open-settings').title = '重启本机编辑器后启用设置';
    }
    if (data.ready === false) {
      status(data.setup_error || '请先配置数据源。', true);
      if (settingsPanel) await settingsPanel.open(true);
      return;
    }
    Object.assign(state, {items: data.items, fields: data.fields, edits: data.edits,
      revision: data.revision, token: data.token, ready: true});
    $('category').replaceChildren(...data.categories.map(category => {
      const count = state.items.filter(item => item.is_item && (category.key === 'all' || item.categories.includes(category.key))).length;
      const option = node('option', `${category.label}（${count}）`); option.value = category.key; return option;
    }));
    $('profile').replaceChildren(...data.profiles.map(profile => {
      const option = node('option', profile.label); option.value = profile.key; return option;
    }));
    for (const field of state.fields) {
      const wrap = node('div'); const label = node('label', field.label);
      label.htmlFor = `stat-${field.key}`;
      const input = node('input');
      Object.assign(input, {id: label.htmlFor, type: 'number', min: field.min, max: field.max, step: 1, required: true});
      input.title = `Object 偏移 ${field.offset}，有符号整数`;
      const host = field.group === 'resistances' ? $('resistance-stats') : field.group === 'recovery' ? $('recovery-stats') : $('stats');
      wrap.append(label, input); host.append(wrap);
    }
    const labels = {server_object: '服务端本地基线', client_object: '客户端物件表', client_language: '客户端语言表'};
    for (const [key, source] of Object.entries(data.sources)) {
      const p = node('p', `${labels[key]}：${source.path}`); p.append(node('code', source.sha256)); $('sources').append(p);
    }
    try {
      const {Viewer} = await import('/viewer.js');
      viewer = new Viewer($('viewport'));
    } catch (error) { console.error('三维视口初始化失败', error); }
    if (!viewer) for (const control of document.querySelectorAll('.view-controls button, .view-controls input')) control.disabled = true;
    try {
      const {CreatureBrowser} = await import('./creatures.js');
      creatures = new CreatureBrowser({api, node, getViewer: () => viewer, status,
        openItem: async id => { await switchLibrary('items'); await select(id); }});
    } catch {
      // A pre-upgrade running handler may serve current HTML but lack the new module allowlist.
      $('library-creatures').disabled = true;
      $('creature-availability-note').textContent = '重启本机编辑器后启用NPC查看';
    }
    try {
      const {ItemDropSources} = await import('./item-drops.js');
      itemDropSources = new ItemDropSources({api, node,
        getGeneration: () => state.workspaceGeneration,
        openCreature: id => switchLibrary('creatures', id)});
    } catch {
      $('item-drop-note').textContent = '重启本机编辑器后查看掉落怪物。';
    }
    renderItems(); await renderDrafts();
    const requested = Number(params.get('id'));
    await select(state.items.some(i => i.id === requested) ? requested : 10);
    if (creatures && params.get('library') === 'creatures') {
      $('creature-search').value = params.get('npc') || '';
      creatures.initialMap = params.get('map') || 'all';
      await switchLibrary('creatures');
    }
  } catch (error) { status(`启动失败：${error.message}`, true); $('model-message').textContent = '请查看启动控制台并重新加载页面。'; }
}

$('form').addEventListener('submit', stage);
$('library-items').addEventListener('click', () => switchLibrary('items'));
$('library-creatures').addEventListener('click', () => switchLibrary('creatures'));
$('form').addEventListener('input', () => {
  if (state.mode === 'view') return;
  state.dirty = true;
  syncWorkflow();
});
$('new-item').addEventListener('click', () => chooseTemplate(true));
$('cancel-template').addEventListener('click', () => chooseTemplate(false));
$('edit-item').addEventListener('click', () => select(state.selected.id, null, 'edit'));
$('clone-item').addEventListener('click', () => select(state.selected.id, null, 'create'));
$('suggest-id').addEventListener('click', suggestId);
$('cancel-edit').addEventListener('click', () => select(state.selected.id));
$('restore').addEventListener('click', restoreForm);
$('export').addEventListener('click', exportPatch);
$('search').addEventListener('input', () => { state.page = 0; renderItems(); });
$('only-editable').addEventListener('change', () => { state.page = 0; renderItems(); });
$('category').addEventListener('change', () => { state.page = 0; renderItems(); });
$('preview-mode').addEventListener('change', () => { state.previewMode = $('preview-mode').value; loadPreview(); });
$('profile').addEventListener('change', loadPreview);
$('prev').addEventListener('click', () => { state.page--; renderItems(); });
$('next').addEventListener('click', () => { state.page++; renderItems(); });
$('reset-view').addEventListener('click', () => viewer?.reset());
$('rotate-left').addEventListener('click', () => viewer?.rotate(-Math.PI / 8));
$('rotate-right').addEventListener('click', () => viewer?.rotate(Math.PI / 8));
$('zoom-in').addEventListener('click', () => viewer?.zoom(.8));
$('zoom-out').addEventListener('click', () => viewer?.zoom(1.25));
$('wireframe').addEventListener('change', () => viewer?.wireframe($('wireframe').checked));
window.addEventListener('beforeunload', event => { if (state.dirty) { event.preventDefault(); event.returnValue = ''; } });
window.addEventListener('pagehide', () => viewer?.dispose());
start();
