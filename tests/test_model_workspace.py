"""Model document persistence and complete resource publication against copied game targets."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import base64
import json
import shutil
import struct
import unittest

import test_creature_auto as existing
from xyj_content import model_workspace as workspace,creature_auto,deployment,native_mesh
from xyj_content.binary import ContentError
from xyj_content.files import sha256,read_json,write_json
from xyj_content.creature_editor import export_patch,verify_patch


PROJECT=existing.PROJECT


class ModelWorkspaceTests(unittest.TestCase):
    setUp=existing.AutomaticCreatureTests.setUp
    edit=existing.AutomaticCreatureTests.edit
    stage=existing.AutomaticCreatureTests.stage

    def blob(self,data):
        key=sha256(data)
        for offset in range(0,len(data),400000):
            workspace.upload(self.c.workspace,dict(id=key,offset=offset,total=len(data),data=base64.b64encode(data[offset:offset+400000]).decode()))
        return key

    def model(self,template=20000):
        state=self.c.workspace;opened=workspace.open_document(state,template)
        docs=[deepcopy(m['document']) for m in opened['models']]
        docs[0]['vertices'][0]['position'][0]+=2
        docs[0]['bones'][0]['translation'][1]=1
        next(m for m in docs[0]['materials'] if m['texture']>=0)['tint']=[.5,1,1]
        keys=[self.blob(workspace.canonical(d)) for d in docs]
        result=workspace.save_document(state,dict(template_id=template,revision=opened['revision'],actor_sha256=opened['actor_sha256'],source_stamp=opened['source_stamp'],base_ref=opened['reference'],documents=keys))
        return opened,result

    def copy_targets(self):
        opened=workspace.open_document(self.c.workspace,20000,fresh=True)
        textures={m['texture'] for model in opened['models'] for m in model['document']['materials'] if m['texture']>=0}
        names=[*('Act/'+name for name in workspace.ACT_FILES),'MODEL/ALL_CHAR.IDX','TEX/ALL_TEX.IDX',
               *('MODEL/'+m['file'] for m in opened['models']),*(f'TEX/ALL{i:05d}.TEX' for i in textures)]
        for name in names:
            p=self.root/'client'/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(PROJECT/'swdol'/name,p)

    def test_model_document_reopens_exactly_and_stale_save_is_rejected(self):
        opened,saved=self.model();ref=saved['reference']
        reopened=workspace.open_document(self.c.workspace,20000,ref)
        self.assertEqual(reopened['models'][0]['document']['bones'][0]['translation'][1],1)
        docs=[self.blob(workspace.canonical(m['document'])) for m in reopened['models']]
        with self.assertRaisesRegex(ContentError,'另一窗口'):
            workspace.save_document(self.c.workspace,dict(template_id=20000,revision=opened['revision'],actor_sha256=opened['actor_sha256'],source_stamp=opened['source_stamp'],base_ref=opened['reference'],documents=docs))

    def test_green_woman_keeps_48_action_models_but_allocates_only_edited_model(self):
        opened,saved=self.model(20087)
        self.assertEqual(len(opened['models']),48)
        edit=self.edit(20087,clone=True);edit['model_ref']=saved['reference']
        result=export_patch(self.root,self.c.workspace.loaded,[edit],catalog=self.c.workspace.creatures(),
                            npc_directory=self.c.workspace.npc_directory,client=self.c.workspace.client)
        package=Path(result['path']);manifest=read_json(package/'manifest.json')
        self.assertEqual(len([name for name in manifest['resources'] if name.endswith('.PLY')]),1)
        self.assertEqual(len(read_json(package/'model-bundles'/saved['reference']/'bundle.json')['documents']),48)
        self.assertTrue(verify_patch(package)['all_records_checked'])

    def test_full_model_package_independent_rebuild_and_new_file_rollback(self):
        self.copy_targets();_,saved=self.model()
        edit=self.edit(20000);edit['model_ref']=saved['reference'];self.stage(edit)
        before={p:p.read_bytes() for p in (self.root/'client').rglob('*') if p.is_file()}
        exported=export_patch(self.root,self.c.workspace.loaded,[edit],catalog=self.c.workspace.creatures(),
                              npc_directory=self.c.workspace.npc_directory,client=self.c.workspace.client)
        package=Path(exported['path']);self.assertGreater(verify_patch(package)['resources'],3)
        resources=read_json(package/'manifest.json')['resources']
        self.assertTrue(any(p.endswith('.TEX') and m['source_sha256'] is None for p,m in resources.items()))
        original=creature_auto._write;failed=False
        def fail(entry,blob):
            nonlocal failed
            if entry['role']=='settings' and not failed:failed=True;raise OSError('isolated settings failure')
            return original(entry,blob)
        with patch.object(creature_auto,'_write',side_effect=fail),self.assertRaisesRegex(ContentError,'已恢复'):
            creature_auto.apply_workspace(self.c,self.c.workspace.creature_revision,server_stopped=True,package=package)
        self.assertEqual({p:p.read_bytes() for p in (self.root/'client').rglob('*') if p.is_file()},before)
        result=creature_auto.apply_workspace(self.c,self.c.workspace.creature_revision,server_stopped=True,package=package)
        self.assertEqual(result['status'],'applied')
        journal=deployment._validate_journal(self.root,self.root/'output/editor/backups/latest')
        self.assertTrue(any(e['role'].startswith('model:') and e['before'] is None for e in journal['entries']))
        row=next(r for r in self.c.workspace.loaded.client.records if struct.unpack_from('<I',r,4)[0]==20000)
        self.assertNotEqual(struct.unpack_from('<H',row,60)[0],25)
        for relative,meta in resources.items():
            self.assertEqual(sha256((self.root/relative).read_bytes()),meta['sha256'])
        project=workspace.project_for(self.c.workspace,self.c.document,20000)
        self.assertEqual(project['reference'],saved['reference'])
        reopened=workspace.open_document(self.c.workspace,20000,project['reference'])
        self.assertEqual(reopened['actor'],25)
        self.assertEqual(reopened['models'][0]['document']['bones'][0]['translation'][1],1)
        docs=[m['document'] for m in reopened['models']]
        docs[0]['bones'][0]['name']='再次编辑'
        resaved=workspace.save_document(self.c.workspace,dict(template_id=20000,revision=reopened['revision'],
                    actor_sha256=reopened['actor_sha256'],source_stamp=reopened['source_stamp'],
                    base_ref=reopened['reference'],documents=[self.blob(workspace.canonical(d)) for d in docs]))
        self.assertNotEqual(resaved['reference'],project['reference'])

    def test_texture_bundle_copy_uses_short_members_and_accepts_legacy_images(self):
        source = self.root / 'legacy-bundle'
        image = b'rgba fixture'
        digest = sha256(image)
        (source / 'images').mkdir(parents=True)
        (source / 'images' / (digest + '.rgba')).write_bytes(image)
        bundle = dict(format='xyj-model-bundle/v1', sources={}, images=[digest])
        reference = sha256(workspace.canonical(bundle))
        write_json(source / 'bundle.json', bundle)
        target = self.root / ('patch-directory-' + 'a' * 36) / 'model-bundles' / reference
        workspace.copy_bundle(source, target, reference)
        compact = target / 'images' / '000.rgba'
        self.assertEqual(compact.read_bytes(), image)
        self.assertEqual(workspace.load_bundle(target, reference), bundle)
        compact.write_bytes(b'changed')
        with self.assertRaisesRegex(ContentError, '校验失败'):
            workspace.load_bundle(target, reference)

    def test_upload_retries_do_not_append_duplicate_bytes_and_hash_failures_are_rejected(self):
        data=b'bounded bytes';key=sha256(data)
        first=dict(id=key,offset=0,total=len(data),data=base64.b64encode(data[:4]).decode())
        self.assertEqual(workspace.upload(self.c.workspace,first)['bytes'],4)
        self.assertEqual(workspace.upload(self.c.workspace,first)['bytes'],4)
        with self.assertRaises(ContentError):workspace.upload(self.c.workspace,{**first,'offset':3})
        with self.assertRaises(ContentError):workspace.upload(self.c.workspace,dict(id='0'*64,offset=0,total=4,data=base64.b64encode(b'fail').decode()))

    def test_changed_resource_snapshot_is_rejected_without_committing_model_session(self):
        from dataclasses import replace
        state=self.c.workspace;initial=workspace.open_document(state,20000)
        folder=self.root/'isolated-tex';folder.mkdir()
        textures={m['texture'] for model in initial['models'] for m in model['document']['materials'] if m['texture']>=0}
        for name in ['ALL_TEX.IDX',*(f'ALL{i:05d}.TEX' for i in textures)]:shutil.copyfile(state.client/'TEX'/name,folder/name)
        state.client=replace(state.client,tex=folder)
        opened=workspace.open_document(state,20000);keys=[self.blob(workspace.canonical(m['document'])) for m in opened['models']]
        index=folder/'ALL_TEX.IDX';raw=bytearray(index.read_bytes());raw[-1]^=1;index.write_bytes(raw)
        with self.assertRaisesRegex(ContentError,'来源已变化'):
            workspace.save_document(state,dict(template_id=20000,revision=opened['revision'],actor_sha256=opened['actor_sha256'],source_stamp=opened['source_stamp'],base_ref=opened['reference'],documents=keys))
        self.assertFalse(workspace.session_path(state,20000).exists())
