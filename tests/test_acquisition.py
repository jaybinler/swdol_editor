"""Acquisition semantics, opaque-byte preservation and independently rebuilt native scripts."""

from copy import deepcopy
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import json
import shutil
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from editor_server import EditorState, make_handler
from xyj_content.acquisition import (AcquisitionCatalog, apply_drop_edits, build_script_payloads,
    instructions, normalize_acquisition, probability)
from xyj_content.binary import ContentError, decode_table
from xyj_content.editor import item_detail, validate_edits, prepare_edits, export_editor_patch, verify_editor_patch
from xyj_content.files import write_json
from xyj_content.objects import find_unique
from xyj_content.plot_maps import decode_script
from xyj_content.settings import default_config


class AcquisitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / 'output/editor/verification-0.11').mkdir(parents=True, exist_ok=True)
        baseline = ROOT / 'output/editor/patches/20261104-165921-7d73b3ee/baseline/client/Main'
        cls.config = {**default_config(), 'client_object': str(baseline/'Object.dbf'), 'language': str(baseline/'Language.DBF')}
        cls.state = EditorState(ROOT, config=cls.config)
        cls.catalog = cls.state.acquisition()
        cls.meta = cls.catalog.public()
        cls.shop = cls.meta['shops'][0]['node']
        cls.reward = cls.meta['rewards'][0]['node']

    def edit(self, target=319):
        item = item_detail(self.state.loaded, 10)
        return {'source_id': 10, 'target_id': target, 'name': '获取测试剑',
                'description': item['description'], 'stats': {},
                'acquisition': {'drop': None, 'shops': [], 'rewards': []}}

    def drop_edit(self, target=319):
        edit = self.edit(target)
        edit['acquisition']['drop'] = {'monster_id': 20003,
            'map_id': min(self.state.creatures().map_memberships[20003]), 'percent': 10}
        return edit

    def all_routes(self):
        edit = self.drop_edit()
        edit['acquisition']['shops'] = [{'node': self.shop}]
        edit['acquisition']['rewards'] = [{'node': self.reward, 'count': 1}]
        return edit

    def test_native_probability_boundaries_precision_and_invalid_values(self):
        self.assertEqual(probability(0)['threshold'], -1)
        self.assertEqual(probability(100)['threshold'], 99)
        self.assertAlmostEqual(probability(10)['effective_percent'], 10.009765625)
        for value in (True, None, 'NaN', float('inf'), -0.1, 100.1, [], {}):
            with self.subTest(value=value), self.assertRaises(ContentError): probability(value)

    def test_legacy_draft_compatibility_and_reject_unsupported_extensions(self):
        edit = self.edit(); edit.pop('acquisition')
        self.assertEqual(validate_edits(self.state.loaded, [edit]), [edit])
        for value in ({}, {'drop': [], 'shops': [], 'rewards': []},
                      {'drop': None, 'shops': [{'node': '../file'}], 'rewards': []},
                      {'drop': None, 'shops': [], 'rewards': [{'node': self.reward, 'count': True}]}):
            with self.subTest(value=value), self.assertRaises(ContentError): normalize_acquisition(value)

    def test_drop_is_server_only_single_quantity_and_preserves_other_candidate_bytes(self):
        edit = self.drop_edit(); self.catalog.validate([edit])
        before = find_unique(self.state.loaded.server, 20003)[1]
        blobs, changes = prepare_edits(self.state.loaded, [edit])
        after = find_unique(decode_table(blobs['server_object']), 20003)[1]
        allowed = set(range(254, 302))
        self.assertTrue(all(a == b or i in allowed for i, (a, b) in enumerate(zip(before, after))))
        self.assertEqual(find_unique(decode_table(blobs['client_object']), 20003)[1],
                         find_unique(self.state.loaded.client, 20003)[1])
        rule = next(f['after'] for f in changes[0]['fields'] if f['field'] == '怪物掉落')
        self.assertEqual(rule['count'], 1)
        self.assertEqual(struct.unpack_from('<h', after, 254 + (rule['slot'] - 1) * 2)[0], 319)
        self.assertEqual(before, find_unique(self.state.loaded.server, 20003)[1])

    def test_unproved_map_and_duplicate_existing_drop_rejected(self):
        edit = self.drop_edit(); edit['acquisition']['drop']['map_id'] = 32767
        with self.assertRaises(ContentError): self.catalog.validate([edit])
        edit = self.drop_edit(target=6160); edit['source_id'] = 6160
        with self.assertRaises(ContentError): self.catalog.validate([edit])

    def test_effective_candidate_capacity_includes_all_drafts(self):
        edit = self.drop_edit()
        table = self.state.loaded.server
        idx, row = find_unique(table, 20003); row = bytearray(row)
        struct.pack_into('<12h', row, 254, *([10] * 8 + [-1] * 4))
        struct.pack_into('<12h', row, 278, *([99] * 8 + [-1] * 4))
        loaded = replace(self.state.loaded, server=replace(table, records=table.records[:idx] + (bytes(row),) + table.records[idx+1:]))
        with self.assertRaises(ContentError): apply_drop_edits(loaded, loaded.server, [edit])

    def test_catalog_missing_scripts_keeps_drop_choices_and_explains_failure(self):
        catalog = AcquisitionCatalog(self.state.loaded, self.state.creatures(), None)
        self.assertGreater(len(catalog.public()['monsters']), 1000)
        self.assertEqual(catalog.public()['shops'], [])
        self.assertTrue(catalog.public()['script_error'])
        catalog.validate([self.drop_edit()])
        with self.assertRaises(ContentError): catalog.validate([self.all_routes()])

    def test_shop_append_preserves_old_products_and_all_opaque_function_bodies(self):
        edit = self.edit(); edit['acquisition']['shops'] = [{'node': self.shop}]
        result = build_script_payloads(self.catalog.scripts, [edit])
        filename, pid, pc = self.shop.split(':'); pid, pc = int(pid), int(pc)
        original = self.catalog.scripts[filename]
        before = decode_script(original, filename); after = decode_script(result[filename], filename)
        old = next(w for p, w in instructions(before[pid]) if p == pc)
        new = next(w for p, w in instructions(after[pid]) if w[0] == 556)
        self.assertEqual(new, (556, old[1]+1, *old[2:], 319))
        for other, program in before.items():
            if other != pid:
                self.assertEqual(program[1:], after[other][1:])
                self.assertEqual(program[0][program[1]-8:program[2]], after[other][0][program[1]-8:program[2]])
        # Only the wrapper length and the one pointer change in the entire original file.
        allowed = set(range(2, 6)) | set(range(520 + (pid - int(filename[4:8])*200)*4, 524 + (pid - int(filename[4:8])*200)*4))
        self.assertTrue(all(a == b or i in allowed for i, (a, b) in enumerate(zip(original, result[filename]))))

    def test_reward_append_keeps_original_grant_and_stops_on_new_grant_failure(self):
        edit = self.edit(); edit['acquisition']['rewards'] = [{'node': self.reward, 'count': 2}]
        filename, pid, _ = self.reward.split(':'); pid = int(pid)
        before = instructions(decode_script(self.catalog.scripts[filename], filename)[pid])
        blob = build_script_payloads(self.catalog.scripts, [edit])[filename]
        after = instructions(decode_script(blob, filename)[pid])
        grants_before = [w for _, w in before if w[0] == 900]
        grants_after = [w for _, w in after if w[0] == 900]
        self.assertEqual(grants_after, grants_before + [(900, 319, 2)])
        idx = next(i for i, (_, w) in enumerate(after) if w == (900, 319, 2))
        self.assertEqual(after[idx-1][1][0], 113)
        self.assertEqual(after[idx+1][1][0], 112)
        self.assertEqual(after[idx+2][1], (0,))

    def test_relocation_rewrites_branch_targets_and_retains_all_other_function_data(self):
        # A compiled-style two-function fixture: branches target the next original command.
        body = bytearray(struct.pack('<IIII', 0, 2, 16, 56))
        body.extend(struct.pack('<IIiiiiiiii', 16, 52, 556, 1, 10, 108, 44, 1, 1, 0))
        body.extend(struct.pack('<IIi', 56, 64, 0))
        raw = bytes(512) + bytes(body)
        edit = self.edit(); edit['acquisition']['shops'] = [{'node': 'TALK0000.EXE:0:24'}]
        result = build_script_payloads({'TALK0000.EXE': raw}, [edit])['TALK0000.EXE']
        programs = decode_script(result, 'TALK0000.EXE'); rows = instructions(programs[0])
        jump = next(w for _, w in rows if w[0] == 108)
        self.assertEqual(jump[1], next(pc for pc, w in rows if w == (1,)))
        self.assertEqual(programs[1][1:3], (64, 68))

    def test_corrupt_nodes_unknown_commands_and_overfull_shop_rejected(self):
        for key in ('TALK0010.EXE:2014:3908', 'TALK0010.EXE:9999:3904'):
            edit = self.edit(); edit['acquisition']['shops'] = [{'node': key}]
            with self.assertRaises(ContentError): build_script_payloads(self.catalog.scripts, [edit])
        full = next((n for n in self.meta['shops'] if n['count'] == 32), None)
        if full:
            edit = self.edit(); edit['acquisition']['shops'] = [{'node': full['node']}]
            with self.assertRaises(ContentError): self.catalog.validate([edit])

    def test_export_rebuild_and_tampered_script_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output/editor/verification-0.11') as tmp:
            root = Path(tmp)
            exported = export_editor_patch(root, self.state.loaded, [self.all_routes()], acquisition=self.catalog)
            package = Path(exported['path'])
            self.assertTrue(verify_editor_patch(root, package)['all_records_checked'])
            manifest = json.loads((package/'manifest.json').read_text(encoding='utf-8'))
            entry = next(iter(manifest['scripts'].values()))
            path = package / entry['path']; path.write_bytes(path.read_bytes()[:-4] + bytes(4))
            # Change a non-wrapper byte even if the final word was already zero.
            blob = bytearray(path.read_bytes()); blob[100] ^= 1; path.write_bytes(blob)
            with self.assertRaises(ContentError): verify_editor_patch(root, package)

    def test_http_catalog_and_failed_save_preserve_revision_and_original_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = EditorState(ROOT, config=self.config)
            state.draft_path = Path(tmp) / 'draft.json'
            server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(state))
            thread = Thread(target=server.serve_forever, daemon=True); thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base+'/api/acquisition/catalog') as response: data = json.load(response)
                self.assertGreater(len(data['shops']), 0)
                edit = self.all_routes(); edit['acquisition']['shops'][0]['node'] = 'TALK0010.EXE:9999:3904'
                req = Request(base+'/api/draft', data=json.dumps({'revision': state.revision,'edits':[edit]}).encode(),
                    headers={'Content-Type':'application/json','Origin':base,'X-Editor-Token':state.token})
                with self.assertRaises(HTTPError): urlopen(req)
                self.assertFalse(state.draft_path.exists()); self.assertEqual(state.revision, 0)
            finally:
                server.shutdown(); server.server_close(); thread.join()


class AcquisitionDeploymentTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'output/editor/verification-0.11').mkdir(parents=True, exist_ok=True)
        from xyj_content.settings import SettingsController
        from xyj_content.patch import PAYLOAD_PATHS
        from xyj_content.files import sha256
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'output/editor/verification-0.11')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        baseline = ROOT / 'output/editor/patches/20261104-165921-7d73b3ee/baseline'
        for relative in PAYLOAD_PATHS.values():
            target = self.root / relative; target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(baseline / relative, target)
        self.region = self.root / 'server/qingfeng'
        shutil.copytree(ROOT/'XYJ_SEVVER/qingfeng/ASM', self.region/'ASM')
        shutil.copyfile(ROOT/'XYJ_SEVVER/qingfeng/NewLocalServer.exe', self.region/'NewLocalServer.exe')
        source = self.root/'source/Object.dbf'; source.parent.mkdir()
        shutil.copyfile(self.region/'Object.dbf', source)
        cfg = {**default_config(), 'client_root': str(self.root/'client'), 'server_object': str(source),
               'server_target': str(self.region/'Object.dbf'), 'map_dir': str(self.region),
               'npc_snapshot': str(ROOT/'output/editor/npc-snapshot')}
        self.settings = self.root/'output/editor/settings.json'
        write_json(self.settings, {'format':'xyj-settings/v1', 'config':cfg, 'validated_files':[]})
        self.controller = SettingsController(self.root, lambda config: EditorState(self.root, config=config))
        self.assertIsNotNone(self.controller.workspace, self.controller.setup_error)
        self.state = self.controller.workspace
        meta = self.state.acquisition().public()
        item = item_detail(self.state.loaded, 10)
        edit = {'source_id':10,'target_id':319,'name':'获取测试剑','description':item['description'],'stats':{},
                'acquisition': {'drop': {'monster_id':20003,
                    'map_id': min(self.state.creatures().map_memberships[20003]), 'percent':10},
                    'shops':[{'node':meta['shops'][0]['node']}],
                    'rewards':[{'node':meta['rewards'][0]['node'],'count':1}]}}
        self.state.save({'revision':self.state.revision,'edits':[edit]})
        self.before = {p:p.read_bytes() for p in [self.settings,*[self.root/p for p in PAYLOAD_PATHS.values()],
                       *[self.region/'ASM'/name for name in ('TALK0001.EXE','TALK0010.EXE')]]}

    def test_combined_apply_rebinds_snapshot_and_reopens_with_all_three_routes(self):
        from xyj_content.deployment import apply_workspace, _validate_journal
        from xyj_content.settings import SettingsController
        result = apply_workspace(self.controller, self.state.revision)
        self.assertEqual(result['status'], 'applied')
        current = self.controller.workspace
        self.assertEqual(current.creatures().drop_sources(319)['total'], 1)
        nodes = current.acquisition().public()
        self.assertTrue(any(319 in row['items'] for row in nodes['shops']))
        self.assertTrue(any(319 in row['items'] for row in nodes['rewards']))
        manifest = _validate_journal(self.root, Path(result['backup_path']))
        self.assertEqual(len(manifest['entries']), 7)
        reopened = SettingsController(self.root, lambda cfg: EditorState(self.root, config=cfg))
        self.assertIsNotNone(reopened.workspace, reopened.setup_error)
        self.assertEqual(reopened.workspace.edits, [])

    def test_script_failure_rolls_back_objects_scripts_settings_and_keeps_draft(self):
        from unittest.mock import patch
        from xyj_content import deployment
        original = deployment._replace_file
        failed = False
        def fail_once(staged, target):
            nonlocal failed
            if target.name == 'TALK0010.EXE' and not failed:
                failed = True
                raise OSError('injected script replacement failure')
            return original(staged, target)
        with patch.object(deployment, '_replace_file', side_effect=fail_once), self.assertRaises(ContentError):
            deployment.apply_workspace(self.controller, self.state.revision)
        self.assertTrue(failed)
        for path, before in self.before.items(): self.assertEqual(path.read_bytes(), before, str(path))
        self.assertEqual(len(self.controller.workspace.edits), 1)
        deployment.recover_deployment(self.root)

    def test_changed_target_script_is_rejected_before_replacing_objects(self):
        from xyj_content.deployment import apply_workspace
        target = self.region/'ASM/TALK0010.EXE'
        blob = bytearray(target.read_bytes()); blob[100] ^= 1; target.write_bytes(blob)
        with self.assertRaises(ContentError): apply_workspace(self.controller, self.state.revision)
        for path, before in self.before.items():
            if path != target: self.assertEqual(path.read_bytes(), before)

    def test_interrupted_script_apply_is_recovered_from_the_durable_journal(self):
        from unittest.mock import patch
        from xyj_content import deployment
        original = deployment._replace_file
        def interrupt(staged, target):
            if target.name == 'TALK0010.EXE':
                raise KeyboardInterrupt()
            return original(staged, target)
        with patch.object(deployment, '_replace_file', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            deployment.apply_workspace(self.controller, self.state.revision)
        deployment.recover_deployment(self.root)
        for path, before in self.before.items(): self.assertEqual(path.read_bytes(), before, str(path))
        self.assertEqual(len(self.state.edits), 1)
