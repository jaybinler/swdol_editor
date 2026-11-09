"""Full native roundtrips, authoring-rig baking and RGB555/alpha boundaries."""

from copy import deepcopy
from pathlib import Path
import hashlib
import struct
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from xyj_content import native_mesh as mesh,native_texture as tex
from xyj_content.binary import ContentError,decompress


class NativeModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blob=(ROOT/'swdol/MODEL/CHR00239.PLY').read_bytes()
        cls.native=mesh.decode(cls.blob)
        cls.doc=mesh.document(cls.native,239,hashlib.sha256(cls.blob).hexdigest())

    def test_complete_matrix_and_morph_roundtrip_is_byte_exact_after_decompression(self):
        for name in ('CHR00239.PLY','CHR00365.PLY','CHR00887.PLY','CHR00001.PLY'):
            with self.subTest(name=name):
                blob=(ROOT/'swdol/MODEL'/name).read_bytes();native=mesh.decode(blob)
                encoded=mesh.encode(native)
                self.assertEqual(decompress(blob[8:],struct.unpack_from('<I',blob)[0]),decompress(encoded[8:],struct.unpack_from('<I',encoded)[0]))
                self.assertEqual(native['tail'],b'')

    def test_default_document_preserves_every_native_pose_position(self):
        mesh.validate_document(self.doc,self.native)
        for frame in (0,1,len(self.native['poses'])-1):
            points=mesh.posed_positions(self.doc,self.native,frame)
            for v in self.doc['vertices']:
                expected=mesh.transform(self.native['poses'][frame][v['group']],mesh.vertex_at(self.native,frame,v['group'],v['origin'])[:3])
                for actual,wanted in zip(points[v['id']],expected):self.assertAlmostEqual(actual,wanted,places=4)

    def test_weighted_rig_and_topology_bake_into_native_morph_records(self):
        doc=deepcopy(self.doc)
        doc['bones'][0]['translation']=[2,0,0]
        v=deepcopy(doc['vertices'][0]);v.update(id='new',origin=None,position=[x+1 for x in v['position']]);doc['vertices'].append(v)
        face=deepcopy(doc['faces'][0]);face.update(id='newface',vertices=['new',*face['vertices'][1:]]);doc['faces'].append(face)
        encoded=mesh.bake(doc,self.native);native=mesh.decode(encoded)
        self.assertEqual(len(native['vertices']),len(doc['vertices']))
        self.assertEqual(len(native['poses']),len(self.native['poses']))
        ordered=sorted(doc['vertices'],key=lambda v:v['group'])
        for frame in (0,4):
            points=mesh.posed_positions(doc,self.native,frame)
            for index,v in enumerate(ordered):
                actual=mesh.transform(native['poses'][frame][v['group']],mesh.vertex_at(native,frame,v['group'],index)[:3])
                for a,b in zip(actual,points[v['id']]):self.assertAlmostEqual(a,b,places=4)

    def test_bad_topology_cycles_and_invalid_weights_are_rejected(self):
        for change in ('cycle','weight','face','number'):
            with self.subTest(change=change),self.assertRaises(ContentError):
                doc=deepcopy(self.doc)
                if change=='cycle':doc['bones'][0]['parent']=doc['bones'][0]['id']
                if change=='weight':doc['vertices'][0]['weights'][0]['value']=.5
                if change=='face':doc['faces'][0]['vertices'][0]='missing'
                if change=='number':doc['vertices'][0]['position'][0]=float('nan')
                mesh.bake(doc,self.native)

    def test_added_bone_inherits_parent_native_animation_with_mixed_weights(self):
        doc=deepcopy(self.doc);parent=doc['bones'][10]
        child=deepcopy(parent);child.update(id='child',parent=parent['id'],source_group=-1)
        doc['bones'].append(child)
        doc['vertices'][0]['weights']=[{'bone':parent['id'],'value':.75},{'bone':'child','value':.25}]
        matrices=mesh.skin_matrices(doc,self.native,5)
        for a,b in zip(matrices['child'],matrices[parent['id']]):self.assertAlmostEqual(a,b,places=12)
        expected=mesh.transform(matrices[parent['id']],doc['vertices'][0]['position'])
        actual=mesh.posed_positions(doc,self.native,5)[doc['vertices'][0]['id']]
        for a,b in zip(actual,expected):self.assertAlmostEqual(a,b,places=5)

    def test_texture_roundtrip_and_final_alpha_quantization(self):
        pixels=bytes([255,120,0,255,10,240,32,127,100,10,240,128])
        blob=tex.encode(3,1,pixels,[.5,1,1]);width,height,actual=tex.rgba(blob)
        self.assertEqual((width,height),(3,1));self.assertEqual(actual[3::4],bytes([255,0,255]))
        self.assertEqual(actual[0],16*255//31);self.assertTrue(tex.png(blob).startswith(b'\x89PNG'))
        with self.assertRaises(ContentError):tex.encode(2049,1,b'')
        with self.assertRaises(ContentError):tex.encode(3,1,pixels,[float('nan'),1,1])
