/** Acquisition form state stays independent of the item template and is persisted only by staging. */
export function emptyAcquisition() { return {drop: null, shops: [], rewards: []}; }

/** Project native probability choices without promising percentages that the engine cannot express. */
export function nearestChance(value, choices) {
  if (value === '' || !Number.isFinite(Number(value)) || Number(value) < 0 || Number(value) > 100 || !choices?.length) return null;
  return choices.reduce((best, choice) => Math.abs(choice.percent - Number(value)) < Math.abs(best.percent - Number(value)) ? choice : best);
}

/** Give drafts a compact, useful summary even when optional source metadata cannot be read. */
export function acquisitionSummary(value) {
  const result = [];
  if (value?.drop) result.push(`怪物 #${value.drop.monster_id} · 地图 #${value.drop.map_id} · 请求掉率 ${value.drop.percent}% · 1 件`);
  for (const shop of value?.shops || []) result.push(`商店 ${shop.node}`);
  for (const reward of value?.rewards || []) result.push(`任务奖励 ${reward.node} · ${reward.count} 件`);
  return result;
}

/** Own asynchronous choice loading, explicit additions and stale-workspace rejection. */
export class AcquisitionForm {
  constructor({api, node, getGeneration, onReady}) {
    this.api = api; this.node = node; this.getGeneration = getGeneration;
    this.onReady = onReady;
    this.$ = id => document.getElementById(`acquisition-${id}`);
    this.value = emptyAcquisition(); this.data = null; this.target = null; this.loading = false;
    this.$('drop-enabled').addEventListener('change', () => this.updateDrop());
    this.$('map').addEventListener('change', () => { this.$('monster').value = ''; this.renderMonsters(); this.updateDrop(); this.changed(); });
    this.$('monster-search').addEventListener('input', () => this.renderMonsters());
    this.$('monster').addEventListener('change', () => this.updateDrop());
    this.$('percent').addEventListener('input', () => this.updateDrop());
    for (const kind of ['shop', 'reward']) {
      this.$(`${kind}-search`).addEventListener('input', () => this.renderChoices(kind));
      this.$(`add-${kind}`).addEventListener('click', () => this.add(kind));
    }
    this.$('retry').addEventListener('click', () => this.load());
  }

  /** Load once per workspace; a failed catalog never clears saved or unsaved acquisition rules. */
  async load() {
    if (this.loading) return;
    this.loading = true; this.$('status').textContent = '正在读取可配置来源…';
    this.$('retry').hidden = true;
    try {
      const data = await this.api('/api/acquisition/catalog');
      if (data.workspace_generation !== this.getGeneration()) throw new Error('数据源已切换，请刷新页面。');
      this.data = data;
      const selected = this.$('map').value || (this.value.drop ? String(this.value.drop.map_id) : '');
      this.$('map').replaceChildren(this.option('', '选择地图'), ...data.maps.map(m => this.option(m.id, `${m.name}（${m.id}）`)));
      this.$('map').value = selected;
      this.renderMonsters(); this.renderChoices('shop'); this.renderChoices('reward');
      this.renderLists(); this.updateDrop(); this.showExisting();
      this.$('status').textContent = data.script_error ? `怪物来源可用；商店／奖励暂不可用：${data.script_error}` :
        `已读取 ${data.shops.length} 个商品表、${data.rewards.length} 个奖励节点。仅列出可完整解析的函数。`;
      this.$('retry').hidden = !data.script_error;
      this.onReady?.();
    } catch (error) {
      this.$('status').textContent = `来源读取失败：${error.message}。已有输入保留，可继续编辑其他道具属性。`;
      this.$('retry').hidden = false;
    } finally { this.loading = false; }
  }

  /** Restore one exact draft; clones begin empty and never inherit their template's acquisition rules. */
  set(value) {
    this.value = structuredClone(value || emptyAcquisition());
    this.$('drop-enabled').checked = !!this.value.drop;
    this.$('map').value = this.value.drop ? String(this.value.drop.map_id) : '';
    this.$('monster').value = '';
    this.$('percent').value = this.value.drop?.percent ?? 10;
    for (const id of ['monster-search', 'shop-search', 'reward-search']) this.$(id).value = '';
    this.renderMonsters(); this.renderLists(); this.updateDrop();
  }

  /** Read current controls only on explicit staging; invalid selections cannot silently disappear. */
  get() {
    const result = structuredClone(this.value);
    result.drop = null;
    if (this.$('drop-enabled').checked) {
      if (!this.data || !this.$('map').value || !this.$('monster').value) throw new Error('请选择有效的地图和掉落怪物。');
      if (!nearestChance(this.$('percent').value, this.data.probabilities)) throw new Error('掉落率必须是 0 至 100 的数值。');
      result.drop = {map_id: Number(this.$('map').value), monster_id: Number(this.$('monster').value), percent: Number(this.$('percent').value)};
    }
    return result;
  }

  option(value, label) { const el = this.node('option', label); el.value = String(value); return el; }

  renderMonsters() {
    const selected = this.$('monster').value || String(this.value.drop?.monster_id || '');
    const map = this.$('map').value, query = this.$('monster-search').value.trim().toLowerCase();
    const matches = (this.data?.monsters || []).filter(m => map !== '' && m.maps.includes(Number(map)) &&
      (!query || `${m.id} ${m.name}`.toLowerCase().includes(query)));
    this.$('monster').replaceChildren(this.option('', matches.length ? '选择怪物' : '该地图没有匹配的怪物'),
      ...matches.map(m => this.option(m.id, `${m.name}（${m.id}）`)));
    if (!matches.some(m => String(m.id) === selected) && this.data?.monsters.some(m => String(m.id) === selected && m.maps.includes(Number(map)))) {
      const m = this.data.monsters.find(m => String(m.id) === selected);
      this.$('monster').append(this.option(m.id, `${m.name}（当前已选）`));
    }
    this.$('monster').value = selected;
  }

  updateDrop() {
    const enabled = this.$('drop-enabled').checked;
    this.$('drop-fields').hidden = !enabled;
    for (const id of ['map', 'monster', 'percent']) { this.$(id).required = enabled; this.$(id).disabled = !enabled; }
    const chance = nearestChance(this.$('percent').value, this.data?.probabilities);
    this.$('chance').textContent = chance ? `实际生效掉率：${chance.percent.toFixed(4)}%` : '读取来源后显示实际生效掉率。';
    const monster = this.data?.monsters.find(m => String(m.id) === this.$('monster').value);
    const maps = monster?.maps.map(id => this.data.maps.find(m => m.id === id)?.name || `地图 ${id}`) || [];
    const inferred = monster?.reset_maps?.includes(Number(this.$('map').value));
    const summoned = monster?.summon_maps?.includes(Number(this.$('map').value));
    this.$('map-note').textContent = (maps.length > 1 ? `当前关联地图：${maps.join('、')}。同编号怪物共用该掉落配置。` :
      maps.length ? `当前关联地图：${maps[0]}。` : '选择地图和怪物，核对实际出现地点。') +
      (summoned ? '所选地图来自怪物技能召唤，需由召唤者释放技能后出现。' :
        inferred ? '所选地图由刷新候选推导，表示可能出现；请核对游戏条件。' : maps.length ? '未解析脚本的分布仍需游戏核对。' : '');
  }

  renderChoices(kind) {
    const query = this.$(`${kind}-search`).value.trim().toLowerCase();
    const selected = this.$(kind).value;
    const choices = (this.data?.[`${kind}s`] || []).filter(n =>
      (!query || `${n.label} ${n.node}`.toLowerCase().includes(query)) && (kind !== 'shop' || n.count < 32));
    this.$(kind).replaceChildren(this.option('', choices.length ? '选择配置节点' : '没有匹配的可配置节点'),
      ...choices.map(n => this.option(n.node, `${n.label} · 位置 ${n.pc}${kind === 'shop' ? ` · ${n.count}/32 项` : ''}`)));
    this.$(kind).value = choices.some(n => n.node === selected) ? selected : '';
  }

  add(kind) {
    const node = this.$(kind).value, entries = this.value[`${kind}s`];
    const count = Number(this.$('reward-count-input').value);
    if (!node) { this.$('status').textContent = '请先选择配置节点。'; this.$(kind).focus(); return; }
    if (entries.some(e => e.node === node)) { this.$('status').textContent = '该节点已添加，请勿重复添加。'; return; }
    if (entries.length >= 20) { this.$('status').textContent = '每类获取途径最多添加 20 项。'; return; }
    if (kind === 'reward' && (!Number.isInteger(count) || count < 1 || count > 32767)) {
      this.$('status').textContent = '奖励数量必须是 1 至 32767 的整数。'; return;
    }
    entries.push({node, ...(kind === 'reward' ? {count} : {})});
    this.$(`${kind}-panel`).open = true;
    this.renderLists(); this.changed();
    this.$('status').textContent = '已添加到表单，暂存后加入补丁草稿。';
  }

  renderLists() {
    for (const kind of ['shop', 'reward']) {
      const entries = this.value[`${kind}s`];
      this.$(`${kind}-count`).textContent = entries.length ? `（新增 ${entries.length} 项）` : '';
      this.$(`${kind}-list`).replaceChildren(...entries.map((entry, index) => {
        const item = this.node('li');
        const choice = this.data?.[`${kind}s`].find(n => n.node === entry.node);
        item.append(this.node('span', `${choice?.label || entry.node}${kind === 'reward' ? ` · ${entry.count} 件` : ''}`));
        const remove = this.node('button', '移除'); remove.type = 'button';
        remove.setAttribute('aria-label', `移除${choice?.label || entry.node}`);
        remove.addEventListener('click', () => { entries.splice(index, 1); this.renderLists(); this.changed(); });
        item.append(remove); return item;
      }));
      if (entries.length) this.$(`${kind}-panel`).open = true;
    }
  }

  /** Show applied shop/reward references without turning them into additions on an unrelated edit. */
  showItem(id) { this.target = id; this.showExisting(); }

  /** Expose a pending row for the source list without presenting a form change as an applied drop. */
  plannedSource() {
    if (!this.$('drop-enabled').checked || !this.$('monster').value || !this.$('map').value) return null;
    const id = Number(this.$('monster').value), mapId = Number(this.$('map').value);
    const monster = this.data?.monsters.find(m => m.id === id);
    const map = this.data?.maps.find(m => m.id === mapId);
    const chance = nearestChance(this.$('percent').value, this.data?.probabilities);
    return {id, name: monster?.name || `怪物 ${id}`, map: map?.name || `地图 ${mapId}`,
      percent: chance?.percent ?? null};
  }

  showExisting() {
    const labels = ['shops', 'rewards'].flatMap(kind => (this.data?.[kind] || []).filter(n => n.items.includes(this.target)).map(n => n.label));
    this.$('existing').textContent = labels.length ? `现有商店／奖励来源：${labels.join('；')}。下面的配置用于追加来源。` :
      '现有怪物来源见“掉落怪物”；下面的配置用于追加获取途径。';
  }

  changed() { document.getElementById('acquisition').dispatchEvent(new Event('input', {bubbles: true})); }
}
