const $ = id => document.getElementById(id);

/** Read-only, paginated NPC browser. Owns request tickets and never changes item forms or drafts. */
export class CreatureBrowser {
  constructor({api, node, getViewer, status, openItem}) {
    Object.assign(this, {api, node, getViewer, status, openItem});
    this.active = false;
    this.page = 0;
    this.listTicket = this.detailTicket = this.modelTicket = 0;
    $('creature-search').addEventListener('input', () => {
      clearTimeout(this.debounce);
      this.listTicket++;
      this.debounce = setTimeout(() => { this.page = 0; this.list(); }, 200);
    });
    $('creature-map').addEventListener('change', () => { this.page = 0; this.list(); });
    $('creature-prev').addEventListener('click', () => { this.page--; this.list(); });
    $('creature-next').addEventListener('click', () => { this.page++; this.list(); });
    $('creature-retry').addEventListener('click', () => this.open());
    $('creature-instance').addEventListener('change', () => this.showModel());
    $('instance-prev').addEventListener('click', () => this.select(this.selected.id, this.selected.instance_page - 1));
    $('instance-next').addEventListener('click', () => this.select(this.selected.id, this.selected.instance_page + 1));
  }

  /** Invalidate every pending request before returning the shared viewport to the item editor. */
  cancel() {
    this.active = false;
    this.listTicket++; this.detailTicket++; this.modelTicket++;
    clearTimeout(this.debounce);
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
        const option = (value, label) => { const o = this.node('option', label); o.value = value; return o; };
        $('creature-map').replaceChildren(option('all', '全部地图'), option('unplaced', `未找到地图记录（${meta.unplaced}）`),
          ...meta.maps.map(m => option(String(m.id), `${m.name}（${m.templates} 项）`)));
        this.mapNames = new Map(meta.maps.map(m => [m.id, m.name]));
        if (this.initialMap !== undefined) {
          if ([...$('creature-map').options].some(o => o.value === this.initialMap)) $('creature-map').value = this.initialMap;
          delete this.initialMap;
        }
        const source = meta.source;
        $('creature-source').replaceChildren(...[
          source.server, `快照复核：${new Date(source.captured_at).toLocaleString('zh-CN', {timeZone: 'Asia/Hong_Kong', hour12: false})}`,
          `${meta.templates} 个模板 / ${meta.instances} 条存档记录`, source.scope, source.consistency,
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
    $('creature-items').setAttribute('aria-busy', 'true');
    $('creature-prev').disabled = $('creature-next').disabled = true;
    const query = new URLSearchParams({q: $('creature-search').value, map: $('creature-map').value, page: String(this.page)});
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
          this.node('span', `#${item.id} · ${item.matched_instances} 条匹配记录 · 共 ${item.maps.length} 张地图`, 'item-meta'));
        button.append(text);
        button.addEventListener('click', () => this.select(item.id));
        return button;
      });
      $('creature-items').replaceChildren(...buttons);
      if (result.items.length) await this.select(result.items.some(i => i.id === previous) ? previous : result.items[0].id);
      else {
        this.clearDetail();
        $('creature-items').append(this.node('p', '没有匹配的NPC或怪物，请调整地图或搜索条件。', 'muted'));
      }
    } catch (error) {
      if (!this.active || ticket !== this.listTicket) return;
      this.clearDetail();
      $('creature-items').replaceChildren(this.node('p', error.message, 'error'));
      this.status(error.message, true);
    } finally {
      if (this.active && ticket === this.listTicket) $('creature-items').setAttribute('aria-busy', 'false');
    }
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
    for (const id of ['creature-maps', 'creature-drops', 'creature-instance', 'creature-saved-items']) $(id).replaceChildren();
    for (const id of ['creature-description', 'creature-position', 'creature-drop-note', 'creature-probability-note', 'instance-page']) $(id).textContent = '';
    $('creature-saved-note').textContent = '';
    $('instance-prev').disabled = $('instance-next').disabled = true;
  }

  /** Show one template and one page of saved occurrences; keep all game strings in text nodes. */
  async select(id, page = 0) {
    const ticket = ++this.detailTicket;
    this.modelTicket++;
    this.getViewer()?.clear();
    $('model-message').textContent = '正在读取角色…';
    $('creature-instance').disabled = true;
    $('instance-prev').disabled = $('instance-next').disabled = true;
    try {
      const query = new URLSearchParams({map: $('creature-map').value, page: String(page)});
      const detail = await this.api(`/api/creatures/${id}?${query}`);
      if (!this.active || ticket !== this.detailTicket) return;
      this.selected = detail;
      for (const button of $('creature-items').querySelectorAll('button')) {
        const active = Number(button.dataset.creatureId) === id;
        button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
      }
      $('preview-title').textContent = detail.name;
      $('id-tag').textContent = `模板 ${id} · 只读`;
      $('slot-label').textContent = `${detail.instance_total} 条匹配的存档记录`;
      $('creature-description').textContent = detail.description || '暂无模板说明。';
      $('creature-maps').replaceChildren(...detail.maps.map(map => {
        const button = this.node('button', `${map.name} · ${map.instances}`);
        button.addEventListener('click', () => { $('creature-map').value = String(map.id); this.page = 0; this.list(); });
        return button;
      }));
      if (!detail.maps.length) $('creature-maps').append(this.node('p', '当前快照未找到出现记录；不代表脚本中不存在。', 'muted'));
      const template = this.node('option', '模板基础外观'); template.value = '';
      $('creature-instance').replaceChildren(template, ...detail.instances.map(instance => {
        const o = this.node('option', `#${instance.id} ${instance.name} · ${this.mapNames.get(instance.map_id)}`);
        o.value = instance.key; return o;
      }));
      if (detail.instances.length) $('creature-instance').value = detail.instances[0].key;
      $('creature-instance').disabled = false;
      $('instance-page').textContent = `${detail.instance_total} 条 · ${detail.instance_page + 1} / ${detail.instance_pages}`;
      $('instance-prev').disabled = !detail.instance_page;
      $('instance-next').disabled = detail.instance_page + 1 >= detail.instance_pages;
      $('creature-drop-note').textContent = detail.drop_note;
      $('creature-probability-note').textContent = detail.probability_note;
      $('creature-drops').replaceChildren(...detail.inventory.map(item => {
        const tr = this.node('tr'), name = this.node('td');
        const text = `#${item.id} ${item.name}`;
        if (item.known) {
          const button = this.node('button', text, 'text-link');
          button.addEventListener('click', () => this.openItem(item.id)); name.append(button);
        } else name.textContent = text;
        const chance = this.node('td', `${item.check_percent.toFixed(2)}% · 阈值 ${item.threshold}${item.capacity_limited ? ' · 受前序容量限制' : ''}`);
        chance.title = `槽位 ${item.slot}，原始阈值 ${item.threshold}；仅为单次携带生成判定`;
        tr.append(name, chance, this.node('td', '未确认')); return tr;
      }));
      if (!detail.inventory.length) {
        const tr = this.node('tr'), td = this.node('td', '模板未配置初始携带物品；其他奖励来源尚未确认。');
        td.colSpan = 3; tr.append(td); $('creature-drops').append(tr);
      }
      history.replaceState(null, '', `/?library=creatures&npc=${id}&map=${encodeURIComponent($('creature-map').value)}`);
      this.status(`已打开 ${detail.name} · NPC与怪物只读查看`);
      await this.showModel();
    } catch (error) {
      if (!this.active || ticket !== this.detailTicket) return;
      this.clearDetail(); this.status(error.message, true); $('model-message').textContent = error.message;
    }
  }

  /** Bind stored item slots to the selected occurrence, preserving unknown, empty and template-only states. */
  showSavedInventory(instance) {
    const stored = instance?.saved_inventory;
    const known = stored?.status === 'stored';
    $('creature-saved-note').textContent = !instance ? '当前为模板基础外观；请选择存档实例查看携带字段。' :
      known ? `实例 #${instance.id} 的 Hand_ItemArr／Hand_CountArr 存档字段；不是在线背包或最终掉落。` :
      '此旧快照未采集实例携带字段；不能当作空背包。请在设置中更新 NPC 快照。';
    const items = known ? stored.items : [];
    $('creature-saved-items').replaceChildren(...items.map(item => {
      const row = this.node('tr'), name = this.node('td');
      const label = `#${item.id} ${item.name}`;
      if (item.known) {
        const button = this.node('button', label, 'text-link');
        button.addEventListener('click', () => this.openItem(item.id)); name.append(button);
      } else name.textContent = label;
      row.append(name, this.node('td', String(item.count)), this.node('td', String(item.slot)));
      return row;
    }));
    if (!items.length) {
      const row = this.node('tr');
      const cell = this.node('td', !instance ? '未选择实例' : known ? '该实例 9 个存档携带槽均未记录物品；不代表运行时没有物品。' : '实例携带数据未采集');
      cell.colSpan = 3; row.append(cell); $('creature-saved-items').append(row);
    }
  }

  /** Render the chosen saved equipment, rejecting stale responses after filters, selection or library changes. */
  async showModel() {
    if (!this.active || !this.selected) return;
    const detail = this.selected, ticket = ++this.modelTicket;
    const role = $('creature-instance').value;
    const instance = detail.instances.find(r => r.key === role);
    this.showSavedInventory(instance);
    $('creature-position').textContent = instance ?
      `存档名称：${instance.name}；所在地图：${this.mapNames.get(instance.map_id)}。重生配置：${this.mapNames.get(instance.revive_map_id) || `地图 ${instance.revive_map_id}`}（${instance.revive_position.join(', ')}）。重生点不是实时位置。` :
      '模板基础外观；未采用某个具体实例的装扮。';
    $('preview-title').textContent = instance?.name || detail.name;
    $('model-message').textContent = '正在读取角色模型…';
    $('model-warnings').textContent = ''; $('model-info').textContent = '';
    const viewer = this.getViewer(); viewer?.clear();
    if (!viewer) { $('model-message').textContent = '三维渲染不可用，仍可查看地图与携带配置。'; return; }
    try {
      const model = await this.api(`/api/creatures/${detail.id}/model${role ? `?role=${encodeURIComponent(role)}` : ''}`);
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
