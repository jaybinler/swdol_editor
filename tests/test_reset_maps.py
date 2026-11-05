"""Native reset-pool graph boundaries and shared real-data map consumers."""

from copy import deepcopy
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.request import urlopen
from unittest.mock import patch
import json
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from editor_server import EditorState, make_handler
from xyj_content.binary import ContentError, decode_table
from xyj_content.editor import export_editor_patch, item_detail
from xyj_content.objects import find_unique
from xyj_content.plot_maps import NATIVE_SHA256
from xyj_content.reset_maps import build_reset_map_index, reset_candidates
from xyj_content.settings import default_config


def record(tid, candidates=(-1, -1, -1, -1, -1)):
    """Create one isolated NPC record without interpreting unrelated fields."""
    row = bytearray(832)
    struct.pack_into('<I', row, 4, tid)
    struct.pack_into('<5h', row, 516, *candidates)
    return bytes(row)


def role(tid=1, zone=18, revive=18):
    """Provide the minimal already-validated map seed required by the pure graph builder."""
    return {'template_id': tid, 'map_id': zone, 'revive_map_id': revive}


class ResetMapGraphTests(unittest.TestCase):
    def build(self, records, roles=None, scripts=None):
        return build_reset_map_index(records, roles or {'npcpool:1': role()}, scripts or {}, native_sha256=NATIVE_SHA256)

    def test_minus_one_is_self_and_duplicate_slots_remain_visible_without_duplicate_maps(self):
        records = {1:record(1,(-1,2,2,0,9999)), 2:record(2,(1,3,-1,-1,-1)), 3:record(3)}
        before = deepcopy(records)
        rows = reset_candidates(1, records)
        self.assertEqual((rows[0]['id'], rows[0]['is_self']), (1,True))
        self.assertFalse(rows[3]['valid']); self.assertFalse(rows[4]['valid'])
        result = self.build(records)
        self.assertEqual([r['template_id'] for r in result['relations']], [2,3])
        self.assertEqual(result['relations'][0]['slots'], [2,3])
        self.assertEqual(result['relations'][1]['path'], [1,2,3])
        self.assertEqual(result['diagnostics']['invalid_slots'], 2)
        self.assertGreater(result['diagnostics']['cycle_edges'], 0)
        self.assertEqual(records,before)

    def test_two_maps_keep_independent_roots_and_unreachable_cycles_stay_unplaced(self):
        records = {1:record(1,(2,-1,-1,-1,-1)),2:record(2),4:record(4,(5,-1,-1,-1,-1)),5:record(5,(4,-1,-1,-1,-1))}
        result=self.build(records,{'npcpool:1':role(), 'npcpool:2':role(zone=23,revive=23)})
        self.assertEqual([(r['template_id'],r['map_id']) for r in result['relations']],[(2,18),(2,23)])
        self.assertEqual(len({(r['template_id'],r['map_id']) for r in result['relations']}),2)

    def test_cross_map_unknown_and_invalid_revive_seeds_do_not_inherit_a_map(self):
        records={1:record(1,(2,-1,-1,-1,-1)),2:record(2)}
        roles={'npcpool:1':role(revive=23), 'npcpool:2':role(revive=-1), 'npcpool:3':role(revive=True),
               'npcpool:4':role(zone=-1,revive=-1), 'npcpool:5':role(zone=23,revive=23)}
        result=self.build(records,roles)
        self.assertEqual([(r['template_id'],r['map_id']) for r in result['relations']],[(2,23)])
        self.assertEqual(result['diagnostics']['cross_map_or_unknown_revive_instances'],3)

    def test_verified_script_spawn_can_seed_potential_reset_relations(self):
        records={1:record(1,(2,-1,-1,-1,-1)),2:record(2),3:record(3)}
        scripts={1:[{'template_id':1,'map_id':23,'root_key':'npcpool:1','plot_id':5453,'file':'TALK0027.EXE','offset':1000}]}
        result=self.build(records,{'npcpool:1':role(tid=3,zone=23,revive=18)},scripts)
        self.assertEqual(result['relations'][0]['root']['kind'],'script')
        self.assertEqual(result['relations'][0]['map_id'],23)
        self.assertEqual(result['relations'][0]['status'],'potential_reset')

    def test_unverified_native_version_does_not_interpret_opaque_template_bytes(self):
        records={1:record(1,(2,-1,-1,-1,-1)),2:record(2)}
        for version in (None,'wrong-native'):
            result=build_reset_map_index(records,{'npcpool:1':role()},{},native_sha256=version)
            self.assertFalse(result['available']); self.assertEqual(result['relations'],[])
        with self.assertRaises(ContentError): reset_candidates(3,records)
        with self.assertRaises(ContentError): reset_candidates(1,{1:b'short'})

    def test_depth_and_state_limits_bound_long_chains_without_inventing_relations(self):
        records={tid:record(tid,(tid+1,-1,-1,-1,-1)) for tid in range(1,6)}
        records[6]=record(6)
        with patch('xyj_content.reset_maps.MAX_RESET_DEPTH',2):
            result=self.build(records)
            self.assertEqual([r['template_id'] for r in result['relations']],[2])
            self.assertEqual(result['diagnostics']['depth_limited_edges'],1)
        with patch('xyj_content.reset_maps.MAX_RESET_STATES',2),self.assertRaises(ContentError):
            self.build(records)


class RealResetMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        baseline=ROOT/'output/editor/patches/20261104-165921-7d73b3ee/baseline/client/Main'
        cls.state=EditorState(ROOT,config={**default_config(), 'npc_snapshot':'output/editor/npc-snapshot-script-v3-final',
             'client_object':str(baseline/'Object.dbf'),'language':str(baseline/'Language.DBF')})
        cls.catalog=cls.state.creatures()

    def test_user_confirmed_variants_have_map18_witnesses_and_zero_direct_instances(self):
        c=self.catalog
        self.assertEqual(c.detail(20009)['instance_total'],29)
        for tid,slot in ((20088,2),(20089,3)):
            detail=c.detail(tid,'18')
            self.assertEqual(detail['instance_total'],0); self.assertEqual(detail['instances'],[])
            self.assertEqual([m['id'] for m in detail['maps']],[18])
            self.assertEqual(detail['maps'][0]['sources'],['reset'])
            witness=detail['maps'][0]['reset_sources'][0]
            self.assertEqual(witness['path'],[20009,tid]); self.assertEqual(witness['slots'],[slot])
            self.assertEqual(c.roles[witness['root']['root_key']]['revive_map_id'],18)
            self.assertEqual(c.listing(str(tid),'unplaced')['total'],0)
            self.assertEqual(c.listing(str(tid),'18',template_id=tid)['total'],1)

    def test_all_shared_consumers_include_the_same_reset_maps(self):
        c=self.catalog
        self.assertEqual(c.metadata()['unplaced'],438-c.metadata()['summon_map_coverage']['newly_placed_templates'])
        self.assertEqual(c.metadata()['reset_map_coverage']['newly_placed_templates'],326)
        self.assertEqual(c.metadata()['instances'],10429)
        source=next(r for r in c.drop_sources(3616)['items'] if r['id']==20088)
        self.assertEqual(source['maps'],[{'id':18,'name':'通天柱','sources':['reset']}])
        monster=next(r for r in self.state.acquisition().public()['monsters'] if r['id']==20088)
        self.assertEqual(monster['maps'],[18]); self.assertEqual(monster['reset_maps'],[18])
        zone=next(r for r in c.metadata()['maps'] if r['id']==18)
        self.assertEqual(zone['templates'],c.listing(map_id='18')['total'])
        # A pre-existing script-only source is retained alongside the new index.
        self.assertEqual(c.detail(20799)['maps'][0]['sources'],['script'])

    def test_candidate_slots_and_returned_witnesses_cannot_pollute_cached_state(self):
        detail=self.catalog.detail(20009)
        self.assertEqual([r['raw_id'] for r in detail['reset_candidates']],[-1,20088,20089,20184,20342])
        variant=self.catalog.detail(20088)
        self.assertEqual(variant['reset_candidates'][2]['id'],20088)
        variant['maps'][0]['reset_sources'][0]['path'].clear()
        self.assertEqual(self.catalog.detail(20088)['maps'][0]['reset_sources'][0]['path'],[20009,20088])

    def test_new_item_can_target_a_reset_variant_and_export_preserves_the_pool(self):
        item=item_detail(self.state.loaded,10)
        edit={'source_id':10,'target_id':319,'name':'刷新测试剑','description':item['description'],'stats':{},
              'acquisition':{'drop':{'monster_id':20088,'map_id':18,'percent':5},'shops':[],'rewards':[]}}
        self.state.acquisition().validate([edit])
        with tempfile.TemporaryDirectory(dir=ROOT/'output') as tmp:
            exported=export_editor_patch(Path(tmp),self.state.loaded,[edit],acquisition=self.state.acquisition())
            table=decode_table((Path(exported['path'])/'server/qingfeng/Object.dbf').read_bytes())
            self.assertEqual(find_unique(table,20088)[1][516:526],find_unique(self.state.loaded.server,20088)[1][516:526])

    def test_http_routes_share_the_index_without_mutating_the_draft(self):
        state=self.state; before=(state.revision,deepcopy(state.edits))
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(state))
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            for path in ('/api/creatures/20088?map=18','/api/creatures?template_id=20088&map=18',
                         '/api/item/3616/drop-sources','/api/acquisition/catalog'):
                with urlopen(base+path) as response:data=json.load(response)
                self.assertIsInstance(data,dict)
            self.assertEqual((state.revision,state.edits),before)
        finally:
            server.shutdown();server.server_close();thread.join()
