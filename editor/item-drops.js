/** Paginated template-configured monster sources; owns requests, never item form or draft state. */
export class ItemDropSources {
  constructor({api, node, openCreature, getGeneration, root = document}) {
    Object.assign(this, {api, node, openCreature, getGeneration});
    this.$ = id => root.getElementById(id);
    this.ticket = 0;
    this.active = false;
    this.loading = false;
    this.$('item-drop-prev').addEventListener('click', () => { if (!this.loading) this.load(this.page - 1); });
    this.$('item-drop-next').addEventListener('click', () => { if (!this.loading) this.load(this.page + 1); });
    this.$('item-drop-retry').addEventListener('click', () => this.load(this.page));
  }

  /** Invalidate pending responses when the item section is hidden. */
  cancel() {
    this.active = false;
    this.loading = false;
    this.ticket++;
  }

  /** Switch identities immediately; repeated form edits for the same ID do not issue another request. */
  show(target) {
    const key = JSON.stringify(target);
    if (this.active && key === this.key) return;
    this.active = true;
    this.key = key;
    this.target = target;
    this.page = 0;
    this.ticket++;
    if (target.planned) {
      const source = target.planned, row = this.node('tr'), name = this.node('td');
      const button = this.node('button', source.name, 'text-link');
      button.dataset.dropCreatureId = String(source.id);
      button.addEventListener('click', async () => {
        try { await this.openCreature(source.id); }
        catch (error) { this.$('item-drop-note').textContent = error.message; }
      });
      name.append(button); row.append(name, this.node('td', String(source.id)), this.node('td', source.map));
      this.$('item-drop-rows').replaceChildren(row);
      this.$('item-drop-count').textContent = '1 种 · 待发布';
      this.$('item-drop-note').textContent = `${target.unsaved ? '表单未暂存' : '已暂存，待配套发布'}。` +
        `${source.percent === null ? '掉率待填写' : `实际掉率 ${source.percent.toFixed(4)}%`}，成功掉落 1 件。`;
      this.$('item-drop-page').textContent = '1 / 1';
      this.$('item-drop-prev').disabled = this.$('item-drop-next').disabled = true;
      this.$('item-drop-retry').hidden = true;
      this.$('item-drop-sources').setAttribute('aria-busy', 'false');
      this.loading = false;
      return;
    }
    if (target.reason) {
      this.loading = false;
      this.message(target.reason);
      this.$('item-drop-count').textContent = '—';
      this.$('item-drop-note').textContent = '按当前 NPC／怪物模板物品配置查询。';
      this.$('item-drop-page').textContent = '—';
      this.$('item-drop-prev').disabled = this.$('item-drop-next').disabled = true;
      this.$('item-drop-retry').hidden = true;
      this.$('item-drop-sources').setAttribute('aria-busy', 'false');
      return;
    }
    return this.load(0);
  }

  /** Replace stale rows with one safe text message. */
  message(text) {
    const row = this.node('tr'), cell = this.node('td', text);
    cell.colSpan = 3;
    row.append(cell);
    this.$('item-drop-rows').replaceChildren(row);
  }

  /** Fetch a bounded page, discarding errors and results from an old item, workspace or hidden section. */
  async load(page = 0) {
    if (!this.active || !this.target?.id || this.target.reason || this.target.planned) return;
    const ticket = ++this.ticket;
    const target = this.target;
    this.page = Math.max(0, page);
    this.loading = true;
    this.$('item-drop-sources').setAttribute('aria-busy', 'true');
    this.$('item-drop-prev').disabled = this.$('item-drop-next').disabled = true;
    this.$('item-drop-retry').hidden = true;
    this.$('item-drop-count').textContent = '—';
    this.$('item-drop-page').textContent = '—';
    this.$('item-drop-note').textContent = `正在查询道具 ID ${target.id}…`;
    this.message('正在读取掉落怪物…');
    try {
      const data = await this.api(`/api/item/${target.id}/drop-sources?page=${this.page}`);
      if (!this.active || ticket !== this.ticket) return;
      if (data.workspace_generation !== this.getGeneration()) throw new Error('数据源已切换，请刷新页面后重试。');
      this.page = data.page;
      this.$('item-drop-count').textContent = `${data.total} 种`;
      this.$('item-drop-page').textContent = `${data.page + 1} / ${data.pages}`;
      this.$('item-drop-prev').disabled = !data.page;
      this.$('item-drop-next').disabled = data.page + 1 >= data.pages;
      this.$('item-drop-note').textContent = '来自模板配置；点击怪物查看详情。未覆盖全部脚本奖励。';
      this.$('item-drop-rows').replaceChildren(...data.items.map(creature => {
        const row = this.node('tr'), name = this.node('td');
        const button = this.node('button', creature.name, 'text-link');
        button.dataset.dropCreatureId = String(creature.id);
        button.setAttribute('aria-label', `查看 ${creature.name}（编号 ${creature.id}）`);
        button.addEventListener('click', async () => {
          try { await this.openCreature(creature.id); }
          catch (error) { if (this.active && ticket === this.ticket) this.$('item-drop-note').textContent = error.message; }
        });
        name.append(button);
        row.append(name, this.node('td', String(creature.id)),
          this.node('td', creature.maps.map(map => map.name).join(' / ') || '未找到地图记录'));
        return row;
      }));
      if (!data.total) this.message(target.draft ? '新增道具尚未配置怪物掉落。' : '当前数据未找到配置该道具的怪物。');
    } catch (error) {
      if (!this.active || ticket !== this.ticket) return;
      this.message(`读取失败：${error.message}`);
      this.$('item-drop-note').textContent = '无法读取 NPC 数据，可重试或在设置中核对数据源。道具编辑仍可使用。';
      this.$('item-drop-retry').hidden = false;
    } finally {
      if (this.active && ticket === this.ticket) {
        this.loading = false;
        this.$('item-drop-sources').setAttribute('aria-busy', 'false');
      }
    }
  }
}
