"""Slot bitmask, real icon mapping, multi-frame corruption and equipped-avatar integration checks."""

from pathlib import Path
import copy
import struct
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from xyj_content.appearance import PROFILES, equipped_model
from xyj_content.binary import ContentError, decode_table
from xyj_content.models import item_model, read_action, read_mesh, resolve_model, unpack_asset
from xyj_content.objects import find_unique
from xyj_content.sprites import icon_reference, sprite_png, sprite_path
from xyj_content.taxonomy import item_categories
from mesh_intersections import intersection_count


class AppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = ROOT / 'swdol'
        cls.table = decode_table((cls.client / 'Main/Object.dbf').read_bytes())

    def record(self, object_id):
        return find_unique(self.table, object_id)[1]

    def test_slot_masks_preserve_both_hand_bits_and_ignore_names(self):
        sword = item_categories(self.record(10))
        self.assertEqual(sword['slot_mask'], 12)
        self.assertEqual([s['label'] for s in sword['slots']], ['主手', '副手'])
        for object_id, category in ((3311, 'head'), (3178, 'body'), (3054, 'feet'), (3001, 'gloves'), (6001, 'ordinary')):
            self.assertIn(category, item_categories(self.record(object_id))['categories'])
        self.assertFalse(item_categories(self.record(20000))['is_item'])

    def test_unknown_slot_bits_are_explicit_and_not_lost(self):
        record = bytearray(self.record(3311)); struct.pack_into('<I', record, 52, 0x80000001)
        categories = item_categories(record)
        self.assertEqual(categories['slot_mask'], 0x80000001)
        self.assertIn('other', categories['categories'])
        self.assertIn('head', categories['categories'])

    def test_real_icons_use_act_references_and_have_transparency(self):
        reference = icon_reference(self.client, self.record(10))
        self.assertEqual(reference, {'frame': 3375, 'pose': 0})
        self.assertEqual(sprite_path(self.client, 3375).name, 'item00375.tsw')
        for object_id in (10, 3311, 3178, 3054, 6001):
            png = sprite_png(self.client, **icon_reference(self.client, self.record(object_id)))
            self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack_from('>II', png, 16), (40, 40))

    def test_multi_frame_tsw_uses_frame_table_offsets(self):
        first = sprite_png(self.client, 3771, 0)
        second = sprite_png(self.client, 3771, 1)
        self.assertNotEqual(first, second)
        self.assertEqual(struct.unpack_from('>II', second, 16), (256, 256))
        with self.assertRaises(ContentError): sprite_png(self.client, 3771, 11)
        with self.assertRaises(ContentError): sprite_png(self.client, 3771, -1)

    def test_corrupt_frame_offset_fails_before_decompression(self):
        blob = bytearray(sprite_path(self.client, 3375).read_bytes())
        struct.pack_into('<I', blob, 12, len(blob) + 1)
        with patch('xyj_content.sprites.read_bytes', return_value=bytes(blob)):
            with self.assertRaisesRegex(ContentError, '越界'):
                sprite_png.__wrapped__(self.client, 3375, 0)

    def test_six_verified_avatar_profiles_have_native_body_slot_and_mesh(self):
        for key, definition in PROFILES.items():
            action = read_action(self.client, definition['actor'], 0)
            model = read_mesh(resolve_model(self.client, action['frame']))
            self.assertEqual([m['material_index'] for m in model['meshes']], [4, 5])
            self.assertTrue({'1', '2', '3'}.issubset(model['anchors']))
            result = equipped_model(self.client, self.table, self.record(10), key)
            self.assertEqual(result['profile'], key)
            self.assertIn('/api/sprite/', result['meshes'][0]['texture_url'])

    def test_body_texture_is_applied_to_body_material_only(self):
        cloth = equipped_model(self.client, self.table, self.record(3178))
        armor = equipped_model(self.client, self.table, self.record(3183))
        self.assertEqual(cloth['profile'], 'human_male')
        self.assertEqual(cloth['meshes'][0]['texture_url'], '/api/sprite/3015/0.png')
        self.assertEqual(armor['meshes'][0]['texture_url'], '/api/sprite/3012/0.png')
        self.assertNotIn('texture_url', cloth['meshes'][1])
        self.assertEqual(cloth['meshes'][0]['positions'], armor['meshes'][0]['positions'])
        self.assertEqual(cloth['meshes'][1], armor['meshes'][1])

    def test_incompatible_body_variant_and_nonwearable_item_fail_explicitly(self):
        with self.assertRaisesRegex(ContentError, '不兼容'):
            equipped_model(self.client, self.table, self.record(3178), 'fairy_female')
        with self.assertRaisesRegex(ContentError, '普通物品'):
            equipped_model(self.client, self.table, self.record(6001))
        with self.assertRaisesRegex(ContentError, '部位'):
            equipped_model(self.client, self.table, self.record(3054))

    def test_attachment_uses_real_anchor_and_does_not_pollute_cached_mesh(self):
        base = read_mesh(resolve_model(self.client, read_action(self.client, 1, 0)['frame']))
        original = copy.deepcopy(base)
        head = equipped_model(self.client, self.table, self.record(3311))
        self.assertTrue(any(m.get('part') == 'equipment' for m in head['meshes']))
        self.assertEqual(base, original)
        sword = equipped_model(self.client, self.table, self.record(10))
        equipment = next(m for m in sword['meshes'] if m.get('part') == 'equipment')
        standalone = item_model(self.client, self.record(10))['meshes'][0]
        self.assertNotEqual(equipment['positions'], standalone['positions'])
        head_part = next(m for m in head['meshes'] if m.get('part') == 'equipment')
        head_y = head_part['positions'][1::3]
        sword_y = equipment['positions'][1::3]
        self.assertGreater(min(head_y), 75)  # hat must sit above the shoulders, not in a hand
        self.assertGreater(sum(head_y)/len(head_y), sum(sword_y)/len(sword_y) + 20)

    def test_pangu_shield_conservative_box_allows_single_and_offhand_preview(self):
        for object_id in (3369, 4102):
            record = self.record(object_id)
            self.assertEqual(item_categories(record)['slot_mask'], 8)
            model = item_model(self.client, record)
            self.assertEqual(model['file'], 'ITM00051.PLY')
            self.assertEqual(model['declared_bounds'][0][2], 0)
            self.assertGreater(model['bounds'][0][2], 5)
            self.assertEqual(model['vertices'], 43)
            self.assertEqual(model['triangles'], 44)
            for profile in PROFILES:
                worn = equipped_model(self.client, self.table, record, profile)
                parts = [m for m in worn['meshes'] if m.get('part') == 'equipment']
                self.assertTrue(parts)
                self.assertTrue(all(parts[0]['indices'][i] < len(parts[0]['positions'])//3 for i in range(len(parts[0]['indices']))))

    def test_conservative_bounds_do_not_disable_corrupt_box_checks(self):
        path = self.client / 'MODEL/ITM00051.PLY'
        raw = bytearray(unpack_asset(path))
        # Finite but too small: valid geometry must not slip past the containment check.
        struct.pack_into('<f', raw, 4, 0)
        with patch('xyj_content.models.unpack_asset', return_value=bytes(raw)):
            with self.assertRaisesRegex(ContentError, '超出'):
                read_mesh.__wrapped__(path)
        raw = bytearray(unpack_asset(path))
        struct.pack_into('<f', raw, 4, float('nan'))
        with patch('xyj_content.models.unpack_asset', return_value=bytes(raw)):
            with self.assertRaisesRegex(ContentError, '包围盒无效'):
                read_mesh.__wrapped__(path)

    def test_pangu_shield_does_not_cross_human_body_surfaces(self):
        for object_id in (3369, 4102):
            for profile in ('human_male', 'human_female', 'demon_female', 'fairy_male'):
                with self.subTest(object_id=object_id, profile=profile):
                    worn = equipped_model(self.client, self.table, self.record(object_id), profile)
                    body = [m for m in worn['meshes'] if m.get('part') != 'equipment']
                    shield = [m for m in worn['meshes'] if m.get('part') == 'equipment']
                    self.assertEqual(intersection_count(body, shield), 0)

    def test_mainhand_origin_matches_verified_live_mainhand_marker(self):
        # Native world code uses slot 1 for the mainhand actor +0x588 (marker 2).
        marker = {'action': {'scale': [1, 1, 1]}, 'warnings': [], 'meshes': [
            {'positions': [0, 0, 0], 'indices': [], 'uvs': [], 'texture': None}]}
        with patch('xyj_content.appearance.item_model', return_value=marker):
            worn = equipped_model(self.client, self.table, self.record(10), 'human_male')
        origin = next(m['positions'] for m in worn['meshes'] if m.get('part') == 'equipment')
        for actual, expected in zip(origin, (-20.1584396, 39.1203690, -1.4527690)):
            self.assertAlmostEqual(actual, expected, places=4)


if __name__ == '__main__':
    unittest.main()
