"""Synthetic compiled scripts, graph boundaries and real script-generated map regression."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import hashlib
import json
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from editor_server import EditorState
from xyj_content.binary import ContentError
from xyj_content.creatures import CreatureCatalog
from xyj_content.plot_maps import decode_script, trace_plot, build_script_index, validate_script_index


def script_blob(functions, *, mz=False):
    """Encode a bounded numbered test script; fixture instructions are never executed."""
    data = bytearray(8 + len(functions) * 4)
    struct.pack_into('<II', data, 0, 5400, len(functions))
    for index, words in enumerate(functions):
        start = len(data)
        struct.pack_into('<I', data, 8 + index * 4, start)
        data += struct.pack('<II', start, start + 8 + (len(words) - 1) * 4)
        data += struct.pack('<' + 'i' * len(words), *words)
    header = bytearray(512)
    if mz:
        header[:2] = b'MZ'
        size = len(header) + len(data)
        struct.pack_into('<HH', header, 2, size % 512, (size + 511) // 512)
        struct.pack_into('<H', header, 8, 32)
    return bytes(header + data)


def spawn(template=20799, hook=-1, count=1):
    """Return the native 21-word random-create form with one explicit optional hook."""
    return [821, 800, template, hook, *([-1] * 13), 0, count, 400, 0]


class PlotDecoderTests(unittest.TestCase):
    def test_both_wrappers_and_truncated_or_overlapping_headers(self):
        for mz in (False, True):
            blob = script_blob([spawn() + [0]], mz=mz)
            program = decode_script(blob, 'TALK0027.EXE')[5400]
            rows, _, stops = trace_plot(program)
            self.assertEqual(rows[0]['template_id'], 20799)
            self.assertEqual(stops, {})
            with self.assertRaises(ContentError):
                decode_script(blob[:518], 'TALK0027.EXE')
        blob = bytearray(script_blob([[0], [0]]))
        struct.pack_into('<I', blob, 512 + 12, struct.unpack_from('<I', blob, 512 + 8)[0])
        with self.assertRaises(ContentError):
            decode_script(bytes(blob), 'TALK0027.EXE')
        with self.assertRaises(ContentError):
            decode_script(script_blob([[0]]), '../../TALK0027.EXE')

    def test_create_id_in_operands_or_unsupported_data_is_not_scanned(self):
        # A complete create record inside a menu's unparsed arguments is not an instruction.
        for words in ([400, 821, 0], [402, *spawn(), 0], [13, *spawn(), 0], [116, *spawn(), 0]):
            rows, _, _ = trace_plot(decode_script(script_blob([words]), 'TALK0027.EXE')[5400])
            self.assertEqual(rows, [])
        rows, _, _ = trace_plot(decode_script(script_blob([spawn(count=0) + [0]]), 'TALK0027.EXE')[5400])
        self.assertEqual(rows, [])

    def test_fixed_create_skips_equipment_words_that_look_like_instructions(self):
        words = [904, 20000, 0, 0, 0, 0, *([-1] * 4), *([-1] * 10), 0, 1, 0, 0]
        words[10:14] = [821, 800, 20799, 5454]
        rows, _, _ = trace_plot(decode_script(script_blob([words + [0]]), 'TALK0027.EXE')[5400])
        self.assertEqual([row['template_id'] for row in rows], [20000])

    def test_jumps_are_bounded_and_loops_terminate(self):
        blob = script_blob([[100, 12, *spawn(), 0]])  # body starts at 20, target is the function header.
        rows, _, diagnostics = trace_plot(decode_script(blob, 'TALK0027.EXE')[5400])
        self.assertEqual(rows, [])
        self.assertIn('ambiguous_boundary', diagnostics)
        program = decode_script(script_blob([[100, 20, 0]]), 'TALK0027.EXE')[5400]
        self.assertEqual(trace_plot(program)[0], [])

    def test_generated_hooks_and_two_maps_form_a_bounded_deduplicated_graph(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'output') as tmp:
            directory = Path(tmp)
            (directory / 'ASM').mkdir()
            native = b'synthetic-native'
            (directory / 'NewLocalServer.exe').write_bytes(native)
            (directory / 'ASM/TALK0027.EXE').write_bytes(script_blob([
                spawn(hook=5401) + [0], [261, 1, 5400, 0]]))
            records = []
            for tid in (20739, 20799):
                row = bytearray(832); struct.pack_into('<I', row, 4, tid); records.append(bytes(row))
            roots = [{'map_id': zone, 'key': f'npcpool:{index}', 'plots': [5400, -1, -1, -1]}
                     for index, zone in enumerate((23, 23, 51))]
            with patch('xyj_content.plot_maps.NATIVE_SHA256', hashlib.sha256(native).hexdigest()):
                result, _ = build_script_index(roots, tuple(records), directory)
            self.assertEqual({r['map_id'] for r in result['relations']}, {23, 51})
            self.assertEqual(len(result['relations']), 2)
            self.assertEqual(result['visited_contexts'], 4)
            (directory / 'NewLocalServer.exe').write_bytes(b'changed')
            with self.assertRaisesRegex(ContentError, '版本未核验'):
                build_script_index(roots, tuple(records), directory)


@unittest.skipUnless((ROOT / 'output/editor/npc-snapshot-script-v3/manifest.json').is_file(), '真实 v3 快照未提供')
class RealScriptMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = EditorState(ROOT, ROOT / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf')
        cls.catalog = CreatureCatalog(ROOT, state.loaded, directory=ROOT / 'output/editor/npc-snapshot-script-v3')

    def test_20799_is_in_element_city_with_source_and_without_fake_saved_instances(self):
        c = self.catalog
        detail = c.detail(20799, '23')
        self.assertEqual(detail['instance_total'], 0)
        self.assertEqual(detail['instances'], [])
        self.assertEqual([m['id'] for m in detail['maps']], [23])
        self.assertEqual(detail['maps'][0]['sources'], ['script'])
        source = detail['maps'][0]['script_sources'][0]
        self.assertEqual((source['plot_id'], source['opcode']), (5453, 821))
        self.assertEqual(c.roles[source['root_key']]['template_id'], 20739)
        self.assertIn(20799, [r['id'] for r in c.listing('20799', '23')['items']])
        self.assertEqual(c.listing('20799', 'unplaced')['total'], 0)
        self.assertEqual(c.detail(20739)['maps'][0]['id'], 23)
        zone = next(m for m in c.metadata()['maps'] if m['id'] == 23)
        self.assertEqual(zone['templates'], c.listing(map_id='23')['total'])

    def test_all_memberships_are_unique_and_match_filters_and_totals(self):
        c = self.catalog
        for entry in c.entries:
            detail = c.detail(entry['id'])
            ids = [m['id'] for m in detail['maps']]
            self.assertEqual(ids, entry['maps'])
            self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(c.metadata()['unplaced'], c.listing(map_id='unplaced')['total'])
        self.assertEqual(c.metadata()['instances'], 10429)

    def test_forged_relation_roots_and_paths_fail_closed(self):
        directory = ROOT / 'output/editor/npc-snapshot-script-v3'
        manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
        value = json.loads((directory / manifest['catalog_file']).read_text('utf-8'))['script_index']
        for field, bad in (('root_key', 'npcpool:99999999'), ('map_id', 999999), ('file', '../../TALK0027.EXE'), ('opcode', 1)):
            altered = deepcopy(value); altered['relations'][0][field] = bad
            with self.subTest(field=field), self.assertRaises(ContentError):
                validate_script_index(altered, self.catalog.roles, self.catalog.records)
