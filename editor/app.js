const $ = id => document.getElementById(id);
const state = {items: [], fields: [], edits: [], revision: 0, details: new Map(), selected: null,
  draftTarget: null, dirty: false, busy: false, page: 0, ticket: 0, previewTicket: 0, previewMode: 'equipped', library: 'items'};
let viewer, creatures, settingsPanel;

/** Change only the visible library; preserve the item form and its unsaved values in place. */
async function switchLibrary(library) {
  if (state.busy || !creatures || state.library === library) return;
  state.library = library;
  state.ticket++; state.previewTicket++;
  const npc = library === 'creatures';
  $('workspace-main').classList.toggle('creatures-layout', npc);
  $('item-library').hidden = $('properties').hidden = npc;
  $('creature-library').hidden = $('creature-properties').hidden = !npc;
  document.querySelector('.preview-settings').hidden = npc;
  document.querySelector('.draft-area').hidden = npc;
  document.querySelector('.detail-icon').hidden = npc;
  document.querySelector('.eyebrow').hidden = npc;
  $('viewport').setAttribute('aria-label', npc ? 'NPC与怪物三维预览' : '道具三维预览');
  const skip = document.querySelector('.skip');
  skip.href = npc ? '#creature-properties' : '#properties';
  skip.textContent = npc ? '跳到NPC与怪物详情' : '跳到道具属性';
  $('library-items').setAttribute('aria-pressed', String(!npc));
  $('library-creatures').setAttribute('aria-pressed', String(npc));
  $('preview-scope-note').textContent = npc ? '角色首帧与存档装扮预览；动画、染色、特效及实例缩放尚未完整还原。' :
    '基础模型预览使用原始材质；装备姿态、染色和特效仍需与游戏内核对。';
  viewer?.clear();
  if (npc) await creatures.open();
  else {
    creatures.cancel();
    if (state.selected) {
      const item = state.selected;
      $('preview-title').textContent = $('item-name').value || item.name;
      $('id-tag').textContent = `ID ${item.id}`;
      $('slot-label').textContent = item.slots.map(s => s.label).join(' / ') || item.type_label;
      history.replaceState(null, '', `/?id=${item.id}`);
      status(state.dirty ? '已返回道具；未暂存的修改仍保留在表单中。' : `已返回 ${item.name}`);
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
function setBusy(value) {
  state.busy = value;
  $('export').disabled = value || !state.edits.length;
  $('editor-fields').disabled = value || !state.selected?.editable;
  for (const button of document.querySelectorAll('[data-remove]')) button.disabled = value;
}
async function detail(id) {
  if (!state.details.has(id)) state.details.set(id, await api(`/api/item/${id}`));
  return state.details.get(id);
}

/** Render read-only template data as text; null instance bonuses must never look like zero. */
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
  }
  $('instance-note').textContent = properties.applicable ?
    '化元、崩甲由装备附加词条产生；模板不含最终值。此处固定数值不包含强化及随机词条加成。' :
    '当前为非道具记录，这些道具属性不适用。';
  $('template-properties').setAttribute('aria-busy', 'false');
}

function renderItems() {
  const query = $('search').value.trim().toLowerCase();
  const category = $('category').value;
  const items = state.items.filter(item => (!$('only-editable').checked || item.is_item) &&
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
    button.addEventListener('click', () => select(item.id));
    $('items').append(button);
  }
  if (!items.length) $('items').append(node('p', '没有匹配的道具。', 'muted'));
  $('page-info').textContent = `${state.page + 1} / ${pages}`;
  $('prev').disabled = state.page === 0;
  $('next').disabled = state.page + 1 >= pages;
}

function fillForm(edit) {
  const item = state.selected;
  $('target-id').value = edit?.target_id ?? item.id;
  $('item-name').value = edit?.name ?? item.name;
  $('description').value = edit?.description ?? item.description;
  for (const field of state.fields) $(`stat-${field.key}`).value = edit?.stats[field.key] ?? item.stats[field.key];
  state.dirty = false;
  state.draftTarget = edit?.target_id ?? null;
  $('form-error').textContent = '';
  $('edit-state').textContent = !item.editable ? '只读' : edit ? '已暂存' : '原始记录';
  $('appearance').textContent = `外观脚本 ${item.actor} / 模式 ${item.mode} / 装备动作 ${item.mode + 2}`;
  $('readonly-reason').textContent = item.readonly_reason;
  setBusy(state.busy);
}

async function select(id, target = null) {
  if (state.busy) return;
  if (state.library !== 'items') await switchLibrary('items');
  if (state.dirty && !confirm('表单有尚未暂存的修改。放弃这些修改并切换？')) return;
  const ticket = ++state.ticket;
  state.previewTicket++;
  status(`正在打开道具 ${id}…`);
  $('editor-fields').disabled = true;
  $('preview-mode').disabled = $('profile').disabled = true;
  $('template-properties').setAttribute('aria-busy', 'true');
  for (const group of ['requirements', 'resistances', 'hidden-attributes']) $(group).replaceChildren();
  $('instance-note').textContent = '正在读取模板属性…';
  try {
    const item = await detail(id);
    if (ticket !== state.ticket) return;
    state.selected = item;
    renderTemplateProperties(item.template_properties);
    const edit = target === null ? state.edits.find(e => e.source_id === id && e.target_id === id) :
      state.edits.find(e => e.source_id === id && e.target_id === target);
    fillForm(edit);
    renderItems();
    $('preview-title').textContent = edit?.name ?? item.name;
    $('id-tag').textContent = `ID ${item.id}`;
    $('slot-label').textContent = item.slots.map(slot => slot.label).join(' / ') || item.type_label;
    const icon = $('detail-icon');
    icon.hidden = false; $('icon-error').hidden = true;
    icon.alt = `${item.name}的游戏图标`;
    icon.onerror = () => { if (state.selected?.id === id) { icon.hidden = true; $('icon-error').hidden = false; } };
    icon.src = `/api/icon/${id}.png`;
    $('preview-mode').disabled = false;
    $('preview-mode').value = item.type === 2 ? state.previewMode : 'single';
    history.replaceState(null, '', `/?id=${id}`);
    status(`已打开 ${item.name} · 基线 ${item.id} · 修改仅保存至本地草稿`);
    await loadPreview();
  } catch (error) { if (ticket === state.ticket) {
    status(error.message, true); setBusy(false);
    $('template-properties').setAttribute('aria-busy', 'false');
    $('instance-note').textContent = '模板属性读取失败，请重新选择道具。';
  } }
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
    const open = node('button', `${edit.target_id === edit.source_id ? '修改' : '复制'} #${edit.target_id} ${edit.name}`);
    open.addEventListener('click', () => select(edit.source_id, edit.target_id));
    const remove = node('button', '移除');
    remove.dataset.remove = String(edit.target_id);
    remove.setAttribute('aria-label', `从草稿移除 ${edit.name}`);
    remove.addEventListener('click', () => removeDraft(edit.target_id));
    head.append(open, remove); entry.append(head);
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
  $('draft-list').replaceChildren(...entries);
  $('draft-count').textContent = `${state.edits.length} 个操作`;
  $('export').disabled = state.busy || !state.edits.length;
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
  if (state.busy || !state.selected?.editable || !$('form').reportValidity()) return;
  const item = state.selected;
  const stats = Object.fromEntries(state.fields.map(field => [field.key, Number($(`stat-${field.key}`).value)]));
  const edit = {source_id: item.id, target_id: Number($('target-id').value),
    name: $('item-name').value, description: $('description').value, stats};
  setBusy(true);
  $('form-error').textContent = '';
  try {
    const edits = state.edits.filter(e => e.target_id !== (state.draftTarget ?? edit.target_id));
    // Never silently replace a different operation targeting the same ID.
    if (edits.some(e => e.target_id === edit.target_id)) throw new Error('目标 ID 已被另一个草稿占用。');
    if (state.draftTarget === null && state.edits.some(e => e.target_id === edit.target_id && e.source_id !== edit.source_id)) {
      throw new Error('目标 ID 已被另一个草稿占用。');
    }
    await saveEdits([...edits, edit]);
    state.dirty = false; state.draftTarget = edit.target_id;
    $('edit-state').textContent = '已暂存';
    $('preview-title').textContent = edit.name;
    status(`已暂存 ${edit.name}（ID ${edit.target_id}）· 草稿版本 ${state.revision}`);
  } catch (error) { $('form-error').textContent = error.message; status(error.message, true); }
  finally { setBusy(false); }
}

async function removeDraft(target) {
  if (state.busy) return;
  setBusy(true);
  try {
    await saveEdits(state.edits.filter(e => e.target_id !== target));
    if (state.draftTarget === target) fillForm(null);
    status(`已从草稿移除 ID ${target}。`);
  } catch (error) { status(error.message, true); }
  finally { setBusy(false); }
}

async function exportPatch() {
  if (state.busy) return;
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
      revision: data.revision, token: data.token});
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
      wrap.append(label, input); $('stats').append(wrap);
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
$('form').addEventListener('input', () => { state.dirty = true; $('edit-state').textContent = '未暂存'; });
$('restore').addEventListener('click', () => fillForm(null));
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
