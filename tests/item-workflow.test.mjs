import test from 'node:test';
import assert from 'node:assert/strict';
import {operationMode, targetIdError, replaceDraftOperation, displayProperties, weaponProperties, hasWeaponProperties} from '../editor/item-workflow.js';

test('weapon visibility follows weapon/offhand category rather than equipment type or nonzero values', () => {
  for (const slot of ['mainhand', 'offhand']) {
    assert.equal(hasWeaponProperties({type:2, categories:[slot, 'weapon'], stats:{damage:0,delay:0,range:0}}), true);
  }
  for (const category of ['head', 'body', 'throwing', 'treasure', 'ordinary', 'other']) {
    assert.equal(hasWeaponProperties({type:2, categories:[category], stats:{damage:70,delay:32,range:90}}), false);
  }
  assert.equal(hasWeaponProperties(null), false);
  assert.equal(hasWeaponProperties({type:2}), false);
});

test('weapon overview preserves zero and negative values and falls back for invalid form input', () => {
  const baseline = {damage:70, delay:32, range:90};
  const current = {damage:0, delay:-2147483648, range:-32768};
  assert.deepEqual(weaponProperties(baseline, current).map(field => field.value), [0,-2147483648,-32768]);
  assert.deepEqual(weaponProperties(baseline, {damage:NaN, delay:undefined}).map(field => field.value), [70,32,90]);
  assert.deepEqual(weaponProperties(baseline, current, false).map(field => field.value), [null,null,null]);
  assert.deepEqual(baseline, {damage:70, delay:32, range:90});
  assert.deepEqual(current, {damage:0, delay:-2147483648, range:-32768});
});

const edit = (source, target, name = '测试') => ({source_id: source, target_id: target, name});

test('legacy operations recover their explicit edit or create mode', () => {
  assert.equal(operationMode(edit(10, 10)), 'edit');
  assert.equal(operationMode(edit(10, 319)), 'create');
});

test('new operations cannot overwrite another clone of the same template', () => {
  const drafts = [edit(10, 319, '原草稿')];
  assert.throws(() => replaceDraftOperation(drafts, edit(10, 319)), /其他草稿/);
  assert.equal(drafts[0].name, '原草稿');
  assert.deepEqual(replaceDraftOperation(drafts, edit(10, 320)), [...drafts, edit(10, 320)]);
});

test('moving an opened clone keeps unrelated drafts and rejects occupied destinations', () => {
  const drafts = [edit(10, 319), edit(10, 320), edit(10, 10)];
  assert.throws(() => replaceDraftOperation(drafts, edit(10, 320), 319), /其他草稿/);
  assert.deepEqual(replaceDraftOperation(drafts, edit(10, 321), 319), [drafts[1], drafts[2], edit(10, 321)]);
  assert.equal(drafts.length, 3);
});

test('reopening an existing edit replaces only its own operation', () => {
  const drafts = [edit(10, 10), edit(10, 319)];
  assert.deepEqual(replaceDraftOperation(drafts, edit(10, 10, '改名'), 10), [drafts[1], edit(10, 10, '改名')]);
});

test('ID validation distinguishes source records, other drafts and the opened operation', () => {
  const ids = new Set([10, 318]);
  const drafts = [edit(10, 319), edit(10, 320)];
  assert.match(targetIdError(318, ids, drafts), /游戏记录占用/);
  assert.match(targetIdError(319, ids, drafts), /其他草稿占用/);
  assert.equal(targetIdError(319, ids, drafts, 319), '');
  assert.match(targetIdError(320, ids, drafts, 319), /其他草稿占用/);
  for (const value of [0, -1, 32768, 1.5, NaN, Infinity]) assert.match(targetIdError(value, ids, []), /整数/);
  assert.equal(targetIdError(32767, ids, []), '');
});

const template = (guard = 0) => ({applicable: true,
  resistances: [{key:'fire', label:'抗火', value:6}],
  hidden_attributes: [{key:'conversion',value:15}, {key:'guard',value:guard,unit:'%'},
    {key:'armor_break',value:0}, {key:'hp_recovery',value:2}, {key:'mp_recovery',value:0}]});
const hiddenValues = properties => Object.fromEntries(properties.hidden_attributes.map(field => [field.key, field.value]));

test('guard follows current evasion only when baseline guard is zero and baseline evasion is nonzero', () => {
  for (const evasion of [5, 0, -3]) {
    const result = displayProperties(template(), {evasion:2}, {evasion});
    assert.equal(hiddenValues(result).guard, evasion);
    assert.equal(hiddenValues(result).evasion, evasion);
    assert.equal(result.hidden_attributes.find(field => field.key === 'guard').display_source, 'evasion');
  }
});

test('original nonzero guard takes priority including signed values', () => {
  for (const guard of [3, -7]) {
    const result = displayProperties(template(guard), {evasion:2}, {evasion:5});
    assert.equal(hiddenValues(result).guard, guard);
    assert.equal(result.hidden_attributes.find(field => field.key === 'guard').display_source, undefined);
  }
});

test('baseline zero evasion never enables guard linkage after form edits', () => {
  const values = hiddenValues(displayProperties(template(), {evasion:0}, {evasion:5}));
  assert.equal(values.evasion, 5);
  assert.equal(values.guard, 0);
});

test('current resistance and recovery values preserve zero and remain independent without mutating inputs', () => {
  const properties = template(); const original = structuredClone(properties);
  const baseline = {evasion:2}; const current = {evasion:5,resist_fire:-128,hp_recovery:0,mp_recovery:11};
  const result = displayProperties(properties, baseline, current);
  assert.equal(result.resistances[0].value,-128);
  assert.equal(hiddenValues(result).hp_recovery,0);
  assert.equal(hiddenValues(result).mp_recovery,11);
  assert.equal(hiddenValues(result).conversion,15);
  assert.deepEqual(properties,original);
  assert.deepEqual(baseline,{evasion:2});
  assert.equal(Object.hasOwn(current,'guard'),false);
});

test('legacy partial drafts and temporarily invalid input fall back to baseline values', () => {
  const result = displayProperties(template(), {evasion:2}, {evasion:NaN,mp_recovery:undefined});
  assert.equal(hiddenValues(result).guard,2);
  assert.equal(hiddenValues(result).evasion,2);
  assert.equal(hiddenValues(result).hp_recovery,2);
  assert.equal(hiddenValues(result).mp_recovery,0);
  assert.equal(result.resistances[0].value,6);
});

test('non-item attributes never become real zero values or linked guard', () => {
  const properties = template(); properties.applicable = false;
  properties.hidden_attributes.forEach(field => { field.value = null; });
  properties.resistances.forEach(field => { field.value = null; });
  const result = displayProperties(properties, {evasion:2}, {evasion:5,resist_fire:3,hp_recovery:2});
  assert.ok(result.hidden_attributes.every(field => field.value === null));
  assert.equal(result.resistances[0].value,null);
});
