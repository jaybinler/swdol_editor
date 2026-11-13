import {nearestChance} from './acquisition.js';

const choices = Array.from({length: 101}, (_, accepted) => ({
  threshold: accepted - 1,
  percent: (327 * accepted + Math.min(accepted, 68)) * 100 / 32768,
}));
const percent = threshold => choices[Math.max(0, Math.min(100, threshold + 1))].percent.toFixed(4);
const selectors = ['active_skill', 'passive_skill', 'weapon_particle'];

/** Own skill and particle inputs, preserving raw values from existing templates and drafts. */
export class ItemEffectsForm {
  constructor({api, node, getGeneration}) {
    Object.assign(this, {api, node, getGeneration});
    this.$ = id => document.getElementById(`item-fx-${id}`);
    this.data = {skills: [], particles: []};
    for (const key of selectors) this.$(`${key}-search`).addEventListener('input', event => {
      event.stopPropagation();
      this.options(key, Number(this.$(key).value));
    });
    this.$('retry').addEventListener('click', () => this.load());
  }

  /** Reload reference names without replacing any current form values. */
  async load() {
    this.$('retry').hidden = true;
    try {
      const data = await this.api('/api/item-effects/catalog');
      if (data.workspace_generation !== this.getGeneration()) throw new Error('数据源已切换，请刷新页面。');
      this.data = data;
      if (this.item) for (const key of selectors) this.options(key, Number(this.$(key).value));
      this.$('status').textContent = data.particle_error || '';
      this.$('retry').hidden = !data.particle_error;
    } catch (error) {
      this.$('status').textContent = error.message;
      this.$('retry').hidden = false;
    }
    this.render();
  }

  /** Use the relevant catalog for the given field. */
  references(key) { return key === 'weapon_particle' ? this.data.particles : this.data.skills; }

  /** Keep an inherited reference visible even if it is absent from the active catalog. */
  options(key, current) {
    const query = this.$(`${key}-search`).value.trim().toLowerCase();
    const clear = key === 'weapon_particle' ? 0 : -1;
    const rows = this.references(key);
    const matches = rows.filter(row => `${row.id} ${row.name}`.toLowerCase().includes(query)).slice(0, 60);
    if (current !== clear && !matches.some(row => row.id === current)) {
      matches.unshift(rows.find(row => row.id === current) || {id: current, name: '原始编号'});
    }
    const option = (id, label) => Object.assign(this.node('option', label), {value: String(id)});
    this.$(key).replaceChildren(option(clear, '无'), ...matches.map(row => option(row.id, `#${row.id} ${row.name}`)));
    this.$(key).value = String(current);
  }

  /** Load one source or saved operation, including older drafts with no effect fields. */
  set(item, stats = {}) {
    this.item = item;
    this.weapon = !!(item.slot_mask & 12);
    this.$('weapon').hidden = !this.weapon;
    this.$('weapon').disabled = !this.weapon;
    const values = {...item.stats, ...stats};
    for (const key of selectors) {
      this.$(`${key}-search`).value = '';
      this.options(key, values[key]);
    }
    this.originalThreshold = values.passive_chance;
    this.originalPercent = String(Number(percent(this.originalThreshold)));
    this.$('chance').value = this.originalPercent;
    this.originalSpeed = values.particle_speed;
    this.$('speed').min = Math.min(0, this.originalSpeed);
    this.$('speed').max = Math.max(100, this.originalSpeed);
    this.$('speed').value = this.originalSpeed;
    this.render();
  }

  /** Convert edited percentages while retaining any untouched native threshold. */
  threshold() {
    const value = this.$('chance').value;
    return value === this.originalPercent ? this.originalThreshold : nearestChance(value, choices)?.threshold;
  }

  /** Leave the property summary visible in view mode. */
  setEditing(editing) {
    this.$('fields').hidden = !editing || this.item?.type !== 2;
  }

  /** Collect only applicable native fields. The server revalidates references and ranges. */
  get() {
    if (this.item?.type !== 2) return {};
    const fields = {active_skill: Number(this.$('active_skill').value)};
    if (!this.weapon) return fields;
    const passive_chance = this.threshold();
    if (passive_chance === undefined) throw new Error('请输入 0 至 100 的释放几率。');
    return {...fields, passive_skill: Number(this.$('passive_skill').value), passive_chance,
            weapon_particle: Number(this.$('weapon_particle').value), particle_speed: this.$('speed').valueAsNumber};
  }

  /** Supply the current unsaved effect and speed to either model view. */
  preview() {
    if (!this.weapon) return {id: 0, speed: 0};
    return {id: Number(this.$('weapon_particle').value), speed: this.$('speed').valueAsNumber};
  }

  /** Present native values next to human-readable skill and effect names. */
  render() {
    const item = this.item;
    if (!item) return;
    this.$('summary').hidden = ![1, 2, 5].includes(item.type);
    const active = Number(this.$('active_skill').value);
    const passive = Number(this.$('passive_skill').value);
    const threshold = this.threshold();
    const text = (key, id) => id <= 0 ? '无' : `#${id} ${this.references(key).find(row => row.id === id)?.name || '原始编号'}`;
    const rows = [[item.type === 5 ? '学习技能' : '主动技能', text('active_skill', active)]];
    const chance = threshold === undefined ? '请输入 0 至 100' : `${percent(threshold)}%（阈值 ${threshold}）`;
    this.$('chance-note').textContent = chance;
    if (this.weapon || (item.type === 2 && passive > 0)) {
      rows.push(['被动技能', text('passive_skill', passive)], ['释放几率', passive > 0 ? chance : `未配置被动技能 · 阈值 ${threshold ?? '—'}`]);
    }
    if (this.weapon) {
      rows.push(['武器粒子效果', text('weapon_particle', this.preview().id)], ['粒子流速', this.$('speed').value || '—']);
    }
    this.$('values').replaceChildren(...rows.map(([label, value]) => {
      const row = this.node('div', undefined, 'property-row');
      row.append(this.node('dt', label), this.node('dd', value));
      return row;
    }));
  }
}
