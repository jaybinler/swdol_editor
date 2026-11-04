"""Real-data editor integrity, corrupt resource rejection and HTTP request boundaries."""

from dataclasses import replace
from pathlib import Path
from threading import Thread
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
import json
import struct
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))

from editor_server import EditorState, make_handler
from xyj_content.binary import ContentError, compress, decode_table, read_bytes
from xyj_content.catalog import Inputs, load_inputs
from xyj_content.editor import (FIELDS, export_editor_patch, field_schema, item_detail, prepare_edits,
                                item_id_availability, validate_edits, verify_editor_patch)
from xyj_content.files import sha256, write_json
from xyj_content.language import decode_language
from xyj_content.models import item_model, read_mesh, texture_png
from xyj_content.objects import find_unique, view_record
from xyj_content.patch import PAYLOAD_PATHS


class EditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = load_inputs(Inputs(PROJECT / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf',
                                       PROJECT / 'swdol/Main/Object.dbf', PROJECT / 'swdol/Main/Language.DBF'))

    def edit(self, source=10, target=None):
        item = item_detail(self.loaded, source)
        return {'source_id': source, 'target_id': source if target is None else target, 'name': item['name'],
                'description': item['description'], 'stats': {'damage': 9, 'weight': 3}}

    def test_existing_edit_changes_only_explicit_stat_bytes_on_both_sides(self):
        blobs, changes = prepare_edits(self.loaded, [self.edit()])
        for key, original in [('server_object', self.loaded.server), ('client_object', self.loaded.client)]:
            modified = decode_table(blobs[key])
            self.assertEqual(len(original.records), len(modified.records))
            for before, after in zip(original.records, modified.records):
                if struct.unpack_from('<I', before, 4)[0] == 10:
                    allowed = {104, 105, 172, 173, 174, 175}
                    self.assertTrue(all(a == b or i in allowed for i, (a, b) in enumerate(zip(before, after))))
                    self.assertEqual(struct.unpack_from('<h', after, 104)[0], 9)
                else:
                    self.assertEqual(before, after)
        self.assertEqual(decode_language(blobs['client_language']), self.loaded.language)

    def test_clone_and_edit_preserve_all_original_other_records_and_strings(self):
        edit = self.edit(target=319)
        edit['name'] = '工坊测试剑'
        edit['description'] = ''
        blobs, changes = prepare_edits(self.loaded, [edit, self.edit(source=318)])
        language = decode_language(blobs['client_language'])
        self.assertEqual(language.entries[:len(self.loaded.language.entries)], self.loaded.language.entries)
        self.assertEqual(len(language.entries), len(self.loaded.language.entries) + 2)
        for key, original in [('server_object', self.loaded.server), ('client_object', self.loaded.client)]:
            modified = decode_table(blobs[key])
            index, record = find_unique(modified, 319)
            view = view_record(record, index, language if key.startswith('client') else None)
            self.assertEqual((view.name, view.description), ('工坊测试剑', ''))
            self.assertEqual(record[60:64], find_unique(original, 10)[1][60:64])
            remaining = tuple(r for r in modified.records if struct.unpack_from('<I', r, 4)[0] not in (318, 319))
            self.assertEqual(remaining, tuple(r for r in original.records if struct.unpack_from('<I', r, 4)[0] != 318))

    def test_conflicting_ids_and_unknown_or_overflow_fields_rejected(self):
        cases = []
        edit = self.edit(target=318); cases.append([edit])
        cases.append([self.edit(), self.edit()])
        for value in ({'damage': 32768}, {'damage': True}, {'damage': 1.5}, {'unverified': 0}):
            edit = self.edit(); edit['stats'] = value; cases.append([edit])
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ContentError):
                validate_edits(self.loaded, case)

    def test_id_suggestion_excludes_both_tables_and_multiple_drafts(self):
        first = item_id_availability(self.loaded, [])['suggested_id']
        self.assertIsNotNone(first)
        for side in ('server', 'client'):
            table = getattr(self.loaded, side)
            record = bytearray(find_unique(table, 10)[1])
            struct.pack_into('<I', record, 4, first)
            loaded = replace(self.loaded, **{side: replace(table, records=table.records + (bytes(record),))})
            result = item_id_availability(loaded, [self.edit(target=first + 1), self.edit(target=first + 2)])
            with self.subTest(side=side):
                self.assertIn(first, result['baseline_ids'])
                self.assertNotIn(result['suggested_id'], {first, first + 1, first + 2})
                with self.assertRaises(ContentError):
                    validate_edits(loaded, [self.edit(target=first)])

    def test_id_suggestion_reports_exhaustion_without_mutating_loaded_tables(self):
        record = find_unique(self.loaded.server, 10)[1]
        records = tuple(record[:4] + struct.pack('<I', value) + record[8:] for value in range(1, 32768))
        loaded = replace(self.loaded, server=replace(self.loaded.server, records=records))
        self.assertIsNone(item_id_availability(loaded, [])['suggested_id'])
        self.assertIsNotNone(item_id_availability(self.loaded, [])['suggested_id'])

    def test_two_clones_of_edited_template_use_baseline_and_independent_values(self):
        availability = item_id_availability(self.loaded, [])
        occupied = set(availability['baseline_ids'])
        free = [value for value in range(1, 32768) if value not in occupied][:2]
        original = self.edit(); original['stats'] = {'damage': 99}
        clones = [self.edit(target=target) for target in free]
        clones[0]['stats'] = {}; clones[1]['stats'] = {'damage': -7}
        blobs, _ = prepare_edits(self.loaded, [original, *clones])
        for key in ('server_object', 'client_object'):
            table = decode_table(blobs[key])
            damage = lambda target: struct.unpack_from('<h', find_unique(table, target)[1], 104)[0]
            self.assertEqual(damage(10), 99)
            self.assertEqual(damage(free[0]), item_detail(self.loaded, 10)['stats']['damage'])
            self.assertEqual(damage(free[1]), -7)

    def test_text_encoding_and_unsigned_id_boundary_rejected(self):
        for key, value in [('name', '长'*8), ('description', '\0'), ('name', '剑😀'), ('target_id', 0)]:
            edit = self.edit(); edit[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ContentError):
                validate_edits(self.loaded, [edit])

    def test_cross_side_drift_rejected(self):
        index, record = find_unique(self.loaded.server, 10)
        record = bytearray(record); record[800] ^= 1
        server = replace(self.loaded.server, records=self.loaded.server.records[:index] + (bytes(record),) + self.loaded.server.records[index+1:])
        with self.assertRaisesRegex(ContentError, '不一致'):
            prepare_edits(replace(self.loaded, server=server), [self.edit()])

    def test_character_script_record_cannot_be_edited_as_an_item(self):
        tables = []
        for table in (self.loaded.server, self.loaded.client):
            index, record = find_unique(table, 10)
            record = bytearray(record)
            struct.pack_into('<H', record, 60, 1)  # all_char.act namespace
            tables.append(replace(table, records=table.records[:index] + (bytes(record),) + table.records[index+1:]))
        loaded = replace(self.loaded, server=tables[0], client=tables[1])
        self.assertFalse(item_detail(loaded, 10)['editable'])
        with self.assertRaisesRegex(ContentError, '只读'):
            prepare_edits(loaded, [self.edit()])

    def test_signed_attribute_boundaries_roundtrip(self):
        edit = self.edit()
        edit['stats'] = {'damage': -32768, 'defense': 32767, 'weight': -2147483648, 'delay': 2147483647}
        blobs, _ = prepare_edits(self.loaded, [edit])
        for key in ('client_object', 'server_object'):
            record = find_unique(decode_table(blobs[key]), 10)[1]
            for field, value in edit['stats'].items():
                _, offset, fmt = FIELDS[field]
                self.assertEqual(struct.unpack_from('<'+fmt, record, offset)[0], value)

    def test_expanded_schema_uses_actual_signed_byte_and_word_limits(self):
        schema = {field['key']: field for field in field_schema()}
        self.assertEqual(len(schema), 26)
        for key in ('resist_fire', 'resist_water', 'resist_wind', 'resist_thunder', 'resist_light', 'resist_dark'):
            self.assertEqual((schema[key]['min'], schema[key]['max'], schema[key]['group']), (-128, 127, 'resistances'))
        for key in ('hp_recovery', 'mp_recovery'):
            self.assertEqual((schema[key]['min'], schema[key]['max'], schema[key]['group']), (-32768, 32767, 'recovery'))
        expected_offsets = {'hp_recovery': 130, 'mp_recovery': 132, 'resist_fire': 186, 'resist_water': 187,
                            'resist_wind': 188, 'resist_thunder': 189, 'resist_light': 190, 'resist_dark': 191}
        self.assertEqual({key: schema[key]['offset'] for key in expected_offsets}, expected_offsets)
        self.assertEqual(schema['weight']['max'], 2147483647)

    def test_new_numeric_fields_modify_only_their_bytes_and_never_guard(self):
        edit = self.edit(source=4028)
        edit['stats'] = {'evasion': 5, 'hp_recovery': 7, 'mp_recovery': 9,
                         'resist_fire': -128, 'resist_water': 127, 'resist_wind': 0,
                         'resist_thunder': -1, 'resist_light': 6, 'resist_dark': 12}
        blobs, changes = prepare_edits(self.loaded, [edit])
        allowed = {offset + index for key in edit['stats']
                   for offset, fmt in [FIELDS[key][1:]] for index in range(struct.calcsize('<'+fmt))}
        for key, original in (('client_object', self.loaded.client), ('server_object', self.loaded.server)):
            after = decode_table(blobs[key])
            for before_record, after_record in zip(original.records, after.records):
                if struct.unpack_from('<I', before_record, 4)[0] != 4028:
                    self.assertEqual(before_record, after_record)
                    continue
                self.assertEqual(before_record[472:476], after_record[472:476])
                self.assertTrue(all(a == b or index in allowed for index, (a, b) in enumerate(zip(before_record, after_record))))
                for field, expected in edit['stats'].items():
                    _, offset, fmt = FIELDS[field]
                    self.assertEqual(struct.unpack_from('<'+fmt, after_record, offset)[0], expected)
        self.assertEqual(decode_language(blobs['client_language']), self.loaded.language)
        self.assertNotIn('御劲', [field['field'] for field in changes[0]['fields']])
        edit['stats']['guard'] = 5
        with self.assertRaises(ContentError):
            validate_edits(self.loaded, [edit])

    def test_all_new_fields_accept_boundaries_and_reject_invalid_types_and_overflow(self):
        schema = {field['key']: field for field in field_schema() if field['group'] != 'basic'}
        # Exercise every field at each boundary with three whole-table roundtrips.
        for boundary in ('min', 'zero', 'max'):
            values = {key: 0 if boundary == 'zero' else field[boundary] for key, field in schema.items()}
            edit = self.edit(source=4028); edit['stats'] = values
            blobs, _ = prepare_edits(self.loaded, [edit])
            for side in ('client_object', 'server_object'):
                record = find_unique(decode_table(blobs[side]), 4028)[1]
                for key, expected in values.items():
                    _, offset, fmt = FIELDS[key]
                    with self.subTest(field=key, boundary=boundary, side=side):
                        self.assertEqual(struct.unpack_from('<'+fmt, record, offset)[0], expected)
        for key, field in schema.items():
            for value in (field['min'] - 1, field['max'] + 1, True, False, 1.5):
                edit = self.edit(source=4028); edit['stats'] = {key: value}
                with self.subTest(field=key, invalid=value), self.assertRaises(ContentError):
                    validate_edits(self.loaded, [edit])

    def test_export_rebuild_verifier_detects_rehashed_opaque_byte_tampering(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'output', prefix='editor-test-') as temp:
            root = Path(temp)
            paths = []
            for (key, relative), blob in zip(PAYLOAD_PATHS.items(), self.loaded.blobs):
                path = root / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(blob); paths.append(path)
            loaded = load_inputs(Inputs(*paths))
            result = export_editor_patch(root, loaded, [self.edit(target=319)])
            directory = Path(result['path'])
            self.assertTrue(verify_editor_patch(root, directory)['all_records_checked'])
            from xyj_content.binary import encode_table
            path = directory / PAYLOAD_PATHS['client_object']
            table = decode_table(path.read_bytes())
            index, record = find_unique(table, 319)
            record = bytearray(record); record[810] ^= 1
            table = replace(table, records=table.records[:index] + (bytes(record),) + table.records[index+1:])
            blob = encode_table(table); path.write_bytes(blob)
            manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
            manifest['files']['client_object']['sha256'] = sha256(blob)
            write_json(directory / 'manifest.json', manifest)
            with self.assertRaisesRegex(ContentError, '草稿之外'):
                verify_editor_patch(root, directory)

    def test_real_peach_sword_mapping_and_mesh_bounds(self):
        mesh = item_model(PROJECT / 'swdol', find_unique(self.loaded.client, 10)[1])
        self.assertEqual(mesh['file'], 'ITM00198.PLY')
        self.assertEqual(len(mesh['meshes']), 1)  # attachment helpers are hidden
        self.assertEqual(mesh['meshes'][0]['texture'], 554)
        for actual, expected in zip(mesh['bounds'], mesh['action']['bounds']):
            for a, b in zip(actual, expected): self.assertAlmostEqual(a, b, places=4)
        png = texture_png(PROJECT / 'swdol', 554)
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack_from('>II', png, 16), (128, 128))

    def test_empty_model_and_corrupt_texture_rejected(self):
        with self.assertRaises(ContentError):
            read_mesh(PROJECT / 'swdol/MODEL/ITM00289.PLY')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); (root / 'TEX').mkdir()
            raw = struct.pack('<4H', 65535, 2, 1, 16) + struct.pack('<4H', 8, 3, 0, 0)
            packed = compress(raw)
            (root / 'TEX/ALL00001.TEX').write_bytes(struct.pack('<II', len(raw), len(packed)) + packed)
            with self.assertRaises(ContentError): texture_png(root, 1)


class HTTPBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = EditorState(PROJECT, PROJECT / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(cls.state))
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def test_cross_origin_write_and_host_rebinding_are_rejected(self):
        request = Request(self.url + '/api/draft', data=b'{}', headers={'Content-Type':'application/json',
                          'Origin':'https://untrusted.example', 'X-Editor-Token': self.state.token})
        with self.assertRaises(HTTPError) as failure: urlopen(request)
        self.assertEqual(failure.exception.code, 403)
        request = Request(self.url + '/api/bootstrap', headers={'Host':'untrusted.example'})
        with self.assertRaises(HTTPError) as failure: urlopen(request)
        self.assertEqual(failure.exception.code, 403)

    def test_auto_equipped_profile_tracks_item_requirements_between_requests(self):
        for item, profile, expected in ((3324, 'auto', 'fairy_male'), (3324, 'human_female', 'human_female'),
                                        (10, 'auto', 'human_male'), (3324, 'auto', 'fairy_male')):
            with self.subTest(item=item, profile=profile):
                model = json.load(urlopen(self.url + f'/api/model/{item}?mode=equipped&profile={profile}'))
                self.assertEqual(model['profile'], expected)

    def test_static_allowlist_does_not_expose_game_or_draft_files(self):
        for path in ('/../swdol/Main/Object.dbf', '/output/editor/draft.json', '/vendor/../../tools/editor_server.py'):
            with self.subTest(path=path), self.assertRaises(HTTPError) as failure: urlopen(self.url + path)
            self.assertEqual(failure.exception.code, 404)

    def test_stale_draft_revision_rejected_without_write(self):
        with self.assertRaisesRegex(ContentError, '其他页面'):
            self.state.save({'revision': -1, 'edits': []})

    def test_draft_write_failure_preserves_revision_operations_and_file(self):
        state = self.state
        before = (state.revision, list(state.edits))
        blob = state.draft_path.read_bytes() if state.draft_path.exists() else None
        with patch('editor_server.write_json', side_effect=ContentError('模拟写入失败')):
            with self.assertRaisesRegex(ContentError, '模拟写入失败'):
                state.save({'revision': state.revision, 'edits': []})
        self.assertEqual(before, (state.revision, state.edits))
        self.assertEqual(blob, state.draft_path.read_bytes() if state.draft_path.exists() else None)

    def test_legacy_draft_roundtrip_preserves_edit_and_multiple_clone_operations(self):
        with tempfile.TemporaryDirectory(dir=PROJECT / 'output', prefix='draft-compat-') as directory:
            destination = Path(directory) / 'draft.json'
            with patch('editor_server.output_path', return_value=destination):
                state = EditorState(PROJECT, PROJECT / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf')
                item = item_detail(state.loaded, 10)
                occupied = set(item_id_availability(state.loaded, [])['baseline_ids'])
                free = [value for value in range(1, 32768) if value not in occupied][:2]
                edits = [{'source_id': 10, 'target_id': target, 'name': item['name'],
                          'description': item['description'], 'stats': {'damage': -1}}
                         for target in (10, *free)]
                state.save({'revision': 0, 'edits': edits})
                reopened = EditorState(PROJECT, PROJECT / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf')
                self.assertEqual(reopened.revision, 1)
                self.assertEqual(reopened.edits, edits)
                self.assertEqual(json.loads(destination.read_text('utf-8'))['format'], 'xyj-editor-draft/v1')

    def test_icon_and_dynamic_character_material_endpoints(self):
        icon = urlopen(self.url + '/api/icon/10.png').read()
        sprite = urlopen(self.url + '/api/sprite/3375/0.png').read()
        self.assertEqual(icon, sprite)
        model = json.load(urlopen(self.url + '/api/model/3178?mode=equipped&profile=auto'))
        self.assertEqual(model['profile'], 'human_male')
        self.assertEqual(model['meshes'][0]['texture_url'], '/api/sprite/3015/0.png')

    def test_readonly_template_properties_are_available_in_item_api(self):
        shield = json.load(urlopen(self.url + '/api/item/3369'))['template_properties']
        self.assertEqual([v['value'] for v in shield['resistances']], [12]*6)
        self.assertEqual(shield['hidden_attributes'][0]['source'], 'template')
        item = json.load(urlopen(self.url + '/api/item/4028'))
        hidden = {field['key']: field['value'] for field in item['template_properties']['hidden_attributes']}
        self.assertEqual([hidden[key] for key in ('conversion', 'guard', 'armor_break', 'hp_recovery', 'mp_recovery')],
                         [15, 0, 0, 2, 0])
        sword = json.load(urlopen(self.url + '/api/item/82'))['template_properties']
        self.assertEqual(sword['resistances'][1]['value'], -18)
        self.assertEqual(sword['scope'], 'client_template')

    def test_id_availability_endpoint_is_read_only_and_module_is_served(self):
        before = (self.state.revision, list(self.state.edits))
        result = json.load(urlopen(self.url + '/api/item-ids'))
        self.assertEqual(result['revision'], self.state.revision)
        self.assertNotIn(result['suggested_id'], result['baseline_ids'] + result['draft_ids'])
        self.assertEqual(before, (self.state.revision, self.state.edits))
        module = urlopen(self.url + '/item-workflow.js').read().decode('utf-8')
        self.assertIn('export function replaceDraftOperation', module)

    def test_invalid_profile_and_sprite_frame_fail_without_exposing_files(self):
        for path in ('/api/model/3178?mode=equipped&profile=fairy_female', '/api/sprite/3375/999.png',
                     '/api/model/10?mode=unknown'):
            with self.subTest(path=path), self.assertRaises(HTTPError) as failure:
                urlopen(self.url + path)
            self.assertEqual(failure.exception.code, 400)


if __name__ == '__main__':
    unittest.main()
