import test from 'node:test';
import assert from 'node:assert/strict';
import {ModelHistory,topology,evaluate,identity,removeBone} from '../editor/model-operations.js';

const fixture = () => ({format:'xyj-model-document/v1',source_frame:1,source_sha256:'0'.repeat(64),
  vertices:[[0,0,0],[1,0,0],[0,1,0]].map((position,i)=>({id:`v${i}`,position,uv:position.slice(0,2),origin:i,group:0,color:0,aux:0xffffffff,weights:[{bone:'b0',value:1}]})),
  faces:[{id:'f0',vertices:['v0','v1','v2'],material:0,group:0,flags:0}],
  bones:[{id:'b0',name:'主体',parent:null,source_group:0,pivot:[0,0,0],translation:[0,0,0],rotation:[0,0,0],scale:[1,1,1],keys:{}}],materials:[{id:'m0',texture:1,tint:[1,1,1],image:null}]});
const select=(vertices=[],faces=[],edges=[])=>({vertices:new Set(vertices),faces:new Set(faces),edges:new Set(edges)});

test('failed topology edits preserve undo and redo state',()=>{
  const h=new ModelHistory(fixture());assert.throws(()=>h.execute('bad',d=>d.faces[0].vertices[0]='missing'));
  assert.equal(h.value.faces[0].vertices[0],'v0');assert.equal(h.undoStack.length,0);
  h.execute('move',d=>d.vertices[0].position[0]=2);h.undo();assert.equal(h.value.vertices[0].position[0],0);h.redo();assert.equal(h.value.vertices[0].position[0],2);
});
test('face subdivision interpolates UV, weights and original morph references',()=>{
  const h=new ModelHistory(fixture());h.execute('split',d=>topology(d,'split',select([],['f0'])));
  assert.equal(h.value.faces.length,3);assert.equal(h.value.vertices.length,4);
  const v=h.value.vertices.at(-1);assert.deepEqual(v.uv,[1/3,1/3]);assert.equal(v.morph_sources.length,3);
  assert.equal(v.weights[0].value,1);h.undo();assert.equal(h.value.faces.length,1);
});
test('edge split, extrusion and vertex merging leave no dangling references',()=>{
  const h=new ModelHistory(fixture());h.execute('edge',d=>topology(d,'split',select([],[],['v0|v1'])));
  assert.equal(h.value.faces.length,2);h.undo();h.execute('extrude',d=>topology(d,'extrude',select([],['f0']),{distance:2}));
  assert.equal(h.value.vertices.length,6);assert.equal(h.value.faces.length,7);
  const ids=new Set(h.value.vertices.map(v=>v.id));assert.ok(h.value.faces.every(f=>f.vertices.every(id=>ids.has(id))));
});
test('native motion and authored bone transforms agree with the compiler convention',()=>{
  const doc=fixture(),first=identity(),second=identity();second[3]=2;
  const model={poses:[[first],[second]],source_groups:[[0,3,0,3,1,0,1]],source_vertices:[[0,0,0],[1,0,0],[0,1,0]],morphs:[{offsets:[4294967295],vertices:[]},{offsets:[4294967295],vertices:[]}]};
  doc.bones[0].translation=[3,0,0];assert.deepEqual(evaluate(model,doc,1).positions.get('v1'),[6,0,0]);
  doc.bones[0].keys['1']={translation:[4,0,0],rotation:[0,0,0],scale:[1,1,1]};assert.deepEqual(evaluate(model,doc,1).positions.get('v1'),[7,0,0]);
});
test('cyclic rigs and duplicate invalid weights are rejected atomically',()=>{
  const h=new ModelHistory(fixture());assert.throws(()=>h.execute('cycle',d=>d.bones[0].parent='b0'));
  assert.throws(()=>h.execute('weights',d=>d.vertices[0].weights=[{bone:'b0',value:.5}]));assert.equal(h.dirty,false);
});
test('deleting a bone redirects children and combines vertex weights',()=>{
  const doc=fixture();doc.bones.push({...structuredClone(doc.bones[0]),id:'b1',parent:'b0'});
  doc.vertices[0].weights=[{bone:'b0',value:.4},{bone:'b1',value:.6}];removeBone(doc,'b1');assert.deepEqual(doc.vertices[0].weights,[{bone:'b0',value:1}]);
});
test('a new bone driven by its parent inherits native animation',()=>{
  const doc=fixture(),pose=identity();pose[3]=3;
  doc.bones.push({...structuredClone(doc.bones[0]),id:'child',parent:'b0',source_group:-1});doc.vertices[0].weights=[{bone:'child',value:1}];
  const model={poses:[[identity()],[pose]],source_groups:[[0,3,0,3,1,0,1]],source_vertices:[[0,0,0],[1,0,0],[0,1,0]],morphs:[{offsets:[4294967295],vertices:[]},{offsets:[4294967295],vertices:[]}]};
  assert.deepEqual(evaluate(model,doc,1).positions.get('v0'),[3,0,0]);
});
