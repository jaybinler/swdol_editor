"""Real-resource portable settings transactions and credential/database boundaries."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from threading import Thread
from urllib.request import Request, urlopen
from urllib.error import HTTPError

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))
from editor_server import EditorState, LocalServer, make_handler
from xyj_content.binary import ContentError
from xyj_content.database import _query, connection_binding, read_npc_rows, write_snapshot, FIELDS
from xyj_content.editor import export_editor_patch
from xyj_content.secrets_windows import protect_password, unprotect_password
from xyj_content.settings import SettingsController, default_config, normalize_config, resolve_paths


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for relative in ('editor/node_modules/three/build/three.module.js', 'editor/node_modules/three/build/three.core.js',
                         'editor/node_modules/three/examples/jsm/controls/OrbitControls.js'):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(PROJECT / relative, target)
        self.config = default_config()
        for name in ('client_root', 'server_object', 'map_dir', 'npc_snapshot'):
            self.config[name] = str(PROJECT / self.config[name])
        self.factory = lambda cfg: EditorState(self.root, config=cfg)
        self.controller = SettingsController(self.root, self.factory)
        self.assertIsNone(self.controller.workspace)  # Fresh installation has no game files.

    def validate(self):
        result = self.controller.validate({'config': self.config, 'password': ''})
        self.assertTrue(result['valid'], result)
        return {'config': result['config'], 'receipt': result['receipt']}

    def test_relocated_root_validation_save_reload_and_external_patch(self):
        request = self.validate()
        self.assertFalse(self.controller.path.exists())
        self.controller.apply(request)
        workspace = self.controller.workspace
        self.assertTrue(workspace.draft_path.is_relative_to(self.root / 'output/editor/workspaces'))
        self.assertEqual(workspace.creatures().metadata()['instances'], 10429)
        workspace.save({'revision': 0, 'edits': []})
        reopened = SettingsController(self.root, self.factory)
        self.assertIsNotNone(reopened.workspace, reopened.setup_error)
        self.assertEqual(reopened.workspace.revision, 1)
        from xyj_content.editor import item_detail
        item = item_detail(workspace.loaded, 10)
        edit = {'source_id': 10, 'target_id': 10, 'name': item['name'], 'description': item['description'], 'stats': {'damage': 9}}
        exported = export_editor_patch(self.root, workspace.loaded, [edit])
        self.assertEqual(exported['verification']['status'], 'offline_validated_runtime_pending')
        document = json.loads((Path(exported['path']) / 'manifest.json').read_text('utf-8'))
        self.assertTrue(Path(document['sources']['server_object']['path']).is_absolute())

    def test_failed_validation_and_forged_receipt_cannot_save(self):
        self.config['client_root'] = str(self.root / 'missing')
        self.assertFalse(self.controller.validate({'config': self.config, 'password': ''})['valid'])
        with self.assertRaises(ContentError):
            self.controller.apply({'config': self.config, 'receipt': 'forged'})
        self.assertFalse(self.controller.path.exists())

    def test_edit_expiry_and_file_change_reject_save(self):
        request = self.validate()
        changed = deepcopy(request)
        changed['config']['map_dir'] = ''
        with self.assertRaises(ContentError):
            self.controller.apply(changed)
        pending = self.controller.pending
        pending['expires'] = time.monotonic() - 1
        with self.assertRaises(ContentError):
            self.controller.apply(request)
        request = self.validate()
        changed_file = self.root / 'mutable-input'
        changed_file.write_bytes(b'changed')
        self.controller.pending['fingerprints'].append({'path': str(changed_file), 'sha256': '0' * 64})
        with self.assertRaises(ContentError):
            self.controller.apply(request)
        self.assertIsNone(self.controller.workspace)

    def test_draft_revision_and_write_failure_keep_active_workspace(self):
        self.controller.apply(self.validate())
        old = self.controller.workspace
        saved = self.controller.path.read_bytes()
        request = self.validate()
        old.save({'revision': old.revision, 'edits': []})
        with self.assertRaises(ContentError):
            self.controller.apply(request)
        request = self.validate()
        with patch('xyj_content.settings.atomic_write', side_effect=ContentError('disk full')):
            with self.assertRaises(ContentError):
                self.controller.apply(request)
        self.assertIs(self.controller.workspace, old)
        self.assertEqual(self.controller.path.read_bytes(), saved)

    def test_corrupt_settings_starts_setup_and_preserves_file(self):
        self.controller.path.parent.mkdir(parents=True)
        self.controller.path.write_bytes(b'corrupt')
        loaded = SettingsController(self.root, self.factory)
        self.assertIsNone(loaded.workspace)
        self.assertTrue(loaded.setup_error)
        self.assertEqual(self.controller.path.read_bytes(), b'corrupt')

    def test_explicit_resource_overrides_and_invalid_database_fields(self):
        for key in ('act_dir', 'model_dir', 'tex_dir', 'tsw_dir'):
            self.config[key] = str(PROJECT / 'swdol' / {'act_dir':'Act','model_dir':'MODEL','tex_dir':'TEX','tsw_dir':'Tsw'}[key])
        request = self.validate()
        resources = resolve_paths(self.root, request['config'])[1]
        self.assertEqual(resources / 'MODEL', PROJECT / 'swdol/MODEL')
        for key, value in [('port', True), ('name', '--execute=DROP'), ('host', 'host\narg'), ('unknown', 'x')]:
            config = deepcopy(self.config)
            config['database'][key] = value
            with self.subTest(key=key), self.assertRaises(ContentError):
                normalize_config(self.root, config)

    def test_snapshot_validation_never_rewrites_existing_manifest(self):
        one = write_snapshot(self.root, [], [], 'a'*64, {'server':'test'})
        original = (one / 'manifest.json').read_bytes()
        two = write_snapshot(self.root, [], [], 'a'*64, {'server':'test'})
        self.assertNotEqual(one, two)
        self.assertEqual((one / 'manifest.json').read_bytes(), original)

    def test_first_run_http_guards_and_stale_workspace(self):
        server = LocalServer(('127.0.0.1', 0), make_handler(self.controller))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        def request(path, data=None, **headers):
            encoded = json.dumps(data).encode() if data is not None else None
            return urlopen(Request(base + path, data=encoded, headers=headers), timeout=15)
        try:
            with request('/api/bootstrap') as response:
                self.assertFalse(json.load(response)['ready'])
            with self.assertRaises(HTTPError) as caught:
                request('/api/settings/save', {}, **{'Content-Type':'application/json', 'Origin':base})
            self.assertEqual(caught.exception.code, 403)
            self.controller.apply(self.validate())
            with request('/api/settings/export') as response:
                exported = response.read()
                self.assertNotIn(b'secret_file', exported)
                self.assertNotIn(b'receipt', exported)
            with self.assertRaises(HTTPError) as caught:
                request('/api/draft', {'revision':0,'edits':[]}, **{'Content-Type':'application/json', 'Origin':base,
                        'X-Editor-Token':self.controller.token, 'X-Workspace-Generation':'0'})
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(self.controller.workspace.revision, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


class CredentialDatabaseTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_dpapi_roundtrip_bound_to_connection(self):
        binding = connection_binding(default_config()['database'])
        password = 'synthetic-test-password-123'
        cipher = protect_password(password, binding)
        self.assertNotIn(password.encode(), cipher)
        self.assertEqual(unprotect_password(cipher, binding), password)
        with self.assertRaises(ContentError):
            unprotect_password(cipher, b'different endpoint')

    def test_native_execution_hash_sql_and_password_boundaries(self):
        config = {**default_config()['database'], 'client': str(PROJECT / 'XYJ_SEVVER/mysql/bin/mysql.exe')}
        with patch('xyj_content.database.subprocess.run') as run:
            with self.assertRaises(ContentError):
                _query(config, 'synthetic-secret', 'SELECT 1 INTO OUTFILE "bad";')
            run.assert_not_called()
        result = SimpleNamespace(returncode=1, stdout=b'', stderr=b'ERROR 1045 synthetic-secret')
        with patch('xyj_content.database.subprocess.run', return_value=result) as run:
            with self.assertRaises(ContentError) as caught:
                _query(config, 'synthetic-secret', 'SHOW COLUMNS FROM npcpool;')
            args, kwargs = run.call_args
            self.assertNotIn('synthetic-secret', ' '.join(args[0]))
            self.assertEqual(kwargs['env']['MYSQL_PWD'], 'synthetic-secret')
            self.assertNotIn('synthetic-secret', str(caught.exception))
            self.assertIn('1045', str(caught.exception))
        config['client'] = str(PROJECT / 'requirements.txt')
        with patch('xyj_content.database.subprocess.run') as run:
            with self.assertRaises(ContentError):
                _query(config, '', 'SHOW COLUMNS FROM npcpool;')
            run.assert_not_called()

    def test_projection_namespaces_and_schema_failure(self):
        schema = '\n'.join((*FIELDS, 'Removed'))
        values = {field: '0' for field in FIELDS}
        values.update(RoleID='1', OriginalObjectID='20000', SectionID='1', RoleName='B1F9B7F9', NickName='')
        row = '\t'.join(values[field] for field in FIELDS)
        with patch('xyj_content.database._query', side_effect=[schema, row, schema, row]):
            rows = read_npc_rows(default_config()['database'], '')
        self.assertNotEqual(rows[0]['key'], rows[1]['key'])
        self.assertEqual(rows[0]['template_id'], 20000)
        with patch('xyj_content.database._query', return_value='RoleID'):
            with self.assertRaises(ContentError):
                read_npc_rows(default_config()['database'], '')


if __name__ == '__main__':
    unittest.main()
