const $ = id => document.getElementById(id);

/** Editable source settings with a server-bound validation receipt; never stores a password in browser storage. */
export class SettingsPanel {
  constructor({api, node, beforeApply, applied}) {
    Object.assign(this, {api, node, beforeApply, applied});
    this.receipt = null;
    this.dirty = false;
    this.busy = false;
    $('open-settings').addEventListener('click', () => this.open());
    $('settings-close').addEventListener('click', () => this.close());
    $('settings-form').addEventListener('submit', event => { event.preventDefault(); this.validate(); });
    $('settings-form').addEventListener('input', () => this.invalidate());
    $('settings-form').addEventListener('change', () => { this.invalidate(); this.syncMode(); });
    $('settings-save').addEventListener('click', () => this.save());
    $('settings-export').addEventListener('click', () => this.export());
    $('settings-import').addEventListener('change', event => this.import(event.target.files[0]));
    $('settings-import-button').addEventListener('click', () => $('settings-import').click());
  }

  /** Show a full settings workspace while retaining the hidden editor form untouched. */
  async open(firstRun = false) {
    if (this.busy) return;
    $('settings-panel').hidden = false;
    $('workspace-main').hidden = true;
    document.querySelector('.library-switch').hidden = true;
    $('open-settings').setAttribute('aria-expanded', 'true');
    $('settings-close').disabled = firstRun;
    try {
      if (!this.initialized || !this.dirty) {
        const data = await this.api('/api/settings');
        this.ready = data.ready;
        this.schema = data.paths;
        if (!this.initialized) this.buildPaths(data.paths);
        this.fill(data.config);
        this.passwordState = data.password_state;
        $('settings-password').placeholder = data.password_state === 'saved' ? '已保护保存；留空沿用此连接的密码' :
          data.password_state === 'unavailable' ? '已保存密码不能在此用户／机器解密，请重新输入' : '输入连接密码';
        $('settings-password').value = '';
        $('settings-close').disabled = !data.ready;
        this.initialized = true;
        this.dirty = false;
        this.invalidate('', false);
        this.message(data.setup_error || '修改数据源后，请先校验，再保存并应用。');
      }
      $('settings-title').focus();
    } catch (error) { this.message(error.message, true); }
  }

  /** Return to the exact prior item/NPC view; unvalidated settings remain only in the form. */
  close() {
    if (!this.ready || this.busy) return;
    $('settings-panel').hidden = true;
    $('workspace-main').hidden = false;
    document.querySelector('.library-switch').hidden = false;
    $('open-settings').setAttribute('aria-expanded', 'false');
  }

  /** Create labelled path inputs and native browse actions from the server's fixed schema. */
  buildPaths(fields) {
    for (const field of fields) {
      const wrapper = this.node('div', undefined, 'setting-field');
      const label = this.node('label', field.label); label.htmlFor = `setting-${field.key}`;
      const row = this.node('div', undefined, 'path-input');
      const input = this.node('input');
      Object.assign(input, {id: label.htmlFor, type: 'text', spellcheck: false, autocomplete: 'off', maxLength: 4096});
      input.placeholder = field.key === 'client_root' ? '例如 D:\\Games\\swdol' : '';
      const browse = this.node('button', '选择…'); browse.type = 'button';
      browse.setAttribute('aria-label', `选择${field.label}`);
      browse.addEventListener('click', () => this.browse(field.kind, input));
      row.append(input, browse); wrapper.append(label, row);
      const advanced = ['client_object','language','act_dir','model_dir','tex_dir','tsw_dir'].includes(field.key);
      const host = advanced ? 'settings-path-advanced' : ['map_dir','map_table','npc_snapshot'].includes(field.key) ? 'settings-npc-paths' : 'settings-path-basic';
      $(host).append(wrapper);
    }
    $('setting-npc_snapshot').closest('.setting-field').id = 'settings-snapshot-field';
  }

  /** Fill nonsecret values without generating synthetic input events or changing the validation token. */
  fill(config) {
    for (const field of this.schema) $(`setting-${field.key}`).value = config[field.key] || '';
    $('setting-npc-mode').value = config.npc_mode;
    for (const [key, value] of Object.entries(config.database)) {
      const input = $(`setting-db-${key}`);
      if (input) input.value = String(value);
    }
    $('settings-remember').checked = config.remember_password;
    this.syncMode();
  }

  /** Read only known settings fields; the password travels separately and is never exported. */
  config() {
    const config = Object.fromEntries(this.schema.map(field => [field.key, $(`setting-${field.key}`).value]));
    config.npc_mode = $('setting-npc-mode').value;
    config.remember_password = $('settings-remember').checked;
    config.database = Object.fromEntries(['host','port','name','user','timeout','client','transport','pipe'].map(key =>
      [key, ['port','timeout'].includes(key) ? Number($(`setting-db-${key}`).value) : $(`setting-db-${key}`).value]));
    return config;
  }

  /** Require fresh server validation after any edit, import or expired receipt. */
  invalidate(message = '配置已修改，请重新校验。', dirty = true) {
    this.receipt = null;
    clearTimeout(this.expiry);
    this.dirty ||= dirty;
    $('settings-save').disabled = true;
    if (message) this.message(message);
  }

  message(text, error = false) {
    $('settings-message').textContent = text;
    $('settings-message').classList.toggle('error', error);
  }

  syncMode() {
    const online = $('setting-npc-mode').value === 'database';
    $('settings-database-fields').disabled = !online || this.busy;
    $('settings-snapshot-field').hidden = online;
    const pipe = $('setting-db-transport').value === 'pipe';
    $('settings-pipe-row').hidden = !pipe;
    $('settings-tcp-port').hidden = pipe;
  }

  setBusy(value) {
    this.busy = value;
    for (const fieldset of document.querySelectorAll('#settings-form > fieldset')) fieldset.disabled = value;
    for (const id of ['settings-validate','settings-import-button','settings-export']) $(id).disabled = value;
    $('settings-save').disabled = value || !this.receipt;
    $('settings-close').disabled = value || !this.ready;
    $('settings-panel').setAttribute('aria-busy', String(value));
    this.syncMode();
  }

  /** Run actual file decoding and optional database projection; only the server can authorize saving. */
  async validate() {
    if (this.busy || !this.initialized) return;
    this.invalidate('', false);
    this.setBusy(true);
    $('settings-checks').replaceChildren();
    this.message('正在校验文件、资源和所选数据源；数据库查询可能需要一些时间…');
    try {
      const result = await this.api('/api/settings/validate', {config: this.config(), password: $('settings-password').value});
      const labels = {passed:'通过', skipped:'未启用', warning:'提示', failed:'失败'};
      $('settings-checks').replaceChildren(...result.checks.map(check => {
        const row = this.node('li', undefined, `check-result check-${check.status}`);
        const heading = this.node('div'); heading.append(this.node('strong', check.name), this.node('span', labels[check.status]));
        row.append(heading, this.node('p', check.message)); return row;
      }));
      if (result.valid) {
        this.fill(result.config);
        this.receipt = result.receipt;
        this.expiry = setTimeout(() => this.invalidate('校验结果已过期，请重新校验。', false), result.expires_in * 1000);
        this.message(result.message);
      } else this.message('校验未通过。修正失败项后重新校验，原工作区仍可使用。', true);
    } catch (error) { this.message(error.message, true); }
    finally { this.setBusy(false); }
  }

  /** Apply only an unchanged receipt; keep current unsaved item inputs until the user accepts reloading. */
  async save() {
    if (this.busy || !this.receipt || !this.beforeApply()) return;
    this.setBusy(true);
    try {
      const result = await this.api('/api/settings/save', {config: this.config(), receipt: this.receipt});
      this.message(result.message);
      $('settings-password').value = '';
      this.dirty = false;
      this.applied();
    } catch (error) { this.invalidate('', false); this.message(error.message, true); }
    finally { this.setBusy(false); }
  }

  async browse(kind, input) {
    if (this.busy) return;
    this.setBusy(true);
    try {
      const result = await this.api('/api/settings/browse', {kind});
      if (result.path) { input.value = result.path; this.invalidate(); }
    } catch (error) { this.message(error.message, true); }
    finally { this.setBusy(false); }
  }

  async export() {
    try {
      const data = await this.api('/api/settings/export');
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:'application/json'}));
      const link = this.node('a'); link.href = url; link.download = 'xyj-settings.json'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      this.message('已导出当前已保存配置，不包含密码；在新机器上导入后仍需重新校验。');
    } catch (error) { this.message(error.message, true); }
  }

  async import(file) {
    if (!file) return;
    try {
      if (file.size > 200000) throw new Error('配置文件超过 200 KB 限制。');
      const data = JSON.parse(await file.text());
      if (data.format !== 'xyj-settings-export/v1' || !data.config || !data.config.database) throw new Error('不是有效的工坊配置文件。');
      const allowed = new Set([...this.schema.map(f => f.key), 'npc_mode','remember_password','database']);
      const dbAllowed = new Set(['host','port','name','user','timeout','client','transport','pipe']);
      if (Object.keys(data.config).some(key => !allowed.has(key)) ||
          Object.keys(data.config.database).some(key => !dbAllowed.has(key))) throw new Error('配置包含未支持或敏感字段。');
      this.invalidate('', true);
      this.fill(data.config);
      $('settings-password').value = '';
      this.invalidate('已导入到表单。请重新选择本机位置、输入连接密码并校验后保存。');
    } catch (error) { this.message(error.message, true); }
    finally { $('settings-import').value = ''; }
  }
}
