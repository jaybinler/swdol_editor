import test from 'node:test';
import assert from 'node:assert/strict';
import {ItemDropSources} from '../editor/item-drops.js';
import {CreatureBrowser} from '../editor/creatures.js';

class Element {
  constructor(tag, text = '') {
    Object.assign(this, {tag, text, children: [], listeners: {}, dataset: {}, attributes: {}, value: '',
      hidden: false, disabled: false, classList: {toggle() {}}});
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; this.text = ''; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() {}
  scrollIntoView() {}
  querySelectorAll(tag) { return this.children.flatMap(child => [...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag)]); }
  querySelector(selector) {
    const id = selector.match(/data-creature-id="(\d+)"/)?.[1];
    return this.querySelectorAll('button').find(child => String(child.dataset.creatureId) === id);
  }
  get textContent() { return [this.text, ...this.children.map(child => child.textContent)].join(' '); }
  set textContent(value) { this.text = value; this.children = []; }
}
const node = (tag, text, className) => Object.assign(new Element(tag, text), {className});
const root = () => {
  const elements = new Map();
  return {getElementById(id) { if (!elements.has(id)) elements.set(id, new Element('div')); return elements.get(id); }};
};
const payload = (id, name = '测试怪物', extra = {}) => ({item_id: id, page: 0, pages: 1, total: 1,
  items: [{id: 20003, name, maps: [{id: 2, name: '测试地图'}]}], workspace_generation: null, ...extra});
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };

test('late success and late failure cannot overwrite the current item', async () => {
  const first = deferred(), second = deferred(), dom = root();
  const view = new ItemDropSources({root: dom, node, getGeneration: () => null,
    api: path => path.includes('/10/') ? first.promise : second.promise, openCreature: async () => {}});
  const old = view.show({id: 10, draft: false});
  const current = view.show({id: 20, draft: false});
  second.resolve(payload(20, '当前怪物')); await current;
  first.reject(new Error('旧请求失败')); await old;
  assert.match(dom.getElementById('item-drop-rows').textContent, /当前怪物/);
  assert.equal(dom.getElementById('item-drop-retry').hidden, true);
  const third = deferred(), fourth = deferred();
  view.api = path => path.includes('/30/') ? third.promise : fourth.promise;
  const stale = view.show({id: 30}); const latest = view.show({id: 40});
  fourth.resolve(payload(40, '最新怪物')); await latest;
  third.resolve(payload(30, '过期怪物')); await stale;
  assert.match(dom.getElementById('item-drop-rows').textContent, /最新怪物/);
  assert.doesNotMatch(dom.getElementById('item-drop-rows').textContent, /过期怪物/);
});

test('same-identity form changes do not refetch; hidden requests are ignored and return can reload', async () => {
  const pending = deferred(), dom = root(); let count = 0;
  const view = new ItemDropSources({root: dom, node, getGeneration: () => null,
    api: async () => { count++; return pending.promise; }, openCreature: async () => {}});
  const first = view.show({id: 10, draft: false});
  view.show({id: 10, draft: false});
  assert.equal(count, 1);
  view.cancel(); pending.resolve(payload(10, '隐藏后的结果')); await first;
  assert.doesNotMatch(dom.getElementById('item-drop-rows').textContent, /隐藏后的结果/);
  await view.show({id: 10, draft: false});
  assert.equal(count, 2);
  assert.match(dom.getElementById('item-drop-rows').textContent, /隐藏后的结果/);
});

test('snapshot failure offers retry while empty new IDs never inherit source relations', async () => {
  const dom = root(); let calls = 0;
  const view = new ItemDropSources({root: dom, node, getGeneration: () => null,
    api: async () => { calls++; throw new Error('快照不可用'); }, openCreature: async () => {}});
  await view.show({id: 319, draft: true});
  assert.equal(dom.getElementById('item-drop-retry').hidden, false);
  assert.match(dom.getElementById('item-drop-rows').textContent, /读取失败/);
  view.api = async () => payload(319, '', {total: 0, items: []});
  await dom.getElementById('item-drop-retry').listeners.click();
  assert.match(dom.getElementById('item-drop-rows').textContent, /新增道具尚未配置/);
  assert.equal(dom.getElementById('item-drop-retry').hidden, true);
  view.show({reason: '请选择有效的新道具 ID。'});
  assert.equal(calls, 1);
  assert.match(dom.getElementById('item-drop-rows').textContent, /有效的新道具 ID/);
});

test('pagination and source links request exact IDs; a workspace mismatch is an error rather than empty', async () => {
  const dom = root(), paths = [], opened = [];
  const view = new ItemDropSources({root: dom, node, getGeneration: () => 7,
    api: async path => { paths.push(path); return payload(10, '怪物名称', {pages: 2, total: 61, workspace_generation: 7, page: path.endsWith('=1') ? 1 : 0}); },
    openCreature: async id => { opened.push(id); }});
  await view.show({id: 10});
  await dom.getElementById('item-drop-rows').querySelectorAll('button')[0].listeners.click();
  assert.deepEqual(opened, [20003]);
  await view.load(1);
  assert.match(paths[1], /\/api\/item\/10\/drop-sources\?page=1$/);
  assert.equal(dom.getElementById('item-drop-next').disabled, true);
  view.api = async () => payload(20, '', {workspace_generation: 6});
  await view.show({id: 20});
  assert.match(dom.getElementById('item-drop-rows').textContent, /数据源已切换/);
  assert.equal(dom.getElementById('item-drop-retry').hidden, false);
});

test('exact NPC navigation resets old filters and discards out-of-order selections', async () => {
  const previous = globalThis.document, dom = root(); globalThis.document = dom;
  try {
    const a = deferred(), b = deferred(), requests = [], selected = [];
    const browser = new CreatureBrowser({node, getViewer: () => null, status() {}, openItem: async () => {},
      api: path => { requests.push(path); return path.includes('template_id=20000') ? a.promise : b.promise; }});
    browser.meta = {};
    browser.select = async id => { selected.push(id); browser.selected = {id}; };
    dom.getElementById('creature-map').value = '99';
    dom.getElementById('creature-search').value = '旧名称';
    browser.page = 8;
    const old = browser.openTemplate(20000);
    const latest = browser.openTemplate(20003);
    assert.equal(dom.getElementById('creature-map').value, 'all');
    assert.equal(dom.getElementById('creature-search').value, '20003');
    assert.equal(browser.page, 0);
    b.resolve({page: 0, pages: 1, total: 1, items: [{id: 20003, name: '目标', maps: []}]}); await latest;
    a.resolve({page: 0, pages: 1, total: 1, items: [{id: 20000, name: '旧目标', maps: []}]}); await old;
    assert.deepEqual(selected, [20003]);
    assert.ok(requests.every(path => path.includes('map=all') && path.includes('template_id=')));
    dom.getElementById('creature-search').listeners.input();
    assert.equal(browser.exactTemplateId, null);
    browser.cancel();
  } finally { globalThis.document = previous; }
});
