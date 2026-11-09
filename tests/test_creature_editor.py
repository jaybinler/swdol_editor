"""NPC authoring integrity, native actor retention, guarded database writes and recovery."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from editor_server import EditorState, make_handler
from xyj_content.binary import ContentError, decode_table
from xyj_content.catalog import Inputs, load_inputs
from xyj_content.settings import default_config
from xyj_content.creature_editor import detail, validate, prepare, respawn, export_patch, verify_patch
from xyj_content.creature_assets import build_variants, scaled_actor, ACT_PATH
from xyj_content.models import actor_data, read_action
from xyj_content.objects import find_unique
from xyj_content.files import read_json, write_json, sha256
from xyj_content.patch import PAYLOAD_PATHS

SNAPSHOT = ROOT / 'output/editor/database-snapshots/985890871774effb-a45cc8c967781d68'


class CreatureEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = EditorState(ROOT, config={**default_config(), 'npc_snapshot': str(SNAPSHOT)})
        cls.loaded = cls.state.loaded
        cls.catalog = cls.state.creatures()
        occupied = {struct.unpack_from('<I', r, 4)[0] for t in (cls.loaded.server,cls.loaded.client) for r in t.records}
        cls.free = [i for i in range(1,32768) if i not in occupied][:3]

    def edit(self, tid=20087, clone=False):
        value = detail(self.loaded,tid,self.catalog)
        keys = ('source_id','target_id','name','description','level','sex','respawn_seconds','appearance_id',
                'scale','attributes','drops','skills','reset_candidates','spawns')
        value = {k: value[k] for k in keys}
        value['drops'] = [{'id':d['id'],'threshold':d['threshold']} for d in value['drops']]
        if clone:
            value['target_id'] = self.free[0]; value['name'] = '测试绿女'; value['reset_candidates'] = [-1]*5
        return value

    def test_native_timing_is_ticks_and_range_includes_corpse_separately(self):
        green,pangu = respawn(5760),respawn(72000)
        self.assertEqual((green['ticks'],green['minimum_seconds'],green['maximum_seconds']), (57600,4608,6854.4))
        self.assertEqual((pangu['minimum_seconds'],pangu['maximum_seconds'],pangu['corpse_seconds']), (57600,85680,60))
        for value in [True,0,-1,float('nan'),float('inf'),'.71',1800000.1]:
            with self.subTest(value=value),self.assertRaises(ContentError):respawn(value)

    def test_npc_sex_and_level_are_distinct_from_item_restriction_codes(self):
        self.assertEqual((detail(self.loaded,20095)['sex'],detail(self.loaded,20096)['sex']),(1,0))
        self.assertEqual(detail(self.loaded,20087)['level'],52)

    def test_template_edits_change_only_allowed_fields_and_keep_server_only_arrays(self):
        edit = self.edit();edit.update(name='新绿女',level=53,sex=1,respawn_seconds=100)
        edit['skills'][0]=11058;edit['drops'][0]={'id':10,'threshold':99}
        blobs,_=prepare(self.loaded,[edit],catalog=self.catalog)
        for key,table in [('server_object',self.loaded.server),('client_object',self.loaded.client)]:
            modified=decode_table(blobs[key]);before=find_unique(table,20087)[1];after=find_unique(modified,20087)[1]
            for original,new in zip(table.records,modified.records):
                if struct.unpack_from('<I',original,4)[0]!=20087:self.assertEqual(original,new)
            self.assertEqual(struct.unpack_from('<h',after,74)[0],53)
            if key=='client_object':
                self.assertEqual(before[254:302],after[254:302]);self.assertEqual(before[332:336],after[332:336])
                self.assertEqual(before[382:414],after[382:414]);self.assertEqual(before[516:526],after[516:526])
            allowed=set(range(12,28))|{74,75,80}|set(range(86,102))
            if key=='server_object':allowed|=set(range(254,302))|set(range(332,336))|set(range(382,414))|set(range(516,526))
            self.assertTrue(all(a==b or i in allowed for i,(a,b) in enumerate(zip(before,after))))

    def test_new_variant_does_not_change_original_template_or_its_reset_weights(self):
        edit=self.edit(20071,clone=True);edit['respawn_seconds']=60
        blobs,_=prepare(self.loaded,[edit],catalog=self.catalog)
        for key,table in [('server_object',self.loaded.server),('client_object',self.loaded.client)]:
            modified=decode_table(blobs[key]);self.assertEqual(find_unique(modified,20071)[1],find_unique(table,20071)[1])
        new=find_unique(decode_table(blobs['server_object']),edit['target_id'])[1]
        self.assertEqual(struct.unpack_from('<5h',new,516),(-1,)*5)

    def test_two_visible_map_variants_keep_independent_native_intervals(self):
        one=self.edit(clone=True);two=deepcopy(one);two['target_id']=self.free[1]
        one['respawn_seconds']=60;two['respawn_seconds']=600
        one['spawns']=[{'action':'add','key':'npcpool:5530','map_id':18,'position':[1,2,3],'count':1,'direction':0}]
        two['spawns']=[{'action':'add','key':'npcpool:5530','map_id':9,'position':[4,5,6],'count':2,'direction':0}]
        blobs,_=prepare(self.loaded,[one,two],catalog=self.catalog)
        table=decode_table(blobs['server_object'])
        self.assertEqual(struct.unpack_from('<i',find_unique(table,one['target_id'])[1],332)[0],600)
        self.assertEqual(struct.unpack_from('<i',find_unique(table,two['target_id'])[1],332)[0],6000)
        self.assertEqual(find_unique(table,20087)[1],find_unique(self.loaded.server,20087)[1])

    def test_copy_plan_precedes_same_package_seed_move_and_freezes_source_scripts(self):
        from xyj_content.creature_database import point_plan
        edit=self.edit();edit['spawns']=[{'action':'update','key':'npcpool:5530','map_id':9,'position':[1,2,3],'count':1,'direction':0}]
        clone=self.edit(clone=True);clone['spawns']=[{'action':'add','key':'npcpool:5530','map_id':18,'position':[4,5,6],'count':1,'direction':0}]
        plan=point_plan([edit,clone],self.catalog)
        self.assertEqual([p['point']['action'] for p in plan],['add','update'])
        self.assertEqual(plan[0]['point']['expected']['plots'],self.catalog.roles['npcpool:5530']['plots'])

    def test_http_draft_versions_token_guards_and_workspace_isolation(self):
        from xyj_content.editor import item_detail
        with tempfile.TemporaryDirectory(dir=ROOT/'output/editor/verification-0.12') as temporary:
            root=Path(temporary)
            config={**default_config(),'server_object':str(self.loaded.paths.server),
                    'client_root':str(ROOT/'swdol'),'client_object':str(self.loaded.paths.client),
                    'language':str(self.loaded.paths.language),'npc_snapshot':str(SNAPSHOT)}
            state=EditorState(root,config=config)
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(state));worker=Thread(target=server.serve_forever,daemon=True);worker.start()
            origin=f'http://127.0.0.1:{server.server_port}'
            def request(path,body=None,token=None):
                headers={'Origin':origin,'Content-Type':'application/json','X-Editor-Token':state.token if token is None else token}
                req=Request(origin+path,data=None if body is None else json.dumps(body).encode(),headers=headers)
                with urlopen(req,timeout=20) as response:return json.load(response)
            try:
                data=request('/api/creature-editor');self.assertTrue(data['native_verified'])
                self.assertEqual(data['revision'],0)
                seeds=request('/api/creature-seeds?map=33&source=20087')['points']
                self.assertTrue(seeds);self.assertEqual(seeds[0]['template_id'],20087)
                edit=self.edit(clone=True)
                with self.assertRaises(HTTPError) as bad:request('/api/creature-draft',{'revision':0,'edits':[edit]},'bad')
                self.assertEqual(bad.exception.code,403)
                saved=request('/api/creature-draft',{'revision':0,'edits':[edit]});self.assertEqual(saved['revision'],1)
                before=state.creature_draft_path.read_bytes()
                with self.assertRaises(HTTPError) as stale:request('/api/creature-draft',{'revision':0,'edits':[]})
                self.assertEqual(stale.exception.code,400);self.assertEqual(state.creature_draft_path.read_bytes(),before)
                item=item_detail(self.loaded,10)
                colliding={'source_id':10,'target_id':edit['target_id'],'name':item['name'],'description':item['description'],'stats':{}}
                with self.assertRaises(ContentError):state.save({'revision':0,'edits':[colliding]})
                fresh=EditorState(root,config=config);fresh.load_creature_draft()
                self.assertEqual(fresh.creature_edits,saved['edits'])
                self.assertFalse((ROOT/'output/editor/creatures.json').exists())
            finally:
                server.shutdown();server.server_close();worker.join(timeout=3)

    def test_id_text_reference_capacity_and_nonfinite_values_are_rejected(self):
        cases=[]
        for key,value in [('target_id',10),('name','字'*7),('level',101),('sex',2),('scale',[1,float('nan'),1]),('appearance_id',10)]:
            e=self.edit(clone=True);e[key]=value;cases.append(e)
        e=self.edit();e['skills'][0]=10;cases.append(e)
        e=self.edit();e['drops']=[{'id':10,'threshold':99}]*9+[{'id':0,'threshold':-1}]*3;cases.append(e)
        e=self.edit();e['reset_candidates'][0]=10;cases.append(e)
        for e in cases:
            with self.subTest(edit=e),self.assertRaises(ContentError):validate(self.loaded,[e],self.catalog)
        with self.assertRaises(ContentError):validate(self.loaded,[self.edit(clone=True)],self.catalog,[self.free[0]])

    def test_all_raw_duplicate_skill_and_candidate_slots_survive_save(self):
        edit=self.edit(20071);edit['name']='盘古新名'
        normalized=validate(self.loaded,[edit],self.catalog)[0]
        self.assertEqual(normalized['skills'],edit['skills']);self.assertEqual(normalized['reset_candidates'],edit['reset_candidates'])

    def test_point_changes_require_saved_ownership_and_bounded_positions(self):
        edit=self.edit(clone=True)
        p={'action':'add','key':'npcpool:5530','map_id':18,'position':[1,2,3],'count':3,'direction':90}
        edit['spawns']=[p];self.assertEqual(validate(self.loaded,[edit],self.catalog)[0]['spawns'],[p])
        for change in [{'key':'npcpool:9999999'},{'count':51},{'position':[1,float('inf'),3]},{'map_id':32767},{'action':'update'}]:
            e=deepcopy(edit);e['spawns'][0].update(change)
            with self.subTest(change=change),self.assertRaises(ContentError):validate(self.loaded,[e],self.catalog)

    def test_scaled_actor_preserves_all_original_data_and_every_selector(self):
        original=actor_data(ROOT/'swdol',157);new=scaled_actor(original,[1.2,.9,1])
        for factors in ([1], [1,1,1,1], [True,1,1], [.1,1,1], [1,float('nan'),1]):
            with self.subTest(factors=factors),self.assertRaises(ContentError):scaled_actor(original,factors)
        count=struct.unpack_from('<H',original)[0]
        self.assertEqual(new[2+count*6:len(original)],original[2+count*6:])
        self.assertEqual(struct.unpack_from('<H',new)[0],count)
        for i in range(count):
            start,length=struct.unpack_from('<IH',new,2+i*6)
            old_start,old_length=struct.unpack_from('<IH',original,2+i*6)
            if old_length:self.assertGreaterEqual(start,len(original));self.assertGreaterEqual(length,old_length)
        with self.assertRaises(ContentError):scaled_actor(b'\x01\x00'+struct.pack('<IH',8,2)+b'??',[1,1,1])

    def test_resource_slot_allocation_excludes_all_existing_object_actor_references(self):
        edit=self.edit(clone=True);edit['scale']=[1.2,1,1]
        resources,actors=build_variants(ROOT/'swdol',self.loaded,[edit])
        before=resources[ACT_PATH]['source'];after=resources[ACT_PATH]['data'];actor=actors[edit['target_id']]
        self.assertEqual(struct.unpack_from('<I',before,10+(actor-1)*12)[0],0)
        changed=set(range(10+(actor-1)*12,10+actor*12))
        self.assertTrue(all(a==b or i in changed for i,(a,b) in enumerate(zip(before,after))))

    def test_recovery_rejects_invalid_keys_and_columns_before_any_row_query(self):
        from xyj_content.creature_database import rollback_points
        with tempfile.TemporaryDirectory(dir=ROOT/'output/editor/verification-0.12') as temporary:
            path=Path(temporary)/'journal.json'
            entry={'table':'npcpool','role':1,'before':None,'after':{'RoleID':'31'}}
            for changes in ({'role':'1; DROP TABLE npcpool'}, {'after':{'RoleID':'31','bad`=1;--':'31'}}):
                write_json(path,{'format':'xyj-npc-point-journal/v1','entries':[{**entry,**changes}]})
                with self.subTest(changes=changes),patch('xyj_content.creature_database.schema',return_value={'npcpool':{'RoleID':'int(11)'}}):
                    with self.assertRaises(ContentError):rollback_points(lambda sql:self.fail('Unexpected row query: '+sql),path)

    def test_patch_rebuild_rejects_extra_table_actor_and_point_mutations(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'output/editor/verification-0.12') as temporary:
            root=Path(temporary);edit=self.edit(clone=True);edit['scale']=[1.1,1,1]
            edit['spawns']=[{'action':'add','key':'npcpool:5530','map_id':18,'position':[47527,1996,63590],'count':2,'direction':0}]
            result=export_patch(root,self.loaded,[edit],catalog=self.catalog,npc_directory=SNAPSHOT,client=ROOT/'swdol')
            package=Path(result['path']);self.assertEqual(verify_patch(package)['point_changes'],1)
            path=package/'npc-points.json';original=path.read_bytes();data=read_json(path);data['operations'][0]['point']['count']=3;write_json(path,data)
            with self.assertRaises(ContentError):verify_patch(package)
            path.write_bytes(original)
            path=package/ACT_PATH;original=path.read_bytes();path.write_bytes(original[:-1]+bytes([original[-1]^1]))
            with self.assertRaises(ContentError):verify_patch(package)


@unittest.skipUnless(os.environ.get('XYJ_TEST_MYSQL')=='1','Explicit isolated MySQL integration run only')
class CreatureDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(CreatureEditorTests.setUpClass.__func__)
    edit = CreatureEditorTests.edit

    def test_paired_publication_and_file_failure_restore_both_file_and_database_state(self):
        from creature_mysql_fixture import isolated_mysql
        from xyj_content import creature_deployment as deploy
        with tempfile.TemporaryDirectory(dir=ROOT/'output/editor/verification-0.12') as temporary:
            root=Path(temporary);edit=self.edit(clone=True);edit['scale']=[1.1,1,1]
            edit['spawns']=[{'action':'add','key':'npcpool:5530','map_id':18,'position':[47527,1996,63590],'count':1,'direction':0}]
            package=Path(export_patch(root,self.loaded,[edit],catalog=self.catalog,npc_directory=SNAPSHOT,client=ROOT/'swdol')['path'])
            client=root/'game/client';server=root/'game/server/Object.dbf'
            mapping={PAYLOAD_PATHS['server_object']:server,PAYLOAD_PATHS['client_object']:client/'Main/Object.dbf',
                     PAYLOAD_PATHS['client_language']:client/'Main/Language.DBF',ACT_PATH:client/'Act/all_char.act'}
            for relative,target in mapping.items():
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(package/'baseline'/relative,target)
            shutil.copyfile(ROOT/'XYJ_SEVVER/qingfeng/NewLocalServer.exe',server.parent/'NewLocalServer.exe')
            config={'client_root':str(client),'server_target':str(server)}
            before={str(p):p.read_bytes() for p in mapping.values()}
            with isolated_mysql(ROOT,root/'mysql') as query:
                count=query('SELECT COUNT(*) FROM npcpool;')
                result=deploy.apply(root,config,query,package)
                self.assertEqual(result['status'],'committed_runtime_pending')
                self.assertEqual(int(query('SELECT COUNT(*) FROM npcpool;')),int(count)+1)
                deploy.recover(root,config,query,Path(result['journal']))
                self.assertEqual({str(p):p.read_bytes() for p in mapping.values()},before)
                self.assertEqual(query('SELECT COUNT(*) FROM npcpool;'),count)
                original=deploy.atomic_write;failed=False
                def fail_once(path,data):
                    nonlocal failed
                    if path==client/'Main/Language.DBF' and not failed:
                        failed=True;raise OSError('injected file replacement failure')
                    return original(path,data)
                with patch.object(deploy,'atomic_write',side_effect=fail_once),self.assertRaises(ContentError):
                    deploy.apply(root,config,query,package)
                self.assertTrue(failed)
                self.assertEqual({str(p):p.read_bytes() for p in mapping.values()},before)
                self.assertEqual(query('SELECT COUNT(*) FROM npcpool;'),count)
    def test_real_legacy_insert_update_remove_rollback_and_drift_guard(self):
        from creature_mysql_fixture import isolated_mysql
        from xyj_content.creature_database import point_plan,apply_points,rollback_points
        with tempfile.TemporaryDirectory(dir=ROOT/'output/editor/verification-0.12') as temporary:
            root=Path(temporary)
            with isolated_mysql(ROOT,root/'mysql') as query:
                count=query('SELECT COUNT(*) FROM npcpool;')
                edit=self.edit(clone=True)
                edit['spawns']=[{'action':'add','key':'npcpool:5530','map_id':9,'position':[123.125,1996,63590],'count':2,'direction':25.5}]
                plan=point_plan([edit],self.catalog);journal=root/'journal.json'
                result=apply_points(query,plan,journal);self.assertEqual(result['rows'],2)
                self.assertEqual(int(query('SELECT COUNT(*) FROM npcpool;')),int(count)+2)
                rollback_points(query,journal);self.assertEqual(query('SELECT COUNT(*) FROM npcpool;'),count)
                edit=self.edit();edit['spawns']=[{'action':'update','key':'npcpool:5530','map_id':9,'position':[1,2,3],'count':1,'direction':90}]
                plan=point_plan([edit],self.catalog);journal=root/'move.json';apply_points(query,plan,journal)
                self.assertEqual(query('SELECT SectionID FROM npcpool WHERE RoleID=5530;').strip(),'9')
                rollback_points(query,journal)
                self.assertEqual(query('SELECT SectionID FROM npcpool WHERE RoleID=5530;').strip(),'18')
                edit['spawns'][0]['action']='remove';journal=root/'remove.json';apply_points(query,point_plan([edit],self.catalog),journal)
                self.assertEqual(query('SELECT Removed FROM npcpool WHERE RoleID=5530;').strip(),'1');rollback_points(query,journal)
                query('UPDATE npcpool SET ReviveX=42 WHERE RoleID=5530;')
                with self.assertRaises(ContentError):apply_points(query,point_plan([edit],self.catalog),root/'drift.json')
