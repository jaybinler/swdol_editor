"""Automatic NPC publication and global retention against isolated files and optional copied MySQL tables."""

from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import uuid

PROJECT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(PROJECT/'tools'))
from editor_server import EditorState,make_handler
from xyj_content import creature_auto as auto,deployment,publication
from xyj_content.binary import ContentError
from xyj_content.files import write_json,read_json,sha256
from xyj_content.settings import SettingsController,default_config
from xyj_content.patch import PAYLOAD_PATHS
from xyj_content.creature_editor import detail,export_patch
from xyj_content.editor import item_detail
from xyj_content.verification_inputs import paired_fixture

SNAPSHOT=PROJECT/'output/editor/database-snapshots/985890871774effb-a45cc8c967781d68'
VERIFY=PROJECT/'output/editor/verification-0.13'


class AutomaticCreatureTests(unittest.TestCase):
    def setUp(self):
        VERIFY.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=VERIFY);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        baseline=paired_fixture(PROJECT)/'baseline'
        for relative in PAYLOAD_PATHS.values():
            p=self.root/relative;p.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(baseline/relative,p)
        server=self.root/PAYLOAD_PATHS['server_object'];client=self.root/'client'
        shutil.copyfile(PROJECT/'XYJ_SEVVER/qingfeng/NewLocalServer.exe',server.parent/'NewLocalServer.exe')
        config={**default_config(),'client_root':str(client),'server_object':str(server),'server_target':str(server),
                'npc_snapshot':str(SNAPSHOT),'map_dir':str(PROJECT/'XYJ_SEVVER/qingfeng'),
                'act_dir':str(PROJECT/'swdol/Act'),'model_dir':str(PROJECT/'swdol/MODEL'),
                'tex_dir':str(PROJECT/'swdol/TEX'),'tsw_dir':str(PROJECT/'swdol/Tsw')}
        self.settings=self.root/'output/editor/settings.json'
        write_json(self.settings,{'format':'xyj-settings/v1','config':config,'validated_files':[]})
        self.factory=lambda cfg:EditorState(self.root,config=cfg)
        self.c=SettingsController(self.root,self.factory)
        self.assertIsNotNone(self.c.workspace,self.c.setup_error)
        occupied={struct.unpack_from('<I',r,4)[0] for t in (self.c.workspace.loaded.server,self.c.workspace.loaded.client) for r in t.records}
        self.free=next(i for i in range(1,32768) if i not in occupied)
        self.before={p:p.read_bytes() for p in [self.settings,*(self.root/r for r in PAYLOAD_PATHS.values())]}

    def edit(self,tid=20087,clone=False):
        value=detail(self.c.workspace.loaded,tid,self.c.workspace.creatures())
        keys=('source_id','target_id','name','description','level','sex','respawn_seconds','appearance_id',
              'scale','attributes','drops','skills','reset_candidates','spawns')
        result={k:value[k] for k in keys};result['drops']=[{k:d[k] for k in ('id','threshold')} for d in value['drops']]
        if clone:result.update(target_id=self.free,name='自动测试女',reset_candidates=[-1]*5)
        return result

    def stage(self,edit):
        state=self.c.workspace;state.load_creature_draft()
        state.save_creatures({'revision':state.creature_revision,'edits':[edit]})

    def apply(self,**options):
        return auto.apply_workspace(self.c,self.c.workspace.creature_revision,server_stopped=True,**options)

    def test_template_publish_then_item_then_creature_share_exactly_one_backup_and_package(self):
        self.stage(self.edit(clone=True));self.apply()
        self.assertEqual(self.c.workspace.creature_edits,[])
        self.assertEqual(self.c.workspace.creatures().detail(self.free)['name'],'自动测试女')
        state=self.c.workspace;item=item_detail(state.loaded,10)
        state.save({'revision':state.revision,'edits':[{'source_id':10,'target_id':10,'name':item['name'],
                    'description':item['description'],'stats':{'damage':77}}]})
        deployment.apply_workspace(self.c,state.revision)
        item_state={p:p.read_bytes() for p in (self.root/r for r in PAYLOAD_PATHS.values())}
        e=self.edit(self.free);e['level']=53;self.stage(e);self.apply()
        e=self.edit(self.free);e['level']=52;self.stage(e);self.apply()
        e=self.edit(self.free);e['level']=53;self.stage(e);self.apply()
        folder=self.root/'output/editor/backups/latest';m=deployment._validate_journal(self.root,folder)
        for e in m['entries'][:3]:self.assertEqual((folder/e['backup']).read_bytes(),item_state[Path(e['target'])])
        self.assertFalse((folder.parent/'previous').exists())
        packages=[p for parent in ('patches','creature-patches') for p in (self.root/'output/editor'/parent).glob('*') if p.is_dir()]
        self.assertEqual(packages,[self.root/'output/editor/patches/latest'])
        self.assertEqual(len(list((self.root/'output/editor/publications/receipts').glob('*.json'))),5)
        reopened=SettingsController(self.root,self.factory);self.assertIsNotNone(reopened.workspace,reopened.setup_error)
        reopened.workspace.load_creature_draft();self.assertEqual(reopened.workspace.creature_edits,[])

    def test_failed_second_publication_preserves_preceding_success_and_all_user_inputs(self):
        self.stage(self.edit(clone=True));self.apply()
        latest=self.root/'output/editor/backups/latest/manifest.json';before=latest.read_bytes()
        game={p:p.read_bytes() for p in [self.settings,*(self.root/r for r in PAYLOAD_PATHS.values())]}
        e=self.edit(self.free);e['level']=53;self.stage(e)
        original=deployment._replace_file;failed=False
        def fail_once(staged,target):
            nonlocal failed
            if target==self.settings and not failed:failed=True;raise OSError('settings disk full')
            return original(staged,target)
        with patch.object(deployment,'_replace_file',side_effect=fail_once),self.assertRaisesRegex(ContentError,'已恢复'):
            self.apply()
        self.assertEqual(latest.read_bytes(),before)
        self.assertEqual({p:p.read_bytes() for p in game},game)
        self.assertEqual(self.c.workspace.creature_edits,[e])
        self.assertFalse(list((self.root/'output/editor/patches').glob('.next-*')))

    def test_http_requires_stop_confirmation_and_never_accepts_browser_paths_or_stale_generations(self):
        self.stage(self.edit(clone=True))
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.c));worker=Thread(target=server.serve_forever,daemon=True);worker.start()
        origin=f'http://127.0.0.1:{server.server_port}'
        def post(value):
            req=Request(origin+'/api/creature-export',data=json.dumps(value).encode(),headers={
                'Origin':origin,'Content-Type':'application/json','X-Editor-Token':self.c.token,'X-Workspace-Generation':'0'})
            with urlopen(req,timeout=40) as response:return json.load(response)
        try:
            request_id=str(uuid.uuid4())
            body={'revision':self.c.workspace.creature_revision,'server_stopped':False,'request_id':request_id}
            with self.assertRaises(HTTPError):post(body)
            with self.assertRaises(HTTPError):post({**body,'server_stopped':True,'target':'other'})
            self.assertEqual(post({**body,'server_stopped':True})['status'],'applied')
            with urlopen(origin+'/api/publication-result?request_id='+request_id,timeout=8) as response:receipt=json.load(response)
            self.assertEqual(receipt['status'],'applied');self.assertEqual(receipt['operations'],1)
            with self.assertRaises(HTTPError):post({**body,'server_stopped':True})
        finally:server.shutdown();server.server_close();worker.join(timeout=3)

    def test_completed_receipt_rejects_same_package(self):
        self.stage(self.edit(clone=True));self.apply()
        with self.assertRaisesRegex(ContentError,'已发布'):publication.prepare(self.root,self.root/'output/editor/patches/latest')
        with self.assertRaisesRegex(ContentError,'已发布'):publication.assert_unpublished(self.root,self.root/'output/editor/patches/latest')

    def test_stale_process_rejects_changed_settings_or_draft_before_publication(self):
        self.stage(self.edit(clone=True))
        settings=self.settings.read_bytes()
        document=read_json(self.settings);document['other_editor_revision']=1
        write_json(self.settings,document)
        with patch.object(auto,'prepare') as prepare,self.assertRaisesRegex(ContentError,'另一编辑器'):
            self.apply()
        prepare.assert_not_called()
        self.settings.write_bytes(settings)
        path=self.c.workspace.creature_draft_path;draft=read_json(path)
        draft['revision']+=1;write_json(path,draft)
        with patch.object(auto,'prepare') as prepare,self.assertRaisesRegex(ContentError,'草稿已由另一编辑器'):
            self.apply()
        prepare.assert_not_called()
        self.assertEqual(read_json(path),draft)
        self.assertFalse((self.root/'output/editor/backups/latest').exists())

    def test_reviewed_cli_package_is_applied_without_regenerating_its_identity(self):
        e=self.edit(clone=True);self.stage(e);state=self.c.workspace
        exported=export_patch(self.root,state.loaded,state.creature_edits,catalog=state.creatures(),npc_directory=state.npc_directory,client=state.client)
        package=Path(exported['path']);reviewed=read_json(package/'manifest.json')
        self.apply(package=package)
        latest=self.root/'output/editor/patches/latest'
        self.assertEqual(read_json(latest/'manifest.json'),reviewed)
        with self.assertRaisesRegex(ContentError,'已发布'):publication.assert_unpublished(self.root,latest)

    def test_cleanup_rejects_directory_junctions(self):
        protected=self.root/'protected';protected.mkdir();(protected/'keep').write_text('keep')
        link=self.root/'output/editor/patches/linked'
        import _winapi
        link.parent.mkdir(parents=True,exist_ok=True)
        try:_winapi.CreateJunction(str(protected),str(link))
        except PermissionError:self.skipTest('Junction creation requires unsandboxed integration run')
        with self.assertRaises(ContentError):publication.remove_tree(self.root,link,[link.parent])
        self.assertEqual((protected/'keep').read_text(),'keep')
        link.rmdir()

    def test_known_historical_packages_are_cleaned_after_commit_and_legacy_pending_is_blocked(self):
        old=self.root/'output/editor/creature-patches/20260101-010101-12345678'
        shutil.copytree(paired_fixture(PROJECT),old)
        self.stage(self.edit(clone=True));self.apply();self.assertFalse(old.exists())
        pending=self.root/'output/editor/creature-deployments'/('a'*32)
        write_json(pending/'journal.json',{'format':'xyj-creature-deploy/v1','phase':'applying'})
        e=self.edit(self.free);e['level']=53;self.stage(e)
        with self.assertRaisesRegex(ContentError,'未完成'):self.apply()

    def test_scaled_resource_and_editor_state_restore_without_any_package_dependency(self):
        actor=self.root/'client/Act/all_char.act';actor.parent.mkdir(parents=True,exist_ok=True)
        original=(PROJECT/'swdol/Act/all_char.act').read_bytes();actor.write_bytes(original)
        e=self.edit(clone=True);e['scale']=[1.2,1,1];self.stage(e);self.apply()
        self.assertNotEqual(actor.read_bytes(),original);self.assertEqual(self.c.config['act_dir'],'')
        self.c.workspace.creatures().model(self.free)
        latest=self.root/'output/editor/patches/latest'
        publication.remove_tree(self.root,latest,[latest.parent])
        result=auto.recover_workspace(self.c,server_stopped=True,allow_committed=True)
        self.assertEqual(result['status'],'rolled_back');self.assertEqual(actor.read_bytes(),original)
        for p,b in self.before.items():self.assertEqual(p.read_bytes(),b)

    def test_snapshot_referenced_from_old_package_is_migrated_for_last_backup_recovery(self):
        old=self.root/'output/editor/creature-patches/20260101-010101-12345678'
        shutil.copytree(paired_fixture(PROJECT),old)
        snapshot=old/'baseline/npc-snapshot';shutil.copytree(SNAPSHOT,snapshot)
        doc=read_json(self.settings);doc['config']['npc_snapshot']=str(snapshot)
        doc['validated_files']=[{'path':str(p),'sha256':sha256(p.read_bytes())} for p in snapshot.glob('*.json')]
        write_json(self.settings,doc);self.c=SettingsController(self.root,self.factory)
        self.stage(self.edit(clone=True));self.apply();self.assertFalse(old.exists())
        inputs=self.root/'output/editor/baselines/inputs'
        for source in list(inputs.iterdir()):publication.remove_tree(self.root,source,[inputs])
        auto.recover_workspace(self.c,server_stopped=True,allow_committed=True)
        self.assertIn('baselines/inputs',self.c.config['npc_snapshot'])
        self.assertNotIn(self.free,self.c.workspace.creatures().records)
        reopened=SettingsController(self.root,self.factory);self.assertIsNotNone(reopened.workspace,reopened.setup_error)

    def test_point_capacity_is_global_and_rejected_before_database_queries(self):
        first=self.edit(clone=True)
        point={'action':'add','key':'npcpool:5530','map_id':55,'position':[48359,9999,42999],'count':50,'direction':0}
        first['spawns']=[dict(point) for _ in range(6)]
        second=self.edit(clone=True);second['target_id']=self.free+1;second['spawns']=[dict(point) for _ in range(6)]
        state=self.c.workspace;state.load_creature_draft();state.save_creatures({'revision':0,'edits':[first,second]})
        with self.assertRaisesRegex(ContentError,'500'):
            self.apply(query=lambda sql:self.fail('Database query must not run before capacity guard'))
        self.assertFalse((self.root/'output/editor/backups/latest').exists())

    def test_crash_after_source_draft_clear_restores_original_inputs_on_restart(self):
        class Interrupted(BaseException):pass
        self.stage(self.edit(clone=True));source=self.c.workspace.creature_draft_path
        before=source.read_bytes();original=deployment._replace_file
        def interrupt(staged,target):
            original(staged,target)
            if target==source:raise Interrupted()
        with patch.object(deployment,'_replace_file',side_effect=interrupt),self.assertRaises(Interrupted):self.apply()
        self.assertEqual(read_json(source)['edits'],[])
        reopened=SettingsController(self.root,self.factory);self.assertIsNotNone(reopened.workspace,reopened.setup_error)
        self.assertEqual(source.read_bytes(),before)
        reopened.workspace.load_creature_draft();self.assertEqual(len(reopened.workspace.creature_edits),1)


@unittest.skipUnless(os.environ.get('XYJ_TEST_MYSQL')=='1','Explicit isolated MySQL integration only')
class AutomaticCreatureDatabaseTests(unittest.TestCase):
    setUp=AutomaticCreatureTests.setUp
    edit=AutomaticCreatureTests.edit
    stage=AutomaticCreatureTests.stage
    apply=AutomaticCreatureTests.apply

    def test_points_publish_snapshot_refresh_and_failure_after_sql_restores_both_sides(self):
        from creature_mysql_fixture import isolated_mysql
        with isolated_mysql(PROJECT,self.root/'mysql') as query:
            count=query('SELECT COUNT(*) FROM npcpool;')
            e=self.edit(clone=True);e['spawns']=[{'action':'add','key':'npcpool:5530','map_id':55,'position':[48359,9999,42999],'count':1,'direction':0}]
            self.stage(e);state=self.c.workspace
            exported=export_patch(self.root,state.loaded,state.creature_edits,catalog=state.creatures(),npc_directory=state.npc_directory,client=state.client)
            package=Path(exported['path']);reviewed=read_json(package/'manifest.json')
            self.apply(query=query,package=package)
            self.assertEqual(read_json(self.root/'output/editor/patches/latest/manifest.json'),reviewed)
            self.assertEqual(int(query('SELECT COUNT(*) FROM npcpool;')),int(count)+1)
            self.assertEqual(self.c.workspace.creatures().detail(self.free)['maps'][0]['id'],55)
            saved={p:p.read_bytes() for p in [self.settings,*(self.root/r for r in PAYLOAD_PATHS.values())]}
            last=(self.root/'output/editor/backups/latest/manifest.json').read_bytes()
            e=self.edit(self.free);e['spawns']=[{'action':'add','key':'npcpool:5530','map_id':55,'position':[48559,9999,42999],'count':1,'direction':0}]
            self.stage(e);original=deployment._replace_file;failed=False
            def fail_once(staged,target):
                nonlocal failed
                if target==self.root/PAYLOAD_PATHS['client_language'] and not failed:failed=True;raise OSError('client in use')
                return original(staged,target)
            with patch.object(deployment,'_replace_file',side_effect=fail_once),self.assertRaisesRegex(ContentError,'已恢复'):
                self.apply(query=query)
            self.assertTrue(failed)
            self.assertEqual(int(query('SELECT COUNT(*) FROM npcpool;')),int(count)+1)
            self.assertEqual({p:p.read_bytes() for p in saved},saved)
            self.assertEqual((self.root/'output/editor/backups/latest/manifest.json').read_bytes(),last)
            self.assertEqual(len(self.c.workspace.creature_edits),1)

    def test_database_crash_blocks_startup_and_requires_explicit_stopped_server_recovery(self):
        from creature_mysql_fixture import isolated_mysql
        class Interrupted(BaseException):pass
        with isolated_mysql(PROJECT,self.root/'mysql') as query:
            count=query('SELECT COUNT(*) FROM npcpool;')
            e=self.edit(clone=True);e['spawns']=[{'action':'add','key':'npcpool:5530','map_id':55,'position':[48359,9999,42999],'count':1,'direction':0}]
            self.stage(e);original=deployment._replace_file
            def interrupt(staged,target):
                original(staged,target)
                if target==self.root/PAYLOAD_PATHS['server_object']:raise Interrupted()
            with patch.object(deployment,'_replace_file',side_effect=interrupt),self.assertRaises(Interrupted):self.apply(query=query)
            reopened=SettingsController(self.root,self.factory)
            self.assertIsNone(reopened.workspace);self.assertTrue(auto.recovery_pending(self.root))
            with self.assertRaises(ContentError):auto.recover_workspace(reopened,server_stopped=False)
            with patch('xyj_content.creature_connection.database_query',return_value=(reopened.config,query)):
                self.assertEqual(auto.recover_workspace(reopened,server_stopped=True)['status'],'rolled_back')
            self.assertEqual(query('SELECT COUNT(*) FROM npcpool;'),count)
            for p,b in self.before.items():self.assertEqual(p.read_bytes(),b)
            self.assertIsNotNone(reopened.workspace)
