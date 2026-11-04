"""Exercise actual file replacement and recovery using local temporary destinations only."""

from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import test_content_tool as fixtures
from deploy_test_patch import deploy
from xyj_content.binary import ContentError
from xyj_content.files import read_json
from xyj_content.patch import PAYLOAD_PATHS, build_patch, verify_patch


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.root = f.root
        self.share = self.root / "mock-remote"
        self.remote_object = self.share / "qingfeng/Object.dbf"
        self.remote_object.parent.mkdir(parents=True)
        self.remote_object.write_bytes(f.loaded.blobs[0])
        self.targets = {"server_object": self.remote_object,
                        "client_object": f.inputs.client, "client_language": f.inputs.language}
        self.before = {k: p.read_bytes() for k, p in self.targets.items()}
        build_patch(self.root, f.destination, f.loaded, f.spec)
        self.report = Path("output/deployment-test")

    def test_installs_exact_three_files_and_fresh_backups(self):
        result = deploy(self.root, self.share, self.fixture.destination, self.report, remote_preflight=False)
        self.assertEqual(result["status"], "deployed_pending_manual_start")
        self.assertEqual(set(result["files_replaced"]), set(self.targets))
        self.assertTrue(result["verification"]["installed_new_object_matches"])
        self.assertFalse(result["verification"]["gameplay_verified"])
        manifest = read_json(self.root / self.fixture.destination / "manifest.json")
        for key, target in self.targets.items():
            relative = manifest["files"][key]["path"]
            self.assertEqual(target.read_bytes(), (self.root / self.fixture.destination / relative).read_bytes())
            self.assertEqual((self.root / self.report / "before" / relative).read_bytes(), self.before[key])

    def test_second_execution_is_idempotent_and_does_not_replace_again(self):
        deploy(self.root, self.share, self.fixture.destination, self.report, remote_preflight=False)
        with patch("deploy_test_patch.os.replace", wraps=os.replace) as operation:
            result = deploy(self.root, self.share, self.fixture.destination,
                            Path("output/deployment-test-again"), remote_preflight=False)
        self.assertEqual(result["status"], "already_deployed_pending_manual_start")
        self.assertEqual(result["files_replaced_this_run"], [])
        self.assertFalse(any(Path(call.args[1]) in self.targets.values() for call in operation.call_args_list))

    def test_server_commit_failure_restores_both_client_files(self):
        original_replace = os.replace
        failed = False

        def fail_server_once(source, target):
            nonlocal failed
            if Path(target) == self.remote_object and not failed:
                failed = True
                raise PermissionError("simulated remote commit failure")
            return original_replace(source, target)

        with patch("deploy_test_patch.os.replace", side_effect=fail_server_once):
            with self.assertRaisesRegex(ContentError, "failed_rolled_back"):
                deploy(self.root, self.share, self.fixture.destination, self.report, remote_preflight=False)
        for key, target in self.targets.items():
            self.assertEqual(target.read_bytes(), self.before[key])
            self.assertEqual(list(target.parent.glob("*.tmp")), [])
        report = read_json(self.root / self.report / "deployment.json")
        self.assertEqual(report["rollback"]["client_object"], "restored")
        self.assertEqual(report["rollback"]["client_language"], "restored")

    def test_staging_failure_changes_no_destination(self):
        from deploy_test_patch import stage_file
        counter = 0

        def fail_second(target, data, token):
            nonlocal counter
            counter += 1
            if counter == 2:
                raise OSError("simulated disk full")
            return stage_file(target, data, token)

        with patch("deploy_test_patch.stage_file", side_effect=fail_second):
            with self.assertRaises(ContentError):
                deploy(self.root, self.share, self.fixture.destination, self.report, remote_preflight=False)
        for key, target in self.targets.items():
            self.assertEqual(target.read_bytes(), self.before[key])
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_changed_target_or_partial_update_is_rejected(self):
        self.remote_object.write_bytes(b"another server version")
        with self.assertRaisesRegex(ContentError, "目标版本已变化"):
            deploy(self.root, self.share, self.fixture.destination, self.report, remote_preflight=False)
        self.assertEqual(self.fixture.inputs.client.read_bytes(), self.before["client_object"])
        self.assertFalse((self.root / self.report).exists())

    def test_deployed_patch_can_be_verified_against_hash_checked_backup(self):
        deploy(self.root, self.share, self.fixture.destination, self.report, remote_preflight=False)
        sources = {key: self.report / "before" / relative for key, relative in PAYLOAD_PATHS.items()}
        result = verify_patch(self.root, self.fixture.destination, source_overrides=sources)
        self.assertTrue(result["all_existing_records_preserved"])
        self.assertTrue(result["matching_new_text"])
        (self.root / sources["client_language"]).write_bytes(b"invalid baseline")
        with self.assertRaisesRegex(ContentError, "源文件已变化"):
            verify_patch(self.root, self.fixture.destination, source_overrides=sources)


if __name__ == "__main__":
    unittest.main()
