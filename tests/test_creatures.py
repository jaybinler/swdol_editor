"""NPC map membership, real saved appearances, probability semantics and read-only HTTP boundaries."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen, Request
from urllib.parse import quote
import json
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from editor_server import EditorState, make_handler
from xyj_content.binary import ContentError
from xyj_content.creatures import CreatureCatalog, SNAPSHOT, generation_chance, inventory_rules, saved_inventory
from xyj_content.models import read_action, read_mesh, resolve_model


class CreatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = EditorState(ROOT, ROOT / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf')
        cls.catalog = cls.state.creatures()

    def test_snapshot_counts_exclude_reserved_id_and_keep_unknown_maps(self):
        meta = self.catalog.metadata()
        self.assertEqual((meta['templates'], meta['instances'], meta['unplaced']), (1437, 10429, 779))
        for zone in (37, 45, 46, 90):
            entry = next(m for m in meta['maps'] if m['id'] == zone)
            self.assertIn('未确认', entry['name'])
            self.assertGreater(entry['instances'], 0)
        self.assertEqual(self.catalog.listing('20001', '46')['items'][0]['matched_instances'], 1)

    def test_multi_map_membership_alias_search_and_unplaced_records(self):
        memberships = self.catalog.detail(20000)['maps']
        self.assertGreater(len(memberships), 1)
        for member in memberships[:2]:
            self.assertIn(20000, [r['id'] for r in self.catalog.listing('20000', str(member['id']))['items']])
        self.assertIn(20208, [r['id'] for r in self.catalog.listing('丁枫[武器]')['items']])
        self.assertEqual(self.catalog.listing(map_id='unplaced')['total'], 779)
        self.assertTrue(all(not r['maps'] for r in self.catalog.listing(map_id='unplaced')['items']))

    def test_repeated_role_numbers_are_namespaced_by_table(self):
        values = list(self.catalog.roles.values())
        duplicate = next(key for key, count in Counter(v['id'] for v in values).items() if count > 1)
        rows = [v for v in values if v['id'] == duplicate]
        self.assertEqual(len({r['key'] for r in rows}), len(rows))
        self.assertEqual(len(self.catalog.roles), 10429)

    def test_page_and_map_parameters_are_bounded(self):
        self.assertEqual(len(self.catalog.listing()['items']), 60)
        for query in ({'page': -1}, {'page': 100001}, {'query': 'x'*129}, {'map_id': '../../data'}, {'map_id': '999999'}):
            with self.subTest(query=query), self.assertRaises(ContentError):
                self.catalog.listing(**query)
        detail = self.catalog.detail(20000, page=999)
        self.assertEqual(detail['instance_page'], detail['instance_pages'] - 1)

    def test_threshold_boundary_is_not_reported_as_kill_rate(self):
        self.assertEqual(generation_chance(-1), 0)
        self.assertAlmostEqual(generation_chance(0), 32800/32768)
        self.assertAlmostEqual(generation_chance(5), 196800/32768)
        self.assertEqual(generation_chance(99), 100)
        self.assertEqual(generation_chance(32767), 100)
        wolf = self.catalog.detail(20003)
        self.assertEqual([r['id'] for r in wolf['inventory']], [6160, 3062, 6712, 6007])
        self.assertTrue(all(r['drop_percent'] is None for r in wolf['inventory']))
        self.assertEqual(wolf['drop_status'], 'unconfirmed')

    def test_unknown_items_and_capacity_are_not_silently_removed(self):
        record = bytearray(self.catalog.records[20000])
        struct.pack_into('<12h', record, 254, *([32760]*12))
        rows = inventory_rules(record, self.catalog.definitions)
        self.assertEqual(len(rows), 12)
        self.assertFalse(rows[0]['known'])
        self.assertTrue(rows[8]['capacity_limited'])
        self.assertFalse(rows[7]['capacity_limited'])

    def test_generation_probability_matches_exhaustive_native_output_domain(self):
        for threshold in (-1, 0, 5, 10, 50, 67, 68, 98, 99, 100):
            actual = sum(value % 100 <= threshold for value in range(32768)) * 100 / 32768
            self.assertEqual(generation_chance(threshold), actual)

    def test_saved_inventory_keeps_instance_identity_and_unknown_separate_from_empty(self):
        self.assertEqual(saved_inventory({}, self.catalog.definitions)['status'], 'not_collected')
        empty = {'hand_items':[-1]*9, 'hand_counts':[0]*9}
        self.assertEqual(saved_inventory(empty, self.catalog.definitions), {'status':'stored','items':[]})
        one = {**empty, 'hand_items':[3901]+[-1]*8, 'hand_counts':[2]+[0]*8}
        two = {**empty, 'hand_items':[6160]+[-1]*8, 'hand_counts':[7]+[0]*8}
        a, b = [saved_inventory(row, self.catalog.definitions)['items'][0] for row in (one,two)]
        self.assertEqual((a['id'],a['count']), (3901,2))
        self.assertEqual((b['id'],b['count']), (6160,7))
        self.assertEqual(one['hand_items'][0],3901)

    def test_v2_ghost_instances_have_empty_stored_slots_and_shared_template_rules(self):
        directory = ROOT / 'output/editor/npc-snapshot-inventory-v2'
        catalog = CreatureCatalog(ROOT, self.catalog.loaded, directory=directory)
        detail = catalog.detail(20995, '52')
        self.assertEqual(detail['instance_total'],14)
        self.assertTrue(all(r['saved_inventory'] == {'status':'stored','items':[]} for r in detail['instances']))
        self.assertEqual([(r['id'],r['threshold']) for r in detail['inventory']], [(28309,10),(3901,50)])
        self.assertTrue(all(r['drop_percent'] is None for r in detail['inventory']))
        self.assertIn('Object 模板',detail['drop_note'])
        # Distinct stored rows must remain distinct through the public detail projection.
        first, second = catalog.instances[20995][:2]
        first['hand_items'][0], first['hand_counts'][0] = 3901, 2
        second['hand_items'][0], second['hand_counts'][0] = 6160, 7
        by_key = {row['key']: row['saved_inventory']['items'] for row in catalog.detail(20995, '52')['instances']}
        self.assertEqual((by_key[first['key']][0]['id'], by_key[first['key']][0]['count']), (3901, 2))
        self.assertEqual((by_key[second['key']][0]['id'], by_key[second['key']][0]['count']), (6160, 7))

    def test_saved_humanoid_uses_actual_body_and_does_not_mutate_cache(self):
        role = self.catalog.instances[20135][0]
        self.assertEqual(role['equipment'][1], 5307)
        a, m = struct.unpack_from('<HH', self.catalog.records[20135], 60)
        base = read_mesh(resolve_model(self.catalog.client, read_action(self.catalog.client, a, m)['frame']))
        before = deepcopy(base)
        model = self.catalog.model(20135, role['key'])
        item = self.catalog.objects[5307]
        actor, mode = struct.unpack_from('<HH', item, 60)
        action = read_action(self.catalog.client, actor, mode+2)
        body = next(m for m in model['meshes'] if m['material_index'] == 4)
        self.assertEqual(body['texture_url'], f"/api/sprite/{action['frame']}/{action.get('pose', 0)}.png")
        self.assertEqual(base, before)
        with self.assertRaisesRegex(ContentError, '不属于'):
            self.catalog.model(20000, role['key'])

    def test_unsupported_mesh_does_not_prevent_detail_or_other_models(self):
        with patch('xyj_content.creatures.read_action', side_effect=ContentError('损坏的动作')):
            with self.assertRaises(ContentError):
                self.catalog.model(20238)
        self.assertEqual(self.catalog.detail(20238)['id'], 20238)
        self.assertEqual(self.catalog.model(20000)['file'], 'CHR00239.PLY')

    def test_native_numeric_render_flags_allow_ghost_mesh_without_hiding_effect_limitations(self):
        model = self.catalog.model(20238)
        self.assertEqual(model['action']['render_flags'], 3)
        self.assertTrue(any('混合与深度' in warning for warning in model['warnings']))
        raw = struct.pack('<HIH', 1, 8, 4) + b'\xff\x00VO'
        with patch('xyj_content.models.actor_data', return_value=raw):
            with self.assertRaisesRegex(ContentError, '暂未解析'):
                read_action(self.catalog.client, 25, 0)

    def test_manifest_corruption_and_source_drift_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as tmp:
            root = Path(tmp)
            target = root / SNAPSHOT
            shutil.copytree(ROOT / SNAPSHOT, target)
            manifest = json.loads((target / 'manifest.json').read_text('utf-8'))
            catalog = target / manifest['catalog_file']
            catalog.write_bytes(catalog.read_bytes() + b' ')
            with self.assertRaisesRegex(ContentError, '校验失败'):
                CreatureCatalog(root, self.state.loaded)
        drift = replace(self.state.loaded, blobs=(b'drift', *self.state.loaded.blobs[1:]))
        with self.assertRaisesRegex(ContentError, '基线不一致'):
            CreatureCatalog(ROOT, drift)


class CreatureHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = EditorState(ROOT, ROOT / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(cls.state))
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def test_readonly_routes_pagination_and_raw_files_not_exposed(self):
        data = json.load(urlopen(self.url + '/api/creatures?q=' + quote('野狼') + '&map=all'))
        self.assertTrue(any(r['id'] == 20003 for r in data['items']))
        detail = json.load(urlopen(self.url + '/api/creatures/20003'))
        self.assertLessEqual(len(detail['instances']), 60)
        self.assertNotIn('AccountID', json.dumps(detail))
        for route in ('/output/editor/npc-snapshot/manifest.json', '/npcpool.MYD', '/api/creatures/999999',
                      '/api/creatures?page=-1', '/api/creatures/20003/model?role=npcpool:8756'):
            with self.subTest(route=route), self.assertRaises(HTTPError) as error:
                urlopen(self.url + route)
            self.assertIn(error.exception.code, (400, 404))

    def test_npc_read_does_not_change_draft_or_enable_write_endpoint(self):
        before = (self.state.revision, deepcopy(self.state.edits))
        json.load(urlopen(self.url + '/api/creatures/catalog'))
        model = json.load(urlopen(self.url + '/api/creatures/20000/model'))
        self.assertEqual(model['mode'], 'creature')
        request = Request(self.url + '/api/creatures/20000', data=b'{}', headers={
            'Origin': self.url, 'X-Editor-Token': self.state.token, 'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 404)
        self.assertEqual((self.state.revision, self.state.edits), before)


if __name__ == '__main__':
    unittest.main()
