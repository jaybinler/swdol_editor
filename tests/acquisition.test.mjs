import test from 'node:test';
import assert from 'node:assert/strict';
import {emptyAcquisition, nearestChance, acquisitionSummary} from '../editor/acquisition.js';

const choices = [{threshold: -1, percent: 0}, {threshold: 0, percent: 1.0009765625},
  {threshold: 9, percent: 10.009765625}, {threshold: 99, percent: 100}];

test('blank cloned acquisition state owns independent collections', () => {
  const one = emptyAcquisition(), two = emptyAcquisition();
  one.shops.push({node: 'test'});
  assert.equal(two.shops.length, 0); assert.equal(two.drop, null);
});

test('native rates retain zero and hundred and show representable probabilities', () => {
  assert.equal(nearestChance(0, choices).percent, 0);
  assert.equal(nearestChance(100, choices).percent, 100);
  assert.equal(nearestChance('10', choices).percent, 10.009765625);
});

test('invalid probabilities and unavailable data never silently become zero percent', () => {
  for (const value of ['', NaN, Infinity, -1, 101, 'invalid']) assert.equal(nearestChance(value, choices), null);
  assert.equal(nearestChance(10, []), null);
});

test('draft summary distinguishes all acquisition routes and keeps zero probability', () => {
  const lines = acquisitionSummary({drop: {monster_id: 20003, map_id: 18, percent: 0},
    shops: [{node: 'TALK0010.EXE:2014:3904'}], rewards: [{node: 'TALK0001.EXE:216:4488', count: 2}]});
  assert.equal(lines.length, 3);
  assert.match(lines[0], /0% · 1 件/);
  assert.match(lines[1], /商店/); assert.match(lines[2], /2 件/);
  assert.deepEqual(acquisitionSummary(undefined), []);
});
