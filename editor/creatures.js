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
        $('creature-map-note').textContent = meta.script_map_coverage?.available ?
          '按存档及已解析脚本分类；同一怪物可出现在多张地图。' :
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
          this.node('span', `#${item.id} · 共 ${item.maps.length} 张地图`, 'item-meta'));
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
    for (const id of ['creature-maps', 'creature-drops']) $(id).replaceChildren();
    $('creature-description').textContent = '';
  }

  /** Show one template's maps and generated item probabilities; keep game strings in text nodes. */
  async select(id) {
    const ticket = ++this.detailTicket;
    this.modelTicket++;
    this.getViewer()?.clear();
    $('model-message').textContent = '正在读取角色…';
    try {
      const query = new URLSearchParams({map: $('creature-map').value});
      const detail = await this.api(`/api/creatures/${id}?${query}`);
      if (!this.active || ticket !== this.detailTicket) return;
      this.selected = detail;
      for (const button of $('creature-items').querySelectorAll('button')) {
        const active = Number(button.dataset.creatureId) === id;
        button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
      }
      $('preview-title').textContent = detail.name;
      $('id-tag').textContent = `模板 ${id} · 只读`;
      $('slot-label').textContent = `共 ${detail.maps.length} 张地图`;
      $('creature-description').textContent = detail.description || '暂无模板说明。';
      $('creature-maps').replaceChildren(...detail.maps.map(map => {
        const button = this.node('button', map.name);
        button.addEventListener('click', () => { $('creature-map').value = String(map.id); this.page = 0; this.list(); });
        return button;
      }));
      if (!detail.maps.length) $('creature-maps').append(this.node('p', '当前快照未找到出现记录；不代表脚本中不存在。', 'muted'));
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
      history.replaceState(null, '', `/?library=creatures&npc=${id}&map=${encodeURIComponent($('creature-map').value)}`);
      this.status(`已打开 ${detail.name} · NPC与怪物只读查看`);
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
