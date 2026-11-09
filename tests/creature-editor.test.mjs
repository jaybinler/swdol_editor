import test from 'node:test';
import assert from 'node:assert/strict';
import {CreatureEditor, creatureOperation, nearestProbability} from '../editor/creature-editor.js';

test('NPC operation extraction preserves duplicate slots without leaking read-only properties', () => {
  const input = {source_id:1,target_id:2,name:'绿女',description:'',level:52,sex:0,respawn_seconds:5760,
    appearance_id:1,scale:[1,1,1],attributes:{constitution:10},drops:[{id:10,threshold:99,name:'剑'}],
    skills:[3,3,-1],reset_candidates:[-1,4,4,-1,-1],spawns:[],points:[{key:'private'}],editable:true};
  const result = creatureOperation(input);
  assert.equal(result.points,undefined);assert.deepEqual(result.skills,[3,3,-1]);
  assert.deepEqual(result.drops,[{id:10,threshold:99}]);result.skills[0]=0;assert.equal(input.skills[0],3);
});

test('native quantization respects exact zero, exact full and nonfinite boundaries', () => {
  const table=[{threshold:-1,percent:0},{threshold:4,percent:5.0048828125},{threshold:99,percent:100}];
  assert.equal(nearestProbability(0,table).threshold,-1);
  assert.equal(nearestProbability(5,table).threshold,4);
  assert.equal(nearestProbability(100,table).threshold,99);
  for(const value of [NaN,Infinity,-1,101])assert.throws(()=>nearestProbability(value,table));
});

async function withEditor(run) {
  const previous = globalThis.document, nodes = new Map();
  globalThis.document = {getElementById: id => {
    if (!nodes.has(id)) nodes.set(id, {disabled:false,textContent:'',focus(){this.focused=true;}});
    return nodes.get(id);
  }};
  const editor = Object.assign(Object.create(CreatureEditor.prototype), {
    dirty:false,ticket:0,previewTicket:0,busy:false,meta:{native_verified:true},
    choices(){},fill(value){this.value=value;},renderDrafts(){},error(message){this.message=message;}
  });
  try { await run(editor,nodes); }
  finally { globalThis.document=previous; }
}

const detail = id => ({source_id:id,target_id:id,name:'绿女',description:'',level:52,sex:0,
  respawn_seconds:5760,appearance_id:id,scale:[1,1,1],attributes:{},drops:[],skills:[],
  reset_candidates:[],spawns:[],editable:true,points:[]});

test('late NPC detail responses cannot overwrite a newer selection or reopen a stale draft', async () => {
  await withEditor(async editor => {
    let first,second;
    editor.api=path=>new Promise(resolve=>{if(path.endsWith('/20087'))first=resolve;else second=resolve;});
    const old=editor.select(20087),current=editor.select(20071);
    second(detail(20071));assert.equal(await current,true);
    first(detail(20087));assert.equal(await old,false);
    assert.equal(editor.detail.source_id,20071);assert.equal(editor.loading,false);
  });
});

test('failed NPC loading keeps authoring disabled and reports a recoverable error', async () => {
  await withEditor(async (editor,nodes) => {
    editor.api=async()=>{throw new Error('快照不可用');};
    assert.equal(await editor.select(20087),false);
    assert.equal(nodes.get('npc-fields').disabled,true);
    assert.equal(nodes.get('npc-edit').disabled,true);
    assert.equal(editor.loading,false);assert.match(editor.message,/20087.*快照不可用/);
  });
});

test('returning to view honors keep-input and actually discards after explicit confirmation', async () => {
  await withEditor(async editor => {
    editor.dirty=true;editor.detail=detail(20087);editor.mode='edit';editor.draftTarget=320;
    let calls=0;editor.api=async()=>{calls++;return detail(20087);};
    editor.confirmDiscard=async()=>false;await editor.view();
    assert.equal(editor.dirty,true);assert.equal(editor.draftTarget,320);assert.equal(calls,0);
    editor.confirmDiscard=async()=>true;await editor.view();
    assert.equal(editor.dirty,false);assert.equal(editor.mode,'view');assert.equal(calls,1);
    assert.equal(editor.draftTarget,null);
  });
});

test('map inputs must be joined before saving a monster draft', async () => {
  await withEditor(async (editor,nodes) => {
    editor.mode='edit';editor.pointDirty=true;let calls=0;editor.api=async()=>{calls++;};
    await editor.stage();assert.equal(calls,0);assert.match(editor.message,/尚未加入/);
    assert.equal(nodes.get('npc-add-point').focused,true);
  });
});

test('cancelling publication keeps the draft and prevents writes and repeated confirmation dialogs', async () => {
  await withEditor(async editor => {
    editor.meta={export_mode:'automatic',revision:2,edits:[{target_id:320,spawns:[]}]};
    editor.canPublish=()=>true;let resolve,confirmations=0,calls=0;
    editor.confirmPublish=()=>{confirmations++;return new Promise(done=>{resolve=done;});};
    editor.api=async()=>{calls++;};
    const first=editor.export();await editor.export();assert.equal(confirmations,1);assert.equal(editor.busy,true);
    resolve(false);await first;assert.equal(calls,0);assert.equal(editor.busy,false);assert.equal(editor.meta.edits.length,1);
  });
});

test('automatic publication sends stopped confirmation and reloads only after committed success', async () => {
  await withEditor(async editor => {
    editor.meta={export_mode:'automatic',revision:2,edits:[{target_id:320,spawns:[]}]};
    editor.canPublish=()=>true;editor.confirmPublish=async()=>true;editor.status=()=>{};
    let applied=null;editor.applied=id=>{applied=id;};
    editor.api=async(path,body)=>{assert.equal(path,'/api/creature-export');assert.equal(body.revision,2);assert.equal(body.server_stopped,true);assert.match(body.request_id,/^[0-9a-f-]{36}$/);
      return {status:'applied',operations:1,point_changes:1,backup_path:'latest'};};
    await editor.export();assert.equal(applied,320);assert.equal(editor.dirty,false);assert.equal(editor.meta.edits.length,0);
    editor.meta.edits=[{target_id:320,spawns:[]}];
    editor.api=async()=>{throw new Error('数据库失败，已恢复');};applied=null;
    await editor.export();assert.equal(applied,null);assert.match(editor.message,/已恢复/);assert.equal(editor.meta.edits.length,1);
  });
});

test('a lost publication response is resolved from its receipt without sending another write', async () => {
  await withEditor(async editor => {
    editor.meta={export_mode:'automatic',revision:2,edits:[{target_id:320,spawns:[]}]};
    editor.canPublish=()=>true;editor.confirmPublish=async()=>true;editor.status=()=>{};let writes=0,applied=null;
    editor.applied=id=>{applied=id;};
    editor.api=async(path)=>{
      if(path==='/api/creature-export'){writes++;throw new Error('response lost');}
      assert.match(path,/^\/api\/publication-result\?request_id=/);
      return {status:'applied',operations:1,point_changes:1,backup_path:'latest'};
    };
    await editor.export();assert.equal(writes,1);assert.equal(applied,320);assert.equal(editor.meta.edits.length,0);
  });
});
