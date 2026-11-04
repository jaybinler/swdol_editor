"""Native-backed template display, condition boundaries and read-only patch integrity."""

from dataclasses import replace
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from xyj_content.binary import ContentError, decode_table
from xyj_content.catalog import Inputs, load_inputs
from xyj_content.editor import item_detail, prepare_edits, validate_edits
from xyj_content.item_properties import template_properties
from xyj_content.objects import find_unique


class ItemPropertiesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = load_inputs(Inputs(ROOT / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf',
                                       ROOT / 'swdol/Main/Object.dbf', ROOT / 'swdol/Main/Language.DBF'))

    def record(self, object_id=3369):
        return bytearray(find_unique(self.loaded.client, object_id)[1])

    def test_real_pangu_shield_six_resistances_are_twelve(self):
        values = template_properties(self.record())
        self.assertEqual([v['label'] for v in values['resistances']], ['抗火', '抗水', '抗风', '抗雷', '抗光', '抗暗'])
        self.assertEqual([v['value'] for v in values['resistances']], [12]*6)
        self.assertEqual(values['hidden_attributes'][1]['unit'], '%')

    def test_real_clothing_race_restriction_and_multi_professions(self):
        groups = template_properties(self.record(3178))['requirements']
        self.assertEqual(groups[0]['allowed'], ['人族'])
        self.assertEqual(groups[1]['text'], '不限')
        self.assertEqual(groups[2]['allowed'], ['剑', '刀', '武', '刺', '药', '阴'])

    def test_all_bits_and_empty_allow_lists_are_distinct(self):
        record = self.record()
        struct.pack_into('<I', record, 356, 0xffffffff)
        struct.pack_into('<h', record, 80, 3)
        self.assertEqual([v['status'] for v in template_properties(record)['requirements']], ['all']*3)
        struct.pack_into('<I', record, 356, 0)
        self.assertEqual([v['status'] for v in template_properties(record)['requirements']], ['none']*3)

    def test_sex_code_and_mask_must_both_allow_the_sex(self):
        record = self.record()
        struct.pack_into('<I', record, 356, 0xffffffff)
        for raw, expected in ((1, ['男']), (2, ['女']), (3, ['男', '女'])):
            struct.pack_into('<h', record, 80, raw)
            self.assertEqual(template_properties(record)['requirements'][1]['allowed'], expected)
        struct.pack_into('<h', record, 80, 1)
        struct.pack_into('<I', record, 356, 0xffff7fff)  # mask allows only female, code requires male
        self.assertEqual(template_properties(record)['requirements'][1]['status'], 'none')
        struct.pack_into('<h', record, 80, 7)
        self.assertEqual(template_properties(record)['requirements'][1]['status'], 'unknown')

    def test_gm_only_item_is_not_mislabeled_as_no_profession(self):
        groups = template_properties(self.record(3504))['requirements']
        self.assertEqual(groups[2]['allowed'], ['GM'])
        self.assertEqual(groups[2]['status'], 'limited')

    def test_signed_resistances_zero_and_hidden_native_sign_conversion(self):
        record = self.record()
        struct.pack_into('<6b', record, 186, -128, 127, 0, -1, 1, -12)
        struct.pack_into('<i', record, 472, -15)
        struct.pack_into('<h', record, 138, -3)
        struct.pack_into('<i', record, 476, 20)
        result = template_properties(record)
        self.assertEqual([v['value'] for v in result['resistances']], [-128, 127, 0, -1, 1, -12])
        hidden = {v['key']: v for v in result['hidden_attributes']}
        self.assertEqual(hidden['guard']['value'], 15)
        self.assertEqual(hidden['movement']['value'], -3)
        self.assertEqual(hidden['attack_speed']['value'], 20)
        struct.pack_into('<i', record, 472, -2147483648)
        self.assertEqual(template_properties(record)['hidden_attributes'][1]['value'], -2147483648)

    def test_instance_only_bonuses_are_unknown_not_fake_zero(self):
        result = template_properties(self.record())
        hidden = {v['key']: v for v in result['hidden_attributes']}
        for key in ('conversion', 'armor_break'):
            self.assertIsNone(hidden[key]['value'])
            self.assertEqual(hidden[key]['source'], 'instance_required')

    def test_non_item_and_truncated_record_are_explicit(self):
        record = self.record()
        struct.pack_into('<H', record, 8, 3)
        result = template_properties(record)
        self.assertEqual([v['status'] for v in result['requirements']], ['not_applicable']*3)
        self.assertTrue(all(v['value'] is None for v in result['resistances'] + result['hidden_attributes']))
        with self.assertRaises(ContentError):
            template_properties(bytes(record[:-1]))

    def test_detail_exposes_properties_even_when_record_is_not_editable(self):
        index, record = find_unique(self.loaded.client, 10)
        record = bytearray(record)
        struct.pack_into('<H', record, 60, 1)
        client = replace(self.loaded.client, records=self.loaded.client.records[:index] + (bytes(record),) +
                         self.loaded.client.records[index+1:])
        detail = item_detail(replace(self.loaded, client=client), 10)
        self.assertFalse(detail['editable'])
        self.assertIn('requirements', detail['template_properties'])

    def test_new_readonly_properties_cannot_enter_draft_and_unrelated_bytes_survive(self):
        before = item_detail(self.loaded, 3369)
        edit = {'source_id': 3369, 'target_id': 3369, 'name': before['name'],
                'description': before['description'], 'stats': {'guard': 10}}
        with self.assertRaises(ContentError):
            validate_edits(self.loaded, [edit])
        edit['stats'] = {'strength': 13}
        blobs, _ = prepare_edits(self.loaded, [edit])
        for key, table in (('client_object', self.loaded.client), ('server_object', self.loaded.server)):
            old = find_unique(table, 3369)[1]
            changed = find_unique(decode_table(blobs[key]), 3369)[1]
            self.assertEqual(template_properties(old), template_properties(changed))


if __name__ == '__main__':
    unittest.main()
