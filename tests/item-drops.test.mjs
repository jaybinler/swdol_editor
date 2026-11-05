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

test('a configured pending source replaces empty-state text and rejects late baseline responses', async () => {
  const pending = deferred(), dom = root(), opened = [];
  const view = new ItemDropSources({root:dom,node,getGeneration:()=>null,api:()=>pending.promise,
    openCreature:async id=>opened.push(id)});
  const old = view.show({id:319,draft:true});
  view.show({id:319,draft:true,planned:{id:20071,name:'盘古',map:'通天柱',percent:5.0048828125},unsaved:false});
  pending.resolve(payload(319,'',{total:0,items:[]})); await old;
  assert.match(dom.getElementById('item-drop-rows').textContent,/盘古.*20071.*通天柱/);
  assert.match(dom.getElementById('item-drop-note').textContent,/已暂存，待配套发布/);
  assert.match(dom.getElementById('item-drop-count').textContent,/待发布/);
  await dom.getElementById('item-drop-rows').querySelectorAll('button')[0].listeners.click();
  assert.deepEqual(opened,[20071]);
});

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

test('reset candidate detail keeps duplicate slots, labels provenance, links exact IDs and clears stale rows', async () => {
  const previousDocument=globalThis.document, previousHistory=globalThis.history, dom=root();
  globalThis.document=dom; globalThis.history={replaceState(){}};
  try {
    const opened=[];
    const detail={id:20088,name:'器奴',description:'模板说明',maps:[{id:18,name:'通天柱',sources:['reset'],
      reset_sources:[{path:[20009,20088]}]}],inventory:[],reset_candidates_available:true,
      reset_candidates:[{slot:1,raw_id:20009,id:20009,name:'器奴',is_self:false,valid:true},
        {slot:2,raw_id:20009,id:20009,name:'器奴',is_self:false,valid:true},
        {slot:3,raw_id:-1,id:20088,name:'器奴',is_self:true,valid:true},
        {slot:4,raw_id:20982,id:20982,name:'极·器奴',is_self:false,valid:true},
        {slot:5,raw_id:0,id:0,name:'无效',is_self:false,valid:false}]};
    const browser=new CreatureBrowser({node,getViewer:()=>null,status(){},openItem:async()=>{},api:async()=>detail});
    browser.active=true;
    await browser.select(20088);
    assert.match(dom.getElementById('creature-maps').textContent,/#20009 → #20088/);
    assert.match(dom.getElementById('creature-maps').textContent,/可能出现/);
    assert.equal(dom.getElementById('creature-resets').children.length,5);
    assert.match(dom.getElementById('creature-resets').textContent,/-1：当前模板/);
    assert.match(dom.getElementById('creature-resets').textContent,/无效候选 #0/);
    browser.openTemplate=async id=>opened.push(id);
    await dom.getElementById('creature-resets').querySelectorAll('button')[1].listeners.click();
    assert.deepEqual(opened,[20009]);
    browser.clearDetail();
    assert.equal(dom.getElementById('creature-resets').children.length,0);
    assert.equal(dom.getElementById('creature-reset-note').textContent,'');
    browser.cancel();
  } finally {
    globalThis.document=previousDocument;
    if(previousHistory===undefined) delete globalThis.history; else globalThis.history=previousHistory;
  }
});

test('a filtered exact ID shows its evidenced map and offers navigation without claiming it is unplaced', async () => {
  const previousDocument=globalThis.document, previousHistory=globalThis.history, dom=root();
  const urls=[], statuses=[], pending=deferred(), paths=[];
  globalThis.document=dom; globalThis.history={replaceState(_state,_title,url){urls.push(url);}};
  try {
    const browser=new CreatureBrowser({node,getViewer:()=>null,status:text=>statuses.push(text),openItem:async()=>{},
      api:async path=>{paths.push(path);return path.includes('map=all') ?
        {items:[{id:20048,name:'阿修罗',maps:[16]}]} : pending.promise;}});
    browser.active=true; browser.meta={maps:[{id:16,name:'九曲结界'}]}; browser.selected={id:20968};
    dom.getElementById('preview-title').textContent='旧怪物';
    dom.getElementById('creature-search').value='20048'; dom.getElementById('creature-map').value='unplaced';
    const request=browser.list();
    assert.equal(browser.selected,null);
    assert.doesNotMatch(dom.getElementById('preview-title').textContent,/旧怪物/);
    assert.match(urls.at(-1),/map=unplaced&q=20048/); assert.doesNotMatch(urls.at(-1),/npc=/);
    pending.resolve({page:0,pages:1,total:0,items:[]}); await request;
    assert.equal(dom.getElementById('creature-map').value,'unplaced');
    assert.match(dom.getElementById('creature-items').textContent,/#20048 阿修罗已记录地图：九曲结界/);
    assert.match(statuses.at(-1),/当前地图筛选未包含该编号/);
    assert.equal(paths.length,2);
    const opened=[]; browser.openTemplate=async id=>opened.push(id);
    await dom.getElementById('creature-items').querySelectorAll('button')[0].listeners.click();
    assert.deepEqual(opened,[20048]); browser.cancel();
  } finally {globalThis.document=previousDocument;globalThis.history=previousHistory;}
});

test('late map lookup cannot overwrite a new search, its URL or status', async () => {
  const previousDocument=globalThis.document, previousHistory=globalThis.history, dom=root();
  const pending=deferred(), statuses=[], urls=[];
  globalThis.document=dom; globalThis.history={replaceState(_state,_title,url){urls.push(url);}};
  try {
    const browser=new CreatureBrowser({node,getViewer:()=>null,status:text=>statuses.push(text),openItem:async()=>{},
      api:async path=>path.includes('map=all') ? pending.promise : {page:0,pages:1,total:0,items:[]}});
    browser.active=true; browser.meta={maps:[{id:16,name:'九曲结界'}]};
    dom.getElementById('creature-search').value='20048'; dom.getElementById('creature-map').value='unplaced';
    const old=browser.list(); await new Promise(resolve=>setImmediate(resolve));
    dom.getElementById('creature-search').value='不存在的名字'; const latest=browser.list(); await latest;
    pending.resolve({items:[{id:20048,name:'阿修罗',maps:[16]}]}); await old;
    assert.doesNotMatch(dom.getElementById('creature-items').textContent,/阿修罗/);
    assert.match(statuses.at(-1),/当前地图筛选下没有匹配/);
    assert.match(decodeURIComponent(urls.at(-1)),/q=不存在的名字/);
    assert.equal(dom.getElementById('creature-items').attributes['aria-busy'],'false'); browser.cancel();
  } finally {globalThis.document=previousDocument;globalThis.history=previousHistory;}
});

test('empty ID lookup and failed lookup never invent map relationships', async () => {
  const previousDocument=globalThis.document, dom=root(); globalThis.document=dom;
  try {
    let fail=false;
    const browser=new CreatureBrowser({node,getViewer:()=>null,status(){},openItem:async()=>{},api:async path=>{
      if(path.includes('map=all')) {if(fail) throw new Error('快照读取失败'); return {items:[]};}
      return {page:0,pages:1,total:0,items:[]};
    }});
    browser.active=true; dom.getElementById('creature-search').value='29999'; dom.getElementById('creature-map').value='unplaced';
    await browser.list();
    assert.equal(dom.getElementById('creature-items').querySelectorAll('button').length,0);
    assert.doesNotMatch(dom.getElementById('creature-items').textContent,/已记录地图/);
    fail=true; await browser.list();
    assert.match(dom.getElementById('creature-items').textContent,/无法核对.*快照读取失败/);
    assert.equal(dom.getElementById('creature-items').querySelectorAll('button').length,0); browser.cancel();
  } finally {globalThis.document=previousDocument;}
});

test('search input immediately clears old details and rejects their in-flight response', async () => {
  const previousDocument=globalThis.document, previousHistory=globalThis.history, dom=root(), pending=deferred();
  globalThis.document=dom; globalThis.history={replaceState(){}};
  try {
    const browser=new CreatureBrowser({node,getViewer:()=>null,status(){},openItem:async()=>{},api:()=>pending.promise});
    browser.active=true; dom.getElementById('creature-map').value='unplaced';
    const request=browser.select(20968);
    dom.getElementById('creature-search').value='20048'; dom.getElementById('creature-search').listeners.input();
    assert.equal(browser.selected,null);
    assert.match(dom.getElementById('creature-items').textContent,/正在查询/);
    pending.resolve({id:20968,name:'旧怪物',maps:[],inventory:[]}); await request;
    assert.doesNotMatch(dom.getElementById('preview-title').textContent,/旧怪物/);
    assert.equal(browser.selected,null); browser.cancel();
  } finally {globalThis.document=previousDocument;globalThis.history=previousHistory;}
});

test('summoned creature map shows caster and skill evidence and opens the exact caster ID', async () => {
  const previousDocument=globalThis.document, previousHistory=globalThis.history, dom=root();
  globalThis.document=dom;globalThis.history={replaceState(){}};
  try {
    const detail={id:20355,name:'魔礼海元神',maps:[{id:18,name:'通天柱',sources:['summon'],
      summon_sources:[{caster_template_id:20086,caster_name:'魔礼海',skill_id:11058,skill_name:'元神出窍'}]}],inventory:[]};
    const browser=new CreatureBrowser({node,getViewer:()=>null,status(){},openItem:async()=>{},api:async()=>detail});
    browser.active=true;await browser.select(20355);
    assert.match(dom.getElementById('creature-maps').textContent,/通天柱.*技能召唤.*#20086 魔礼海.*#11058 元神出窍/);
    const opened=[];browser.openTemplate=async id=>opened.push(id);
    await dom.getElementById('creature-maps').querySelectorAll('button')[1].listeners.click();
    assert.deepEqual(opened,[20086]);browser.cancel();
  } finally {globalThis.document=previousDocument;globalThis.history=previousHistory;}
});
