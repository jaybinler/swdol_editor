import {
  ChevronLeft,
  ChevronRight,
  RotateCcw,
  RotateCw,
  Scan,
  ZoomIn,
  ZoomOut,
} from 'lucide-react';
import { Button, Input, NativeSelect, Table, Textarea } from './ui/controls.jsx';

/** Render the settings content with stable form and viewport mounts. */
export function SettingsContent() {
  return (
    <section id="settings-panel" className="settings-panel" aria-labelledby="settings-title" hidden>
      <div className="settings-heading">
        <div>
          <h2 id="settings-title" tabIndex="-1">
            数据源设置
          </h2>
        </div>
        <Button id="settings-close">返回工坊</Button>
      </div>
      <p id="settings-message" role="status" aria-live="polite">
        正在读取配置…
      </p>
      <form id="settings-form">
        <fieldset>
          <legend>游戏文件</legend>
          <div id="settings-path-basic" className="settings-grid"></div>
          <details className="settings-advanced">
            <summary>高级路径</summary>
            <p className="property-note">
              留空时从客户端根目录定位 Main、Act、MODEL、TEX 和
              Tsw。可填写本机路径或已登录的共享路径。
            </p>
            <div id="settings-path-advanced" className="settings-grid"></div>
          </details>
        </fieldset>
        <fieldset>
          <legend>NPC与地图数据</legend>
          <label htmlFor="setting-npc-mode">数据来源</label>
          <NativeSelect id="setting-npc-mode">
            <option value="offline">离线快照（不要求数据库在线）</option>
            <option value="database">从数据库生成本地快照</option>
          </NativeSelect>
          <p className="property-note">
            数据库模式在校验时读取两张 NPC
            表并生成快照；浏览时仍使用本地快照。离线模式使用快照内的地图名称。
          </p>
          <div id="settings-npc-paths" className="settings-grid"></div>
        </fieldset>
        <fieldset id="settings-database-fields">
          <legend>数据库连接</legend>
          <div className="settings-grid">
            <div className="setting-field">
              <label htmlFor="setting-db-transport">连接方式</label>
              <NativeSelect id="setting-db-transport">
                <option value="tcp">TCP（本机或服务器）</option>
                <option value="pipe">本机 Windows 命名管道</option>
              </NativeSelect>
            </div>
            <div className="setting-field">
              <label htmlFor="setting-db-host">主机</label>
              <Input id="setting-db-host" maxLength="253" autoComplete="off" />
            </div>
            <div className="setting-field" id="settings-tcp-port">
              <label htmlFor="setting-db-port">端口</label>
              <Input id="setting-db-port" type="number" min="1" max="65535" />
            </div>
            <div className="setting-field" id="settings-pipe-row" hidden>
              <label htmlFor="setting-db-pipe">命名管道名称</label>
              <Input id="setting-db-pipe" maxLength="100" autoComplete="off" />
            </div>
            <div className="setting-field">
              <label htmlFor="setting-db-name">数据库名</label>
              <Input id="setting-db-name" maxLength="64" autoComplete="off" />
            </div>
            <div className="setting-field">
              <label htmlFor="setting-db-user">数据库账号</label>
              <Input id="setting-db-user" maxLength="256" autoComplete="off" />
            </div>
            <div className="setting-field">
              <label htmlFor="settings-password">密码</label>
              <Input id="settings-password" type="password" maxLength="4096" autoComplete="off" />
            </div>
            <div className="setting-field">
              <label htmlFor="setting-db-timeout">连接超时（秒）</label>
              <Input id="setting-db-timeout" type="number" min="1" max="15" />
            </div>
            <div className="setting-field">
              <label htmlFor="setting-db-client">兼容 MySQL 客户端 mysql.exe</label>
              <Input id="setting-db-client" maxLength="4096" autoComplete="off" />
              <p className="property-note">
                当前核验 MySQL 4.0.27 原工具客户端，其他版本会明确拒绝执行。
              </p>
            </div>
          </div>
          <label className="filter">
            <input type="checkbox" id="settings-remember" />
            使用 Windows 保护保存密码
          </label>
          <p className="property-note">密码绑定当前 Windows 用户，导出不含密码；换机后重新输入。</p>
        </fieldset>
        <div className="settings-actions">
          <Button id="settings-validate" type="submit">
            校验配置
          </Button>
          <Button id="settings-save" type="button" disabled variant="default">
            保存并应用
          </Button>
          <Button id="settings-export" type="button">
            导出已保存配置
          </Button>
          <Button id="settings-import-button" type="button">
            导入配置
          </Button>
          <input id="settings-import" type="file" accept=".json,application/json" hidden />
        </div>
      </form>
      <h3 className="settings-results-title">校验结果</h3>
      <ul id="settings-checks" className="settings-checks" aria-live="polite"></ul>
    </section>
  );
}

/** Render the item library with stable form and viewport mounts. */
export function ItemLibrary() {
  return (
    <aside className="library" id="item-library" aria-label="道具库">
      <div className="section-title">
        <h2>道具库</h2>
        <span id="item-count" className="mono">
          —
        </span>
      </div>
      <Button id="new-item" disabled variant="default">
        新增道具
      </Button>
      <section
        id="template-picker"
        className="template-picker"
        aria-labelledby="template-picker-title"
        hidden
      >
        <h3 id="template-picker-title" tabIndex="-1">
          选择新增模板
        </h3>
        <p className="muted">按名称、ID 或分类查找，再点击下方道具。新增将复用模板图标和模型。</p>
        <p id="template-error" className="error" role="alert"></p>
        <Button id="cancel-template" type="button">
          取消选择
        </Button>
      </section>
      <label className="search-label" htmlFor="search">
        搜索名称或 ID
      </label>
      <Input id="search" type="search" placeholder="例如：桃木剑 / 318" autoComplete="off" />
      <label className="search-label" htmlFor="category">
        道具分类
      </label>
      <NativeSelect id="category" aria-label="道具分类">
        <option value="all">全部道具</option>
      </NativeSelect>
      <label className="filter">
        <input type="checkbox" id="only-editable" defaultChecked /> 只显示道具
      </label>
      <div id="items" className="items" aria-label="搜索结果"></div>
      <div className="pagination">
        <Button id="prev" size="icon" title="上一页" aria-label="上一页">
          <ChevronLeft aria-hidden="true" />
        </Button>
        <span id="page-info">—</span>
        <Button id="next" size="icon" title="下一页" aria-label="下一页">
          <ChevronRight aria-hidden="true" />
        </Button>
      </div>
      <details className="sources">
        <summary>数据来源与版本</summary>
        <div id="sources"></div>
      </details>
    </aside>
  );
}

/** Render the creature library with stable form and viewport mounts. */
export function CreatureLibrary() {
  return (
    <aside className="library" id="creature-library" aria-label="NPC与怪物库" hidden>
      <div className="section-title">
        <h2>NPC与怪物</h2>
        <span id="creature-count" className="mono">
          —
        </span>
      </div>
      <label className="search-label" htmlFor="creature-search">
        搜索名称或 ID
      </label>
      <Input
        id="creature-search"
        type="search"
        maxLength="128"
        placeholder="例如：蝙蝠 / 20000"
        autoComplete="off"
      />
      <label className="search-label" htmlFor="creature-map">
        出现地图
      </label>
      <NativeSelect id="creature-map">
        <option value="all">全部地图</option>
      </NativeSelect>
      <p id="creature-map-note" className="muted creature-caption">
        按存档、脚本、刷新候选及技能召唤分类；同一怪物可关联多张地图。
      </p>
      <div
        id="creature-items"
        className="items"
        aria-label="NPC与怪物搜索结果"
        aria-live="polite"
      ></div>
      <div className="pagination">
        <Button id="creature-prev" size="icon" title="上一页" aria-label="上一页">
          <ChevronLeft aria-hidden="true" />
        </Button>
        <span id="creature-page">—</span>
        <Button id="creature-next" size="icon" title="下一页" aria-label="下一页">
          <ChevronRight aria-hidden="true" />
        </Button>
      </div>
      <Button id="creature-retry" hidden>
        重新读取NPC快照
      </Button>
      <details className="sources">
        <summary>NPC数据来源</summary>
        <div id="creature-source"></div>
      </details>
    </aside>
  );
}

/** Render the preview content with stable form and viewport mounts. */
export function PreviewContent() {
  return (
    <section className="preview" aria-labelledby="preview-title">
      <div className="preview-head">
        <div className="item-identity">
          <div className="detail-icon">
            <img id="detail-icon" alt="道具游戏图标" width="80" height="80" />
            <span id="icon-error" hidden>
              暂无图标
            </span>
          </div>
          <div>
            <h2 id="preview-title">选择道具</h2>
            <p id="slot-label" className="muted">
              —
            </p>
            <p id="model-info" className="muted">
              —
            </p>
          </div>
        </div>
        <span id="id-tag" className="tag mono">
          ID —
        </span>
      </div>
      <div className="preview-settings">
        <label htmlFor="preview-mode">
          预览方式
          <NativeSelect id="preview-mode" aria-label="预览方式">
            <option value="equipped">人物穿戴</option>
            <option value="single">单件外观</option>
          </NativeSelect>
        </label>
        <label htmlFor="profile">
          基础人物
          <NativeSelect id="profile" aria-label="基础人物">
            <option value="auto">自动匹配</option>
          </NativeSelect>
        </label>
      </div>
      <div id="viewport" aria-label="道具三维预览">
        <div id="model-message" role="status">
          正在准备三维预览…
        </div>
      </div>
      <div className="view-controls" aria-label="预览控制">
        <Button id="rotate-left" size="icon" title="左转" aria-label="左转">
          <RotateCcw aria-hidden="true" />
        </Button>
        <Button id="rotate-right" size="icon" title="右转" aria-label="右转">
          <RotateCw aria-hidden="true" />
        </Button>
        <Button id="zoom-in" size="icon" title="放大" aria-label="放大">
          <ZoomIn aria-hidden="true" />
        </Button>
        <Button id="zoom-out" size="icon" title="缩小" aria-label="缩小">
          <ZoomOut aria-hidden="true" />
        </Button>
        <Button id="reset-view" size="icon" title="复位" aria-label="复位">
          <Scan aria-hidden="true" />
        </Button>
        <label>
          <input id="wireframe" type="checkbox" /> 线框
        </label>
      </div>

      <details className="preview-warnings">
        <summary>预览说明</summary>
        <p id="model-warnings" className="preview-note"></p>
      </details>
      <p id="particle-preview-note" className="preview-note" aria-live="polite"></p>
      <section
        id="item-drop-sources"
        className="item-drop-sources"
        aria-labelledby="item-drop-title"
        aria-busy="false"
      >
        <div className="section-title">
          <h2 id="item-drop-title">掉落怪物</h2>
          <span id="item-drop-count">—</span>
        </div>
        <p id="item-drop-note" className="property-note" role="status">
          选择道具后查看掉落怪物。
        </p>
        <Table className="item-source-table">
          <thead>
            <tr>
              <th scope="col">怪物</th>
              <th scope="col">编号</th>
              <th scope="col">出现地图</th>
            </tr>
          </thead>
          <tbody id="item-drop-rows"></tbody>
        </Table>
        <div className="pagination">
          <Button
            id="item-drop-prev"
            disabled
            size="icon"
            title="掉落怪物上一页"
            aria-label="掉落怪物上一页"
          >
            <ChevronLeft aria-hidden="true" />
          </Button>
          <span id="item-drop-page">—</span>
          <Button
            id="item-drop-next"
            disabled
            size="icon"
            title="掉落怪物下一页"
            aria-label="掉落怪物下一页"
          >
            <ChevronRight aria-hidden="true" />
          </Button>
        </div>
        <Button id="item-drop-retry" hidden>
          重新读取掉落怪物
        </Button>
      </section>
    </section>
  );
}

/** Render the item properties with stable form and viewport mounts. */
export function ItemProperties() {
  return (
    <section className="properties" id="properties" aria-labelledby="properties-title">
      <div className="section-title">
        <h2 id="properties-title">道具属性</h2>
        <span id="edit-state" className="tag">
          加载中
        </span>
      </div>
      <div className="item-actions">
        <Button id="edit-item" disabled>
          编辑道具
        </Button>
        <Button id="clone-item" disabled>
          以此为模板新增
        </Button>
      </div>

      <p id="template-source" className="template-source" hidden></p>
      <section id="template-properties" aria-label="模板属性" aria-busy="true">
        <section
          id="weapon-summary"
          className="property-group"
          aria-labelledby="weapon-title"
          hidden
        >
          <h3 id="weapon-title">武器属性</h3>
          <dl id="weapon-properties" className="property-grid"></dl>
        </section>
        <section className="property-group" aria-labelledby="requirements-title">
          <h3 id="requirements-title">适用条件</h3>
          <dl id="requirements" className="property-list">
            <div>
              <dt>状态</dt>
              <dd>待选择道具</dd>
            </div>
          </dl>
        </section>
        <section className="property-group" aria-labelledby="resistances-title">
          <h3 id="resistances-title">抗性</h3>
          <dl id="resistances" className="property-grid"></dl>
        </section>
        <section className="property-group" aria-labelledby="hidden-title">
          <h3 id="hidden-title">隐藏属性与其他加成</h3>
          <dl id="hidden-attributes" className="property-list"></dl>
          <p id="instance-note" className="property-note"></p>
        </section>
        <section id="item-fx-summary" className="property-group" hidden>
          <h3>技能与武器粒子</h3>
          <dl id="item-fx-values" className="property-list"></dl>
        </section>
      </section>
      <form id="form">
        <fieldset id="editor-fields" disabled>
          <label htmlFor="target-id" id="target-id-label">
            道具 ID
          </label>
          <div className="id-controls">
            <Input
              id="target-id"
              type="number"
              min="1"
              max="32767"
              step="1"
              required
              readOnly
              aria-describedby="id-hint"
            />
            <Button id="suggest-id" type="button" hidden>
              推荐空闲 ID
            </Button>
          </div>
          <p id="id-hint" className="property-note" aria-live="polite"></p>
          <label htmlFor="item-name">
            名称 <span className="muted">GBK 最多 15 字节</span>
          </label>
          <Input id="item-name" required />
          <label htmlFor="description">
            说明 <span className="muted">GBK 最多 199 字节</span>
          </label>
          <Textarea id="description" rows="3"></Textarea>
          <div id="weapon-fields" hidden>
            <div className="section-title stats-title">
              <h3>武器属性</h3>
            </div>
            <div id="weapon-stats" className="stats"></div>
          </div>
          <section id="item-fx-fields" className="property-group" hidden>
            <h3>技能与武器粒子</h3>
            <p id="item-fx-status" className="property-note" role="status"></p>
            <Button id="item-fx-retry" type="button" hidden>
              重新读取技能与粒子
            </Button>
            <label htmlFor="item-fx-active_skill-search">查找主动技能</label>
            <Input
              id="item-fx-active_skill-search"
              type="search"
              maxLength="80"
              placeholder="名称或 ID"
            />
            <label htmlFor="item-fx-active_skill">主动技能</label>
            <NativeSelect id="item-fx-active_skill"></NativeSelect>
            <fieldset id="item-fx-weapon" className="item-fx-weapon" hidden>
              <label htmlFor="item-fx-passive_skill-search">查找被动技能</label>
              <Input
                id="item-fx-passive_skill-search"
                type="search"
                maxLength="80"
                placeholder="名称或 ID"
              />
              <label htmlFor="item-fx-passive_skill">被动技能</label>
              <NativeSelect id="item-fx-passive_skill"></NativeSelect>
              <label htmlFor="item-fx-chance">释放几率（%）</label>
              <Input
                id="item-fx-chance"
                type="number"
                min="0"
                max="100"
                step="any"
                required
                aria-describedby="item-fx-chance-note"
              />
              <p id="item-fx-chance-note" className="property-note"></p>
              <label htmlFor="item-fx-weapon_particle-search">查找武器粒子</label>
              <Input
                id="item-fx-weapon_particle-search"
                type="search"
                maxLength="80"
                placeholder="名称或 ID"
              />
              <label htmlFor="item-fx-weapon_particle">武器粒子效果</label>
              <NativeSelect id="item-fx-weapon_particle"></NativeSelect>
              <label htmlFor="item-fx-speed">粒子流速</label>
              <Input id="item-fx-speed" type="number" min="0" max="100" step="1" required />
            </fieldset>
          </section>
          <div className="section-title stats-title">
            <h3>基础属性</h3>
          </div>
          <div id="stats" className="stats"></div>
          <div className="section-title stats-title">
            <h3>抗性</h3>
          </div>
          <div id="resistance-stats" className="stats"></div>
          <div className="section-title stats-title">
            <h3>回复属性</h3>
          </div>
          <div id="recovery-stats" className="stats"></div>
          <section id="acquisition" className="acquisition" aria-labelledby="acquisition-title">
            <h3 id="acquisition-title">获取途径</h3>

            <p id="acquisition-status" className="property-note" role="status" aria-live="polite">
              正在读取可配置来源…
            </p>
            <Button id="acquisition-retry" type="button" hidden>
              重新读取获取途径
            </Button>
            <div id="acquisition-existing" className="property-note"></div>
            <label className="filter">
              <input id="acquisition-drop-enabled" type="checkbox" /> 添加怪物掉落
            </label>
            <div id="acquisition-drop-fields" hidden>
              <label htmlFor="acquisition-map">出现地图</label>
              <NativeSelect id="acquisition-map">
                <option value="">选择地图</option>
              </NativeSelect>
              <label htmlFor="acquisition-monster-search">查找怪物</label>
              <Input
                id="acquisition-monster-search"
                type="search"
                placeholder="名称或编号"
                maxLength="128"
              />
              <label htmlFor="acquisition-monster">掉落怪物（最多一种）</label>
              <NativeSelect id="acquisition-monster">
                <option value="">选择怪物</option>
              </NativeSelect>
              <p id="acquisition-map-note" className="property-note"></p>
              <label htmlFor="acquisition-percent">掉落率（%）</label>
              <Input
                id="acquisition-percent"
                type="number"
                min="0"
                max="100"
                step="any"
                defaultValue="10"
              />
              <p id="acquisition-chance" className="property-note" aria-live="polite"></p>
              <p className="property-note">
                成功掉落固定 1 件。地图用于选择来源；同编号怪物共用掉落配置。
              </p>
            </div>
            <details className="acquisition-channel" id="acquisition-shop-panel">
              <summary>
                商店购买 <span id="acquisition-shop-count"></span>
              </summary>
              <p className="property-note">
                加入已有商品表，保留原商品。购买价格沿用道具模板与游戏原有定价规则。
              </p>
              <label htmlFor="acquisition-shop-search">查找商店／商品</label>
              <Input
                id="acquisition-shop-search"
                type="search"
                maxLength="128"
                placeholder="NPC、商品名称或脚本编号"
              />
              <label htmlFor="acquisition-shop">商品配置节点</label>
              <NativeSelect id="acquisition-shop"></NativeSelect>
              <Button id="acquisition-add-shop" type="button">
                添加商店
              </Button>
              <ul id="acquisition-shop-list" className="acquisition-list"></ul>
            </details>
            <details className="acquisition-channel" id="acquisition-reward-panel">
              <summary>
                任务奖励 <span id="acquisition-reward-count"></span>
              </summary>
              <p className="property-note">
                在已有物品奖励节点成功后追加，沿用原任务条件和领取规则。请按原奖励及关联 NPC
                核对节点。
              </p>
              <label htmlFor="acquisition-reward-search">查找奖励／NPC</label>
              <Input
                id="acquisition-reward-search"
                type="search"
                maxLength="128"
                placeholder="原奖励名称、NPC 或脚本编号"
              />
              <label htmlFor="acquisition-reward">物品奖励节点</label>
              <NativeSelect id="acquisition-reward"></NativeSelect>
              <label htmlFor="acquisition-reward-count-input">每次奖励数量</label>
              <Input
                id="acquisition-reward-count-input"
                type="number"
                min="1"
                max="32767"
                step="1"
                defaultValue="1"
              />
              <Button id="acquisition-add-reward" type="button">
                添加任务奖励
              </Button>
              <ul id="acquisition-reward-list" className="acquisition-list"></ul>
            </details>
          </section>
          <details className="raw">
            <summary>外观关联（只读）</summary>
            <p id="appearance"></p>
          </details>
          <p id="form-error" className="error" role="alert"></p>
          <div className="form-actions" id="form-actions" hidden>
            <Button id="stage" type="submit" variant="default">
              暂存修改
            </Button>
            <Button id="restore" type="button">
              恢复表单
            </Button>
            <Button id="cancel-edit" type="button">
              返回查看
            </Button>
          </div>
        </fieldset>
      </form>
      <p id="readonly-reason" className="muted"></p>
    </section>
  );
}

/** Render the creature properties with stable form and viewport mounts. */
export function CreatureProperties() {
  return (
    <section
      className="properties"
      id="creature-properties"
      aria-labelledby="creature-properties-title"
      hidden
    >
      <div className="section-title">
        <h2 id="creature-properties-title">NPC与怪物详情</h2>
      </div>
      <p id="creature-description" className="muted"></p>
      <section id="creature-authoring" className="creature-authoring" aria-label="怪物新增与编辑">
        <div className="item-actions">
          <Button id="npc-edit" disabled>
            编辑怪物
          </Button>
          <Button id="npc-clone" disabled>
            新增地图变体
          </Button>
        </div>
        <p id="npc-mode-note" className="property-note" role="status">
          选择模板后可编辑，或使用新编号新增地图变体。
        </p>
        <p id="npc-error" className="error" role="alert"></p>
        <form id="npc-form">
          <fieldset id="npc-fields" disabled>
            <h3 id="npc-form-title">怪物配置</h3>
            <div className="npc-field-grid">
              <label>
                模板 ID
                <Input id="npc-target" type="number" min="1" max="32767" required readOnly />
              </label>
              <label>
                名称（GBK 最多 12 字节）
                <Input id="npc-name" required />
              </label>
              <label>
                等级
                <Input id="npc-level" type="number" min="1" max="100" required />
              </label>
              <label>
                性别
                <NativeSelect id="npc-sex">
                  <option value="0">女／母</option>
                  <option value="1">男／公</option>
                </NativeSelect>
              </label>
            </div>
            <label>
              说明<Textarea id="npc-description" rows="2"></Textarea>
            </label>
            <details className="property-group">
              <summary>基础属性</summary>
              <div id="npc-attributes" className="npc-field-grid"></div>
            </details>
            <details className="property-group" open>
              <summary>外观</summary>
              <label>
                外观来源编号
                <Input
                  id="npc-appearance"
                  type="number"
                  list="npc-appearance-options"
                  min="1"
                  max="32767"
                  required
                />
              </label>
              <datalist id="npc-appearance-options"></datalist>
              <div className="npc-field-grid npc-scale">
                <label>
                  宽度比例
                  <Input id="npc-scale-x" type="number" min="0.25" max="4" step="0.01" required />
                </label>
                <label>
                  高度比例
                  <Input id="npc-scale-y" type="number" min="0.25" max="4" step="0.01" required />
                </label>
                <label>
                  厚度比例
                  <Input id="npc-scale-z" type="number" min="0.25" max="4" step="0.01" required />
                </label>
              </div>
              <Button id="npc-preview" type="button">
                预览当前外观
              </Button>
              <Button id="npc-model-edit" type="button">
                修改模型与贴图
              </Button>
              <Button id="npc-model-reset" type="button" hidden>
                恢复当前怪物外观
              </Button>
              <p id="npc-model-state" className="property-note"></p>
            </details>
            <details className="property-group" open>
              <summary>刷新规则</summary>
              <label>
                基础间隔（秒）
                <Input id="npc-respawn" type="number" min="0.7" max="1800000" step="0.1" required />
              </label>
              <p id="npc-timing" className="property-note" aria-live="polite"></p>
              <p className="property-note">
                不同地图需要不同间隔时，请新增可见的地图变体。以下候选若切到其他模板，下一次间隔按实际模板计算。
              </p>
              <div id="npc-candidates" className="npc-field-grid"></div>
            </details>
            <details className="property-group">
              <summary>掉落道具（12 槽）</summary>
              <p className="property-note">
                输入物品编号与掉率；编号为 0 或 -1 表示空槽。修改后最多 8
                个有效候选。掉率显示原生实际值。
              </p>
              <datalist id="npc-item-options"></datalist>
              <div id="npc-drop-fields" className="npc-slot-fields"></div>
            </details>
            <details className="property-group">
              <summary>可释放技能（16 槽）</summary>
              <p className="property-note">
                0 或 -1 为空槽；保留重复槽位，不将重复次数直接解释为施放概率。
              </p>

              <datalist id="npc-skill-options"></datalist>
              <div id="npc-skill-fields" className="npc-field-grid"></div>
            </details>
            <details className="property-group" open>
              <summary>地图生成点</summary>
              <p className="property-note">
                新增点复制所选普通 NPC
                的存档配置；请核对原脚本和初次生成状态。数量按独立存档对象展开。
              </p>
              <label>
                地图<NativeSelect id="npc-point-map"></NativeSelect>
              </label>
              <label>
                存档来源<NativeSelect id="npc-point-seed"></NativeSelect>
              </label>
              <p id="npc-point-source" className="property-note"></p>
              <div className="npc-field-grid">
                <label>
                  操作
                  <NativeSelect id="npc-point-action">
                    <option value="add">新增点</option>
                    <option value="update">移动已有点</option>
                    <option value="remove">移除已有点</option>
                  </NativeSelect>
                </label>
                <label>
                  数量
                  <Input id="npc-point-count" type="number" min="1" max="50" defaultValue="1" />
                </label>
              </div>
              <div className="npc-field-grid">
                <label>
                  X<Input id="npc-point-x" type="number" step="any" />
                </label>
                <label>
                  Y<Input id="npc-point-y" type="number" step="any" />
                </label>
                <label>
                  Z<Input id="npc-point-z" type="number" step="any" />
                </label>
                <label>
                  朝向（度）
                  <Input
                    id="npc-point-dir"
                    type="number"
                    min="0"
                    max="359.999"
                    step="any"
                    defaultValue="0"
                  />
                </label>
              </div>
              <p id="npc-point-pending" className="property-note" role="status" hidden>
                这些点位输入尚未加入修改列表。
              </p>
              <Button id="npc-add-point" type="button">
                加入点位修改
              </Button>
              <Button id="npc-reset-point" type="button" hidden>
                放弃本次点位输入
              </Button>
              <div id="npc-point-changes"></div>
            </details>
            <div className="form-actions">
              <Button type="submit" id="npc-stage" variant="default">
                暂存怪物
              </Button>
              <Button type="button" id="npc-restore">
                恢复表单
              </Button>
              <Button type="button" id="npc-view">
                返回查看
              </Button>
            </div>
          </fieldset>
        </form>
      </section>
      <section className="property-group">
        <h3>出现地图</h3>
        <div id="creature-maps" className="map-memberships"></div>
      </section>
      <details className="property-group" id="creature-reset-panel">
        <summary>刷新候选（5 槽）</summary>
        <p id="creature-reset-note" className="property-note"></p>
        <div className="drop-table-wrap">
          <Table className="drop-table reset-table">
            <thead>
              <tr>
                <th scope="col">槽位</th>
                <th scope="col">候选怪物</th>
              </tr>
            </thead>
            <tbody id="creature-resets"></tbody>
          </Table>
        </div>
      </details>
      <section className="property-group">
        <h3>掉落道具</h3>
        <div className="drop-table-wrap">
          <Table className="drop-table">
            <thead>
              <tr>
                <th scope="col">道具</th>
                <th scope="col">掉率</th>
              </tr>
            </thead>
            <tbody id="creature-drops"></tbody>
          </Table>
        </div>
      </section>
    </section>
  );
}

/** Render the item draft content with stable form and viewport mounts. */
export function ItemDraftContent() {
  return (
    <section className="draft-area" aria-label="道具草稿">
      <p id="draft-count" className="muted">
        0 个操作
      </p>
      <div id="draft-list"></div>
      <div id="export-result"></div>
    </section>
  );
}

/** Render the creature draft content with stable form and viewport mounts. */
export function CreatureDraftContent() {
  return (
    <section className="npc-draft-area" aria-label="怪物草稿">
      <div className="section-title">
        <p id="npc-draft-count" className="muted">
          0 项
        </p>
        <Button id="npc-reload" type="button" variant="ghost">
          重新读取
        </Button>
      </div>
      <div id="npc-draft-list"></div>
      <p id="npc-export-result" className="property-note" role="status"></p>
    </section>
  );
}

/** Render the discard dialog with stable form and viewport mounts. */
export function DiscardDialog() {
  return (
    <dialog id="discard-dialog" aria-labelledby="discard-title" aria-describedby="discard-message">
      <form method="dialog">
        <h2 id="discard-title">有未暂存的内容</h2>
        <p id="discard-message"></p>
        <div className="dialog-actions">
          <Button value="keep" autoFocus>
            保留输入
          </Button>
          <Button value="discard" variant="default">
            放弃并继续
          </Button>
        </div>
      </form>
    </dialog>
  );
}

/** Render the publication dialog with stable form and viewport mounts. */
export function PublicationDialog() {
  return (
    <dialog
      id="publication-dialog"
      aria-labelledby="publication-title"
      aria-describedby="publication-summary"
    >
      <form method="dialog">
        <h2 id="publication-title">确认怪物发布</h2>
        <p id="publication-summary"></p>
        <label className="publication-confirm">
          <input id="publication-stopped" type="checkbox" required />
          相关游戏服务和客户端已停止
        </label>
        <div className="dialog-actions">
          <Button type="button" id="publication-cancel">
            取消
          </Button>
          <Button value="publish" variant="default">
            确认并继续
          </Button>
        </div>
      </form>
    </dialog>
  );
}
