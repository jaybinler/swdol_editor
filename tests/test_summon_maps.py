"""NPC skill summon graph safety and shared map consumers on the verified game data."""

from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from editor_server import EditorState
from xyj_content.binary import ContentError
from xyj_content.editor import item_detail
from xyj_content.plot_maps import NATIVE_SHA256
from xyj_content.settings import default_config
from xyj_content.summon_maps import build_summon_map_index


from xyj_content.verification_inputs import paired_fixture


def npc(skills=()):
    """Build only the NPC skill slots needed by this pure index."""
    row = bytearray(832)
    struct.pack_into('<16h', row, 382, *([*skills] + [-1] * (16 - len(skills))))
    return bytes(row)


def skill(target=2, effect=18, flags=0x20, kind=3):
    """Build the native effect flag, dispatcher type and summon-template operand."""
    row = bytearray(832)
    struct.pack_into('<H', row, 8, kind)
    struct.pack_into('<I', row, 32, flags)
    struct.pack_into('<iii', row, 528, effect, target, 400)
    return bytes(row)


class SummonGraphTests(unittest.TestCase):
    def build(self, records, skills=None, maps=None, native=NATIVE_SHA256):
        return build_summon_map_index(records, {**records, **(skills or {})}, maps or {1:{18}}, native_sha256=native)

    def test_duplicate_skill_slots_yield_one_relation_without_mutating_inputs(self):
        records={1:npc((100,100)),2:npc()}; skills={100:skill()}; maps={1:{18}}
        before=deepcopy((records,skills,maps))
        result=self.build(records,skills,maps)
        self.assertEqual(len(result['relations']),1)
        row=result['relations'][0]
        self.assertEqual((row['template_id'],row['caster_template_id'],row['skill_id'],row['slots']), (2,1,100,[1,2]))
        self.assertEqual(row['path'],[1,2]); self.assertEqual(row['status'],'potential_skill_summon')
        self.assertEqual((records,skills,maps),before)

    def test_multiple_maps_and_cascading_summons_keep_each_root_path(self):
        result=self.build({1:npc((100,)),2:npc((101,)),3:npc()},
                          {100:skill(),101:skill(3)}, {1:{9,18}})
        self.assertEqual([(r['template_id'],r['map_id']) for r in result['relations']],[(2,9),(3,9),(2,18),(3,18)])
        self.assertEqual(result['relations'][1]['path'],[1,2,3])
        self.assertEqual([s['skill_id'] for s in result['relations'][1]['steps']],[100,101])

    def test_unknown_effect_inactive_non_skill_and_invalid_target_do_not_inherit_maps(self):
        result=self.build({1:npc((100,101,102,103,104)),2:npc()},
                          {100:skill(effect=19),101:skill(flags=0),102:skill(kind=8),103:skill(target=999)})
        self.assertEqual(result['relations'],[])
        self.assertEqual(result['diagnostics']['invalid_targets'],1)

    def test_unplaced_caster_and_orphan_cycle_do_not_establish_a_map(self):
        records={1:npc(),2:npc((100,)),3:npc((101,))}
        self.assertEqual(self.build(records,{100:skill(3),101:skill(2)})['relations'],[])

    def test_reachable_cycles_and_self_summons_terminate(self):
        result=self.build({1:npc((100,)),2:npc((101,102))}, {100:skill(2),101:skill(1),102:skill(2)})
        self.assertEqual([r['template_id'] for r in result['relations']],[2])
        self.assertEqual(result['diagnostics']['cycle_edges'],2)

    def test_unknown_native_version_is_disabled_and_wrong_width_fails_closed(self):
        for native in (None,'other-version'):
            result=self.build({1:npc((100,)),2:npc()},{100:skill()},native=native)
            self.assertFalse(result['available']);self.assertEqual(result['relations'],[])
        with self.assertRaises(ContentError):self.build({1:b'short'})

    def test_depth_and_state_limits_bound_graph_work(self):
        records={1:npc((100,)),2:npc((101,)),3:npc()};skills={100:skill(2),101:skill(3)}
        with patch('xyj_content.summon_maps.MAX_SUMMON_DEPTH',2):
            result=self.build(records,skills)
            self.assertEqual([r['template_id'] for r in result['relations']],[2])
            self.assertEqual(result['diagnostics']['depth_limited_edges'],1)
        with patch('xyj_content.summon_maps.MAX_SUMMON_STATES',1),self.assertRaises(ContentError):
            self.build(records,skills)


class RealSummonMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        baseline=paired_fixture(ROOT) / 'baseline/client/Main'
        cls.state=EditorState(ROOT,config={**default_config(),'npc_snapshot':'output/editor/npc-snapshot-script-v3-final',
            'client_object':str(baseline/'Object.dbf'),'language':str(baseline/'Language.DBF')})
        cls.catalog=cls.state.creatures()

    def test_confirmed_skill_links_molihai_to_spirit_on_map18_without_saved_instances(self):
        c=self.catalog;detail=c.detail(20355,'18')
        self.assertEqual(detail['instance_total'],0);self.assertEqual(detail['instances'],[])
        self.assertEqual([(m['id'],m['sources']) for m in detail['maps']],[(18,['summon'])])
        row=detail['maps'][0]['summon_sources'][0]
        self.assertEqual((row['caster_template_id'],row['caster_name'],row['skill_id'],row['skill_name']),
                         (20086,'魔礼海',11058,'元神出窍'))
        self.assertEqual(row['slots'],[1,11,12]);self.assertEqual(row['path'],[20086,20355])
        self.assertEqual(c.detail(20086)['maps'][0]['sources'],['saved'])
        self.assertEqual(c.listing('20355','unplaced')['total'],0)
        self.assertEqual(c.listing('20355','18',template_id=20355)['total'],1)
        self.assertEqual(c.metadata()['unplaced'],431)
        self.assertEqual(c.metadata()['summon_map_coverage']['newly_placed_templates'],7)
        self.assertEqual(c.metadata()['summon_map_coverage']['new_map_memberships'],30)

    def test_acquisition_and_drop_sources_share_summon_maps_and_reject_wrong_map(self):
        state=self.state;c=self.catalog
        source=next(row for row in state.acquisition().public()['monsters'] if row['id']==20355)
        self.assertEqual(source['maps'],[18]);self.assertEqual(source['summon_maps'],[18])
        edit={'source_id':10,'target_id':319,'name':'召唤测试剑','description':item_detail(state.loaded,10)['description'],
              'stats':{},'acquisition':{'drop':{'monster_id':20355,'map_id':18,'percent':5},'shops':[],'rewards':[]}}
        state.acquisition().validate([edit])
        edit['acquisition']['drop']['map_id']=9
        with self.assertRaises(ContentError):state.acquisition().validate([edit])
        # A reverse-source projection must preserve the same membership evidence.
        with patch.dict(c.item_sources,{319:[20355]}):
            self.assertEqual(c.drop_sources(319)['items'][0]['maps'],[{'id':18,'name':'通天柱','sources':['summon']}])

    def test_returned_evidence_and_metadata_cannot_pollute_cached_index(self):
        c=self.catalog;d=c.detail(20355)
        d['maps'][0]['summon_sources'][0]['steps'][0]['slots'].clear()
        c.metadata()['summon_map_coverage']['diagnostics']['bogus']=1
        self.assertEqual(c.detail(20355)['maps'][0]['summon_sources'][0]['slots'],[1,11,12])
        self.assertNotIn('bogus',c.metadata()['summon_map_coverage']['diagnostics'])
