const $ = id => document.getElementById(id);

/** Read-only, paginated NPC browser. Owns request tickets and never changes item forms or drafts. */
export class CreatureBrowser {
  constructor({api, node, getViewer, status, openItem, beforeSelect = () => true, onSelected = () => {}}) {
    Object.assign(this, {api, node, getViewer, status, openItem, beforeSelect, onSelected});
    this.active = false;
    this.page = 0;
    this.listTicket = this.detailTicket = this.modelTicket = 0;
    $('creature-search').addEventListener('input', () => {
      this.exactTemplateId = null;
      clearTimeout(this.debounce);
      this.listTicket++;
      this.detailTicket++;
      this.showListLoading();
      this.debounce = setTimeout(() => { this.page = 0; this.list(); }, 200);
    });
    $('creature-map').addEventListener('change', () => { clearTimeout(this.debounce); this.page = 0; this.list(); });
    $('creature-prev').addEventListener('click', () => { this.page--; this.list(); });
    $('creature-next').addEventListener('click', () => { this.page++; this.list(); });
    $('creature-retry').addEventListener('click', () => this.open());
  }

  /** Invalidate every pending request before returning the shared viewport to the item editor. */
  cancel() {
    this.active = false;
    this.listTicket++; this.detailTicket++; this.modelTicket++;
    clearTimeout(this.debounce);
  }

  /** Focus one exact template independently of old search/map filters, without touching item state. */
  async openTemplate(id) {
    clearTimeout(this.debounce);
    this.detailTicket++;
    this.clearDetail();
    this.exactTemplateId = id;
    this.page = 0;
    $('creature-search').value = String(id);
    $('creature-map').value = 'all';
    delete this.initialMap;
    await this.open();
    if (this.active && this.selected?.id === id) {
      const heading = $('creature-properties-title');
      heading.tabIndex = -1;
      heading.focus({preventScroll: true});
      $('creature-items').querySelector(`[data-creature-id="${id}"]`)?.scrollIntoView({block: 'nearest'});
    }
  }

  /** Load map names and provenance once; a failed snapshot can be retried without reloading item drafts. */
  async open() {
    this.active = true;
    const ticket = ++this.listTicket;
    try {
      if (!this.meta) {
        const meta = await this.api('/api/creatures/catalog');
        if (!this.active || ticket !== this.listTicket) return;
        this.meta = meta;
        $('creature-map-note').textContent = meta.summon_map_coverage?.available ?
          '按存档、脚本、刷新候选及技能召唤分类；候选与召唤关系表示可能出现。' : meta.reset_map_coverage?.available ?
          '按存档、脚本及刷新候选分类；刷新候选表示可能出现。' : meta.script_map_coverage?.available ?
          '按存档及已解析脚本分类；重启新版服务后可补充刷新候选。' :
          '当前快照仅含存档地图；更新快照可补充脚本生成地图。';
        const option = (value, label) => { const o = this.node('option', label); o.value = value; return o; };
        $('creature-map').replaceChildren(option('all', '全部地图'), option('unplaced', `未找到地图记录（${meta.unplaced}）`),
          ...meta.maps.map(m => option(String(m.id), `${m.name}（${m.templates} 项）`)));
        if (this.initialMap !== undefined) {
          if ([...$('creature-map').options].some(o => o.value === this.initialMap)) $('creature-map').value = this.initialMap;
          delete this.initialMap;
        }
        const source = meta.source;
        $('creature-source').replaceChildren(...[
          source.server, `快照复核：${new Date(source.captured_at).toLocaleString('zh-CN', {timeZone: 'Asia/Hong_Kong', hour12: false})}`,
          `${meta.templates} 个模板 / ${meta.instances} 条存档记录`, source.scope, source.consistency,
          meta.reset_map_coverage?.available ?
            `刷新候选补全 ${meta.reset_map_coverage.newly_placed_templates} 个原未归类模板；跨地图复活记录不据此继承地图。` :
            '刷新候选未启用；新版服务需 v3 快照核验原生版本。',
          meta.summon_map_coverage?.available ?
            `技能召唤补全 ${meta.summon_map_coverage.newly_placed_templates} 个原未归类模板；继承召唤者的已知地图。` :
            '技能召唤地图未启用；需新版服务与 v3 快照核验原生版本。',
        ].map(text => this.node('p', text)));
      }
      if (!this.active || ticket !== this.listTicket) return;
      $('creature-retry').hidden = true;
      await this.list();
    } catch (error) {
      if (!this.active || ticket !== this.listTicket) return;
      $('creature-items').replaceChildren(this.node('p', error.message, 'error'));
      $('creature-retry').hidden = false;
      this.status(error.message, true);
    }
  }

  /** Request a bounded page and keep map filtering separate from a template's multiple memberships. */
  async list() {
    if (!this.active) return;
    const ticket = ++this.listTicket;
    this.detailTicket++; this.modelTicket++;
    const previous = this.selected?.id;
    this.showListLoading();
    const query = new URLSearchParams({q: $('creature-search').value, map: $('creature-map').value, page: String(this.page)});
    if (this.exactTemplateId != null) query.set('template_id', String(this.exactTemplateId));
    try {
      const result = await this.api(`/api/creatures?${query}`);
      if (!this.active || ticket !== this.listTicket) return;
      this.page = result.page;
      $('creature-count').textContent = `${result.total} 项`;
      $('creature-page').textContent = `${result.page + 1} / ${result.pages}`;
      $('creature-prev').disabled = !result.page;
      $('creature-next').disabled = result.page + 1 >= result.pages;
      const buttons = result.items.map(item => {
        const button = this.node('button', undefined, 'item creature-item');
        button.dataset.creatureId = item.id;
        const text = this.node('span', undefined, 'item-text');
        text.append(this.node('span', item.name, 'item-name'),
          this.node('span', `#${item.id} · 共 ${item.maps.length} 张地图`, 'item-meta'));
        button.append(text);
        button.addEventListener('click', () => this.select(item.id));
        return button;
      });
      $('creature-items').replaceChildren(...buttons);
      if (result.items.length) await this.select(result.items.some(i => i.id === previous) ? previous : result.items[0].id);
      else await this.showEmpty(query, ticket);
    } catch (error) {
      if (!this.active || ticket !== this.listTicket) return;
      this.clearDetail();
      $('creature-items').replaceChildren(this.node('p', error.message, 'error'));
      this.status(error.message, true);
    } finally {
      if (this.active && ticket === this.listTicket) $('creature-items').setAttribute('aria-busy', 'false');
    }
  }

  /** Remove the previous creature's identity as soon as search/filter input invalidates it. */
  showListLoading() {
    this.clearDetail();
    this.syncLocation();
    $('creature-items').replaceChildren(this.node('p', '正在查询NPC与怪物…', 'muted'));
    $('creature-items').setAttribute('aria-busy', 'true');
    $('creature-count').textContent = $('creature-page').textContent = '—';
    $('creature-prev').disabled = $('creature-next').disabled = true;
    this.status('正在查询NPC与怪物…');
  }

  /** Explain an exact ID hidden by a map filter using fresh catalog evidence, without changing the filter. */
  async showEmpty(query, ticket) {
    const message = '当前地图筛选下没有匹配的NPC或怪物。';
    $('creature-items').replaceChildren(this.node('p', message, 'muted'));
    this.status(message);
    const search = query.get('q').trim();
    if (query.get('map') === 'all' || !/^\d{1,10}$/.test(search) || Number(search) < 1 || Number(search) > 2147483647) return;
    try {
      const lookup = new URLSearchParams({q: String(Number(search)), map: 'all', page: '0'});
      const result = await this.api(`/api/creatures?${lookup}`);
      if (!this.active || ticket !== this.listTicket) return;
      const item = result.items.find(row => row.id === Number(search));
      if (!item) return;
      const maps = item.maps.map(id => this.meta?.maps?.find(map => map.id === id)?.name || `地图 ${id}`);
      const explanation = `#${item.id} ${item.name}${maps.length ? `已记录地图：${maps.join('、')}` : '尚无地图记录'}。当前地图筛选未包含该编号。`;
      const button = this.node('button', `查看 #${item.id}（全部地图）`, 'text-link');
      button.addEventListener('click', () => {
        if (this.active && ticket === this.listTicket) return this.openTemplate(item.id);
      });
      $('creature-items').replaceChildren(this.node('p', explanation, 'muted'), button);
      this.status(explanation);
    } catch (error) {
      if (!this.active || ticket !== this.listTicket) return;
      $('creature-items').append(this.node('p', `无法核对该编号的其他地图：${error.message}`, 'error'));
      this.status('地图核对失败，请重新搜索或切换全部地图重试。', true);
    }
  }

  /** Keep the URL consistent with the visible selection, including empty and loading search states. */
  syncLocation(id = null) {
    const query = new URLSearchParams({library: 'creatures', map: $('creature-map').value});
    if (id !== null) query.set('npc', String(id));
    else if ($('creature-search').value.trim()) query.set('q', $('creature-search').value.trim());
    globalThis.history?.replaceState(null, '', `/?${query}`);
  }

  /** Remove stale identity and detail content when a filtered page is empty or fails. */
  clearDetail() {
    this.selected = null;
    this.modelTicket++;
    this.getViewer()?.clear();
    $('preview-title').textContent = '选择NPC或怪物';
    $('id-tag').textContent = '只读';
    $('slot-label').textContent = $('model-info').textContent = $('model-warnings').textContent = '';
    $('model-message').textContent = '请选择一条记录。';
    for (const id of ['creature-maps', 'creature-drops', 'creature-resets']) $(id).replaceChildren();
    $('creature-reset-note').textContent = '';
    $('creature-description').textContent = '';
  }

  /** Show one template's maps and generated item probabilities; keep game strings in text nodes. */
  async select(id) {
    const generation = this.detailTicket;
    const choice = this.beforeSelect(id);
    const allowed = choice && typeof choice.then === 'function' ? await choice : choice;
    if (!allowed || generation !== this.detailTicket || !this.active) return;
    const ticket = ++this.detailTicket;
    this.clearDetail();
    this.syncLocation();
    $('model-message').textContent = '正在读取角色…';
    try {
      const query = new URLSearchParams({map: $('creature-map').value});
      const detail = await this.api(`/api/creatures/${id}?${query}`);
      if (!this.active || ticket !== this.detailTicket) return;
      this.selected = detail;
      const notified = this.onSelected(id);
      if (notified && typeof notified.then === 'function') await notified;
      if (!this.active || ticket !== this.detailTicket) return;
      for (const button of $('creature-items').querySelectorAll('button')) {
        const active = Number(button.dataset.creatureId) === id;
        button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
      }
      $('preview-title').textContent = detail.name;
      $('id-tag').textContent = `原始模板 ${id}`;
      $('slot-label').textContent = `共 ${detail.maps.length} 张地图`;
      $('creature-description').textContent = detail.description || '暂无模板说明。';
      $('creature-maps').replaceChildren(...detail.maps.map(map => {
        const button = this.node('button', map.name);
        const labels = {saved: '直接存档', script: '脚本生成', reset: '刷新候选', summon: '技能召唤'};
        button.title = `来源：${(map.sources || []).map(source => labels[source] || source).join('、')}`;
        button.addEventListener('click', () => { $('creature-map').value = String(map.id); this.page = 0; this.list(); });
        if (map.reset_sources?.length || map.summon_sources?.length) {
          const group = this.node('div', undefined, 'map-reset-membership');
          group.append(button);
          if (map.reset_sources?.length) {
            const path = map.reset_sources[0].path.map(tid => `#${tid}`).join(' → ');
            group.append(this.node('p', `刷新候选：${path}（可能出现）`, 'property-note map-reset-source'));
          }
          if (map.summon_sources?.length) {
            const source = map.summon_sources[0];
            const note = this.node('p', '技能召唤：', 'property-note map-reset-source');
            const caster = this.node('button', `#${source.caster_template_id} ${source.caster_name}`, 'text-link');
            caster.addEventListener('click', () => this.openTemplate(source.caster_template_id));
            note.append(caster, this.node('span', ` 使用 #${source.skill_id} ${source.skill_name}（可能出现）`));
            group.append(note);
          }
          return group;
        }
        return button;
      }));
      if (!detail.maps.length) $('creature-maps').append(this.node('p', '当前索引未找到地图来源；不代表游戏中不存在。', 'muted'));
      $('creature-reset-note').textContent = detail.reset_candidates_available ?
        '重置时从 5 个槽位选择；重复候选保留，-1 表示沿用当前模板。候选地图关联不代表实时数量。' :
        '刷新候选尚未启用。请重启新版编辑器；旧快照需更新至 v3 以核验服务端版本。';
      $('creature-resets').replaceChildren(...(detail.reset_candidates || []).map(candidate => {
        const row = this.node('tr'), cell = this.node('td');
        if (candidate.valid) {
          const button = this.node('button', `#${candidate.id} ${candidate.name}`, 'text-link');
          button.addEventListener('click', () => this.openTemplate(candidate.id));
          cell.append(button);
          if (candidate.is_self) cell.append(this.node('span', candidate.raw_id === -1 ? '（-1：当前模板）' : '（当前模板）', 'muted'));
        } else cell.textContent = `无效候选 #${candidate.raw_id}；重置时保留当前模板`;
        row.append(this.node('td', String(candidate.slot)), cell); return row;
      }));
      $('creature-drops').replaceChildren(...detail.inventory.map(item => {
        const tr = this.node('tr'), name = this.node('td');
        const text = `#${item.id} ${item.name}`;
        if (item.known) {
          const button = this.node('button', text, 'text-link');
          button.addEventListener('click', () => this.openItem(item.id)); name.append(button);
        } else name.textContent = text;
        tr.append(name, this.node('td', `${item.check_percent.toFixed(2)}%`)); return tr;
      }));
      if (!detail.inventory.length) {
        const tr = this.node('tr'), td = this.node('td', '未配置掉落道具。');
        td.colSpan = 2; tr.append(td); $('creature-drops').append(tr);
      }
      this.syncLocation(id);
      this.status('');
      await this.showModel();
    } catch (error) {
      if (!this.active || ticket !== this.detailTicket) return;
      this.clearDetail(); this.status(error.message, true); $('model-message').textContent = error.message;
    }
  }

  /** Render the template appearance, rejecting stale responses after filters, selection or library changes. */
  async showModel() {
    if (!this.active || !this.selected) return;
    const detail = this.selected, ticket = ++this.modelTicket;
    $('preview-title').textContent = detail.name;
    $('model-message').textContent = '正在读取角色模型…';
    $('model-warnings').textContent = ''; $('model-info').textContent = '';
    const viewer = this.getViewer(); viewer?.clear();
    if (!viewer) { $('model-message').textContent = '三维渲染不可用，仍可查看地图与掉落道具。'; return; }
    try {
      const model = await this.api(`/api/creatures/${detail.id}/model`);
      if (!this.active || ticket !== this.modelTicket) return;
      await viewer.show(model, message => {
        if (this.active && ticket === this.modelTicket) $('model-warnings').textContent += ` ${message}`;
      });
      if (!this.active || ticket !== this.modelTicket) return;
      viewer.wireframe($('wireframe').checked);
      $('model-message').textContent = '';
      $('model-info').textContent = `${model.mode_label} · ${model.vertices} 顶点 / ${model.triangles} 三角面`;
      $('model-warnings').textContent = model.warnings.join(' ');
    } catch (error) {
      if (!this.active || ticket !== this.modelTicket) return;
      $('model-message').textContent = error.message;
      $('model-info').textContent = '该外观暂不能预览；地图与物品配置仍可查看';
    }
  }
}
