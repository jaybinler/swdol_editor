"""Exercise deployment and crash recovery only against disposable paired game-file copies."""

from pathlib import Path, PureWindowsPath
from unittest.mock import patch
import json
import shutil
import struct
import sys
import tempfile
import unittest
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen
from urllib.error import HTTPError

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))
from editor_server import EditorState, make_handler
from xyj_content import deployment
from xyj_content.binary import ContentError, decode_table
from xyj_content.editor import item_detail
from xyj_content.files import write_json
from xyj_content.files import sha256
from xyj_content.objects import find_unique
from xyj_content.patch import PAYLOAD_PATHS
from xyj_content.settings import SettingsController, default_config, input_path


from xyj_content.verification_inputs import paired_fixture


class AutoDeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        baseline = paired_fixture(PROJECT) / 'baseline'
        for relative in PAYLOAD_PATHS.values():
            p = self.root / relative
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(baseline / relative, p)
        self.source = self.root / 'source/Object.dbf'
        self.source.parent.mkdir()
        shutil.copyfile(self.root / PAYLOAD_PATHS['server_object'], self.source)
        self.config = {**default_config(), 'client_root': str(self.root / 'client'),
                       'server_object': str(self.source),
                       'server_target': str(self.root / PAYLOAD_PATHS['server_object']),
                       'npc_snapshot': str(PROJECT / 'output/editor/npc-snapshot'), 'map_dir': ''}
        self.path = self.root / 'output/editor/settings.json'
        checked = [self.source, *(self.root / p for p in PAYLOAD_PATHS.values())]
        snapshot = Path(self.config['npc_snapshot'])
        checked.extend(snapshot.glob('*.json'))
        write_json(self.path, {'format': 'xyj-settings/v1', 'config': self.config,
                              'validated_files': [{'path': str(p), 'sha256': sha256(p.read_bytes())} for p in checked]})
        self.factory = lambda cfg: EditorState(self.root, config=cfg)
        self.c = SettingsController(self.root, self.factory)
        self.assertIsNotNone(self.c.workspace, self.c.setup_error)
        self.targets = deployment.deployment_targets(self.root, self.config)
        self.before = {str(p): p.read_bytes() for p in [*self.targets.values(), self.path]}
        self.stage()

    def stage(self, damage=70, target=10):
        state = self.c.workspace
        item = item_detail(state.loaded, 10)
        edit = {'source_id': 10, 'target_id': target, 'name': item['name'] if target == 10 else '自动测试剑',
                'description': item['description'], 'stats': {'damage': damage, 'delay': 30, 'range': 100}}
        state.save({'revision': state.revision, 'edits': [edit]})

    def apply(self):
        return deployment.apply_workspace(self.c, self.c.workspace.revision)

    def assertOriginal(self):
        for p, blob in self.before.items():
            self.assertEqual(Path(p).read_bytes(), blob, p)
        self.assertEqual(len(self.c.workspace.edits), 1)

    def test_success_rotates_one_backup_and_reopens_without_duplicate_drafts(self):
        source_bytes = self.source.read_bytes()
        result = self.apply()
        self.assertEqual(result['status'], 'applied')
        self.assertTrue(result['verification']['target_files_verified'])
        self.assertEqual(self.c.workspace.edits, [])
        self.assertEqual(self.c.generation, 1)
        first = {str(p): p.read_bytes() for p in self.targets.values()}
        self.stage(80, 319)
        self.apply()
        latest = self.root / 'output/editor/backups/latest'
        manifest = deployment._validate_journal(self.root, latest)
        for entry in manifest['entries'][:3]:
            self.assertEqual((latest / entry['backup']).read_bytes(), first[entry['target']])
        self.assertFalse((latest.parent / 'previous').exists())
        self.assertEqual(list(latest.parent.glob('.next-*')), [])
        reopened = SettingsController(self.root, self.factory)
        self.assertIsNotNone(reopened.workspace, reopened.setup_error)
        self.assertEqual(reopened.workspace.edits, [])
        self.assertTrue(item_detail(reopened.workspace.loaded, 319)['editable'])
        self.assertEqual(item_detail(reopened.workspace.loaded, 319)['stats']['damage'], 80)
        self.assertEqual(reopened.workspace.creatures().drop_sources(6160)['total'], 3)
        self.assertEqual(self.source.read_bytes(), source_bytes)
        for target in self.targets.values():
            self.assertEqual(list(target.parent.glob('.xyj-apply-*')), [])

    def test_second_target_failure_rolls_back_and_preserves_draft(self):
        original = deployment._replace_file
        failed = False
        def fail_once(staged, target):
            nonlocal failed
            if target == self.targets['client_object'] and not failed:
                failed = True
                raise PermissionError('simulated file in use')
            return original(staged, target)
        with patch.object(deployment, '_replace_file', side_effect=fail_once):
            with self.assertRaisesRegex(ContentError, '已恢复'):
                self.apply()
        self.assertOriginal()
        self.assertEqual(SettingsController(self.root, self.factory).setup_error, '')

    def test_settings_replace_failure_restores_all_three_game_files(self):
        original = deployment._replace_file
        failed = False
        def fail_once(staged, target):
            nonlocal failed
            if target == self.path and not failed:
                failed = True
                raise OSError('simulated settings disk failure')
            return original(staged, target)
        with patch.object(deployment, '_replace_file', side_effect=fail_once):
            with self.assertRaisesRegex(ContentError, '已恢复'):
                self.apply()
        self.assertOriginal()

    def test_staging_failure_never_changes_a_target(self):
        with patch.object(deployment, '_stage_file', side_effect=PermissionError('read only share')):
            with self.assertRaisesRegex(ContentError, '已恢复'):
                self.apply()
        self.assertOriginal()

    def test_interruption_after_write_is_recovered_before_settings_validation(self):
        class Interrupted(BaseException):
            pass
        original = deployment._replace_file
        def interrupt(staged, target):
            original(staged, target)
            if target == self.targets['client_object']:
                raise Interrupted()
        with patch.object(deployment, '_replace_file', side_effect=interrupt):
            with self.assertRaises(Interrupted):
                self.apply()
        reopened = SettingsController(self.root, self.factory)
        self.assertIsNotNone(reopened.workspace, reopened.setup_error)
        self.assertOriginal()
        self.assertEqual(len(reopened.workspace.edits), 1)

    def test_external_change_during_write_is_preserved_and_blocks_recovery(self):
        original = deployment._replace_file
        def change(staged, target):
            original(staged, target)
            if target == self.targets['server_object']:
                target.write_bytes(b'external writer')
                raise OSError('unexpected external writer')
        with patch.object(deployment, '_replace_file', side_effect=change):
            with self.assertRaisesRegex(ContentError, '无法自动恢复'):
                self.apply()
        self.assertEqual(self.targets['server_object'].read_bytes(), b'external writer')
        reopened = SettingsController(self.root, self.factory)
        self.assertIsNone(reopened.workspace)
        self.assertIn('无法自动恢复', reopened.setup_error)
        self.targets['server_object'].write_bytes(self.before[str(self.targets['server_object'])])
        self.assertIsNotNone(SettingsController(self.root, self.factory).workspace)

    def test_backup_failure_preserves_previous_backup_and_all_targets(self):
        self.apply()
        latest = self.root / 'output/editor/backups/latest/manifest.json'
        previous = latest.read_bytes()
        self.stage(80)
        before = {str(p): p.read_bytes() for p in self.targets.values()}
        original = deployment.atomic_write
        def fail_backup(path, blob):
            if any(part.startswith('.next-') for part in path.parts):
                raise ContentError('backup disk full')
            return original(path, blob)
        with patch.object(deployment, 'atomic_write', side_effect=fail_backup):
            with self.assertRaisesRegex(ContentError, 'backup disk full'):
                self.apply()
        self.assertEqual(latest.read_bytes(), previous)
        self.assertEqual({str(p): p.read_bytes() for p in self.targets.values()}, before)
        self.assertIsNotNone(SettingsController(self.root, self.factory).workspace)

    def test_changed_target_empty_target_and_stale_revision_rejected_before_backup(self):
        with self.assertRaises(ContentError):
            deployment.apply_workspace(self.c, True)
        with self.assertRaises(ContentError):
            deployment.apply_workspace(self.c, -1)
        self.c.config['server_target'] = ''
        with self.assertRaisesRegex(ContentError, '覆盖目标'):
            self.apply()
        self.c.config['server_target'] = self.config['server_target']
        self.targets['client_language'].write_bytes(b'changed outside editor')
        with self.assertRaisesRegex(ContentError, '不一致'):
            self.apply()
        self.assertFalse((self.root / 'output/editor/backups/latest').exists())

    def test_target_alias_and_advanced_client_override_rejected(self):
        config = {**self.config, 'server_target': str(self.targets['client_object'])}
        with self.assertRaisesRegex(ContentError, '同一个文件'):
            deployment.deployment_targets(self.root, config)
        config = {**self.config, 'client_object': str(self.source)}
        with self.assertRaisesRegex(ContentError, '客户端'):
            deployment.deployment_targets(self.root, config)
        config = {**self.config, 'server_target': str(self.root / 'output/editor/backups/latest/server/Object.dbf')}
        with self.assertRaisesRegex(ContentError, '备份'):
            deployment.deployment_targets(self.root, config)
        self.assertTrue(PureWindowsPath(r'\\fileserver.example\share\qingfeng\Object.dbf').is_absolute())

    def test_two_editors_cannot_deploy_concurrently(self):
        with deployment.deployment_lock(self.root):
            with self.assertRaisesRegex(ContentError, '另一编辑器'):
                self.apply()

    def test_http_uses_saved_targets_and_rejects_extra_paths_and_old_generations(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.c))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def post(value, generation='0'):
            request = Request(base + '/api/export', data=json.dumps(value).encode(), headers={
                'Content-Type': 'application/json', 'Origin': base, 'X-Editor-Token': self.c.token,
                'X-Workspace-Generation': generation})
            with urlopen(request) as response:
                return json.load(response)
        try:
            revision = self.c.workspace.revision
            with self.assertRaises(HTTPError) as failure:
                post({'revision': revision, 'path': str(self.source)})
            self.assertEqual(failure.exception.code, 400)
            self.assertEqual(post({'revision': revision})['status'], 'applied')
            with self.assertRaises(HTTPError):
                post({'revision': revision})
            with self.assertRaises(HTTPError):
                post({'revision': revision}, '1')
            with urlopen(base + '/api/bootstrap') as response:
                data = json.load(response)
            self.assertEqual(data['export_mode'], 'automatic')
            self.assertEqual(data['edits'], [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
