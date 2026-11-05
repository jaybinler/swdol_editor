"""Corruption, clone integrity, publication boundaries and real-file regression checks."""

from dataclasses import replace
from pathlib import Path
import json
import hashlib
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "tools"))

from xyj_content.binary import (ContentError, FixedTable, MAX_RAW_BYTES, decode_table,
                                encode_table, read_bytes)
from xyj_content.catalog import Inputs, csv_cell, inspect_loaded, load_inputs
from xyj_content.cli import audit_originals
from xyj_content.files import output_path, read_json, sha256, write_json
from xyj_content.language import LanguageTable, decode_language, encode_language, transform
from xyj_content.objects import clone_record, encode_fixed_text, find_unique, view_record
from xyj_content.patch import CloneSpec, PAYLOAD_PATHS, build_patch, verify_patch


def sample_record(object_id: int, type_code: int, name: str, description: str) -> bytes:
    """Make a complete fixture with distinctive unknown bytes that must survive cloning."""
    raw = bytearray(832)
    struct.pack_into("<IHH", raw, 4, object_id, type_code, 0x5251)
    struct.pack_into("<H", raw, 28, 219)
    raw[80:84] = b"STAT"
    raw[800:] = bytes(range(32))
    raw[12:28] = encode_fixed_text(name, 16)
    raw[600:800] = encode_fixed_text(description, 200)
    return bytes(raw)


class ContainerTests(unittest.TestCase):
    def test_dbf_roundtrip_preserves_metadata_and_unknown_record_bytes(self):
        for flag in (0, 1):
            table = FixedTable(flag, b"Object".ljust(16, b"\0"), 832,
                               (sample_record(10, 2, "桃木剑", "说明"),))
            self.assertEqual(decode_table(encode_table(table)), table)

    def test_truncated_payload_rejected(self):
        blob = encode_table(FixedTable(1, bytes(16), 4, (b"TEST",)))
        with self.assertRaisesRegex(ContentError, "压缩长度"):
            decode_table(blob[:-1])

    def test_declared_decompression_bomb_rejected_before_decode(self):
        blob = struct.pack("<4sIII16s", b"DBF\0", MAX_RAW_BYTES + 1, 3, 1, bytes(16)) + b"abc"
        with self.assertRaisesRegex(ContentError, "解压长度"):
            decode_table(blob)

    def test_map_cannot_be_misread_as_single_fixed_table(self):
        import lzokay
        raw = struct.pack("<II", 156, 40) + b"map-data"
        packed = lzokay.compress(raw)
        blob = struct.pack("<4sIII16s", b"DBF\0", len(raw), len(packed), 1, bytes(16)) + packed
        with self.assertRaisesRegex(ContentError, "固定记录表"):
            decode_table(blob)

    def test_damaged_lzo_stream_rejected(self):
        blob = struct.pack("<4sIII16s", b"DBF\0", 8, 3, 1, bytes(16)) + b"bad"
        with self.assertRaisesRegex(ContentError, "LZO"):
            decode_table(blob)

    def test_wrong_magic_rejected(self):
        with self.assertRaises(ContentError):
            decode_table(bytes(32))

    def test_fixed_text_counts_bytes_not_characters(self):
        self.assertEqual(len(encode_fixed_text("一" * 7 + "A", 16)), 16)
        with self.assertRaisesRegex(ContentError, "字节限制"):
            encode_fixed_text("一" * 8, 16)

    def test_unsupported_unicode_or_embedded_null_rejected(self):
        for value in ("剑😀", "剑\0名"):
            with self.assertRaises(ContentError):
                encode_fixed_text(value, 16)


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.table = LanguageTable(136, 0x75, 2, (b"\0", "桃木剑".encode("gbk") + b"\0"))

    def test_roundtrip_preserves_original_entries_and_parameters(self):
        self.assertEqual(decode_language(encode_language(self.table)), self.table)

    def test_observed_transform_prefix_independent_golden_bytes(self):
        encoded = bytes.fromhex("c1 ab 57 ae 1c ba ea d5")
        self.assertEqual(transform(encoded, 0x75, 2, encode=False), struct.pack("<II", 5, 5))

    def test_append_preserves_indices(self):
        updated = self.table.append_texts("测试剑", "测试说明")
        self.assertEqual(updated.entries[:2], self.table.entries)
        self.assertEqual(updated.text(2), "测试剑")

    def test_out_of_bounds_index_rejected(self):
        for index in (-1, 2):
            with self.assertRaises(ContentError):
                self.table.text(index)

    def test_invalid_rotation_rejected(self):
        for phase in (0, 8, 255):
            with self.assertRaises(ContentError):
                transform(b"test", 0x75, phase, encode=False)

    def test_length_table_overrun_or_extra_bytes_rejected(self):
        import lzokay
        for raw in (struct.pack("<I", 99) + b"A\0", struct.pack("<I", 1) + b"\0EXTRA"):
            encoded = transform(raw, 0x75, 2, encode=True) + b"\x75\x02"
            packed = lzokay.compress(encoded)
            blob = struct.pack("<4s5I", b"RTS\0", 1, len(encoded), len(packed), 136, 1) + packed
            with self.assertRaises(ContentError):
                decode_language(blob)


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="xyj-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.inputs = Inputs.from_root(self.root)
        self.language = LanguageTable(136, 0x75, 2, (
            "金钱".encode("gbk") + b"\0", "桃木剑".encode("gbk") + b"\0", "说明".encode("gbk") + b"\0"))
        server = sample_record(10, 2, "桃木剑", "说明")
        client = clone_record(server, 10, "桃木剑", "说明", (1, 2))
        zero_server = sample_record(0, 1, "金钱", "金钱")
        zero_client = clone_record(zero_server, 1, "金钱", "金钱", (0, 0))
        zero_client = bytearray(zero_client)
        struct.pack_into("<I", zero_client, 4, 0)
        st = FixedTable(1, b"Object".ljust(16, b"\0"), 832, (server, zero_server, zero_server))
        ct = replace(st, records=(client, bytes(zero_client), bytes(zero_client)))
        for path, blob in ((self.inputs.server, encode_table(st)), (self.inputs.client, encode_table(ct)),
                           (self.inputs.language, encode_language(self.language))):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        self.loaded = load_inputs(self.inputs)
        self.spec = CloneSpec.from_dict({"version": 1, "template_id": 10, "new_id": 318,
                                        "name": "测试桃木剑", "description": "开发测试说明"})
        self.destination = Path("output/test-patch")

    def test_build_and_reopen_preserves_originals_and_unknown_bytes(self):
        before = {p: p.read_bytes() for p in (self.inputs.server, self.inputs.client, self.inputs.language)}
        result = build_patch(self.root, self.destination, self.loaded, self.spec)
        self.assertTrue(result["all_existing_records_preserved"])
        self.assertFalse(result["gameplay_verified"])
        self.assertEqual(verify_patch(self.root, self.destination), result)
        for path, data in before.items():
            self.assertEqual(path.read_bytes(), data)
        table = decode_table(read_bytes(self.root / self.destination / PAYLOAD_PATHS["server_object"]))
        _, record = find_unique(table, 318)
        self.assertEqual(record[28:600], self.loaded.server.records[0][28:600])
        self.assertEqual(record[800:], self.loaded.server.records[0][800:])

    def test_duplicate_id_zero_is_preserved_and_never_selected(self):
        summary, server, client = inspect_loaded(self.loaded)
        self.assertEqual(len(summary["server_duplicate_ids"]["0"]), 2)
        self.assertEqual(len(server), 3)
        self.assertEqual(len(client), 3)
        with self.assertRaises(ContentError):
            find_unique(self.loaded.server, 0)

    def test_collision_rejected_without_output(self):
        with self.assertRaisesRegex(ContentError, "已存在"):
            build_patch(self.root, self.destination, self.loaded, replace(self.spec, new_id=10))
        self.assertFalse((self.root / self.destination).exists())

    def test_template_stats_disagreement_rejected(self):
        records = list(self.loaded.client.records)
        row = bytearray(records[0]); row[80] ^= 1; records[0] = bytes(row)
        loaded = replace(self.loaded, client=replace(self.loaded.client, records=tuple(records)))
        with self.assertRaisesRegex(ContentError, "不一致"):
            build_patch(self.root, self.destination, loaded, self.spec)

    def test_stale_input_hash_rejected(self):
        build_patch(self.root, self.destination, self.loaded, self.spec)
        self.inputs.server.write_bytes(self.inputs.server.read_bytes() + b"changed")
        with self.assertRaisesRegex(ContentError, "源文件已变化"):
            verify_patch(self.root, self.destination)

    def test_payload_tampering_rejected(self):
        build_patch(self.root, self.destination, self.loaded, self.spec)
        path = self.root / self.destination / PAYLOAD_PATHS["client_object"]
        path.write_bytes(path.read_bytes()[:-1])
        with self.assertRaisesRegex(ContentError, "校验失败"):
            verify_patch(self.root, self.destination)

    def test_changed_stats_rejected_even_if_manifest_hash_is_updated(self):
        build_patch(self.root, self.destination, self.loaded, self.spec)
        directory = self.root / self.destination
        path = directory / PAYLOAD_PATHS["server_object"]
        table = decode_table(read_bytes(path)); records = list(table.records)
        row = bytearray(records[0]); row[80] ^= 1; records[0] = bytes(row)
        blob = encode_table(replace(table, records=tuple(records))); path.write_bytes(blob)
        manifest = read_json(directory / "manifest.json")
        manifest["files"]["server_object"].update(sha256=sha256(blob), bytes=len(blob))
        write_json(directory / "manifest.json", manifest)
        with self.assertRaisesRegex(ContentError, "未批准"):
            verify_patch(self.root, self.destination)

    def test_manifest_path_traversal_rejected(self):
        build_patch(self.root, self.destination, self.loaded, self.spec)
        path = self.root / self.destination / "manifest.json"
        manifest = read_json(path); manifest["files"]["server_object"]["path"] = "../../Object.dbf"
        write_json(path, manifest)
        with self.assertRaisesRegex(ContentError, "路径"):
            verify_patch(self.root, self.destination)

    def test_original_directories_or_escape_cannot_be_output(self):
        for path in (Path("swdol/Main"), Path("output"), Path("output/../../escape")):
            with self.assertRaisesRegex(ContentError, "输出"):
                output_path(self.root, path)

    def test_existing_patch_is_never_overwritten(self):
        build_patch(self.root, self.destination, self.loaded, self.spec)
        with self.assertRaisesRegex(ContentError, "已存在"):
            build_patch(self.root, self.destination, self.loaded, self.spec)

    def test_failed_verification_does_not_publish_partial_patch(self):
        with patch("xyj_content.patch.verify_patch", side_effect=ContentError("verification failed")):
            with self.assertRaises(ContentError):
                build_patch(self.root, self.destination, self.loaded, self.spec)
        self.assertFalse((self.root / self.destination).exists())
        self.assertEqual(list((self.root / "output").glob(".xyj-build-*")), [])

    def test_invalid_spec_types_and_unknown_edits_are_rejected(self):
        for change in ({"new_id": True}, {"new_id": 32768}, {"damage": 999}, {"name": ""}):
            value = self.spec.to_dict() | change
            with self.assertRaises(ContentError):
                CloneSpec.from_dict(value)

    def test_auditor_detects_source_change_and_ignores_outputs(self):
        files = [{"path": p.relative_to(self.root).as_posix(), "bytes": p.stat().st_size,
                  "sha256": sha256(p.read_bytes())}
                 for p in (self.inputs.server, self.inputs.client, self.inputs.language)]
        inventory = self.root / "baseline.json"
        write_json(inventory, {"scope": ["XYJ_SEVVER", "swdol"], "files": files})
        self.assertTrue(audit_originals(self.root, inventory)["all_original_sha256_unchanged"])
        build_patch(self.root, self.destination, self.loaded, self.spec)
        self.assertEqual(audit_originals(self.root, inventory)["added_in_original_directories"], [])
        self.inputs.language.write_bytes(b"changed")
        self.assertFalse(audit_originals(self.root, inventory)["all_original_sha256_unchanged"])

    def test_csv_formula_prefixes_are_escaped(self):
        for value in ("=cmd()", "  @SUM(A1)", "-1+2", "+1"):
            self.assertTrue(csv_cell(value).startswith("'"))
        self.assertEqual(csv_cell("桃木剑"), "桃木剑")

    def test_remote_preflight_accepts_matching_files_and_detects_later_change(self):
        from check_server_patch import check_server
        from snapshot_server import STATIC_FILES
        build_patch(self.root, self.destination, self.loaded, self.spec)
        share = Path(r"\\192.0.2.60\xyj")
        remote = {name: b"static-file" for name in STATIC_FILES}
        remote["qingfeng/Object.dbf"] = self.loaded.blobs[0]
        snapshot = self.root / "evidence/snapshot.json"
        write_json(snapshot, {"kind": "read_only_static_file_snapshot", "share": str(share),
                              "files": [{"path": k, "sha256": sha256(v)} for k, v in remote.items()]})
        with patch("check_server_patch.read_bytes", side_effect=lambda p: remote[p.relative_to(share).as_posix()]):
            self.assertTrue(check_server(self.root, share, self.destination, snapshot)["remote_static_baseline_matches"])
            remote["qingfeng/Object.dbf"] += b"changed"
            self.assertFalse(check_server(self.root, share, self.destination, snapshot)["remote_static_baseline_matches"])

    def test_remote_snapshot_never_persists_credentials_or_live_database_files(self):
        from snapshot_server import CONFIG_KEYS, STATIC_FILES, snapshot
        share = Path(r"\\192.0.2.60\xyj")
        remote = {name: b"static-file" for name in STATIC_FILES}
        remote["qingfeng/Object.dbf"] = self.loaded.blobs[0]
        remote["GMTools/Object.dbf"] = self.loaded.blobs[0]
        for name in CONFIG_KEYS:
            remote[name] = b"Password=DO_NOT_STORE\nLanIP=192.0.2.60\nGameDB=swdol\n"
        original_reader = read_bytes

        def fake_read(path):
            if path.is_relative_to(share):
                return remote[path.relative_to(share).as_posix()]
            return original_reader(path)

        with patch("snapshot_server.read_bytes", side_effect=fake_read):
            result = snapshot(self.root, share, Path("output/remote-snapshot"))
        self.assertFalse(result["remote_files_written"])
        self.assertFalse(result["database_files_read"])
        self.assertNotIn("DO_NOT_STORE", json.dumps(result))
        self.assertFalse(any(p.suffix.lower() in (".myd", ".myi", ".mdf", ".ldf")
                             for p in (self.root / "output/remote-snapshot").rglob("*")))


@unittest.skipUnless((PROJECT / "swdol/Main/Language.DBF").exists(), "真实游戏文件未提供")
class RealFileTests(unittest.TestCase):
    def test_known_real_language_and_template_alignment(self):
        paths = Inputs.from_root(PROJECT)
        baseline = PROJECT / "output/release-review-318/rollback/client/Main"
        if (baseline / "Object.dbf").exists() and (baseline / "Language.DBF").exists():
            paths = replace(paths, client=baseline / "Object.dbf", language=baseline / "Language.DBF")
            # The user updated the live local server. Pin this historical comparison to
            # the captured copy with the original server hash, keeping its assertions intact.
            historical = PROJECT / 'output/remote-server-20261029/server/GMTools/Object.dbf'
            if historical.exists():
                self.assertEqual(hashlib.sha256(historical.read_bytes()).hexdigest(),
                                 'c882357212108e047f9fed8b2e17aece4d366f22cda0042b3ad8f8550e7bff1c')
                paths = replace(paths, server=historical)
        loaded = load_inputs(paths)
        self.assertEqual(len(loaded.language.entries), 15219)
        self.assertEqual(loaded.language.text(10818), "桃木剑")
        self.assertEqual(loaded.language.text(1103), "鬼差")
        self.assertEqual(loaded.language.text(15217), "吸星曳影")
        si, sr = find_unique(loaded.server, 10)
        ci, cr = find_unique(loaded.client, 10)
        self.assertEqual(view_record(sr, si).name, view_record(cr, ci, loaded.language).name)
        self.assertEqual(decode_language(encode_language(loaded.language)), loaded.language)
        summary, _, _ = inspect_loaded(loaded)
        self.assertEqual(summary["name_difference_ids"], [])
        self.assertEqual(summary["client_only_ids"], [317])
        self.assertEqual(summary["compared_unique_nonzero_ids"], 6763)


if __name__ == "__main__":
    unittest.main()
