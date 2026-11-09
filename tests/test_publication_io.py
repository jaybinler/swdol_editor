"""Bounded retry of an uncommitted filesystem action must never change operation semantics."""
from pathlib import Path
from unittest.mock import patch
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from xyj_content.files import move_path,retry_file_operation


class PublicationIOTests(unittest.TestCase):
    def test_transient_windows_denial_retries_the_same_rename_then_commits_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            source=Path(temporary)/'source';target=Path(temporary)/'target';source.write_bytes(b'complete')
            import os
            original=os.rename;calls=[]
            def rename(a,b):
                calls.append((a,b))
                if len(calls)<3:
                    error=PermissionError('transient');error.winerror=5;raise error
                return original(a,b)
            with patch('xyj_content.files.os.rename',side_effect=rename),patch('xyj_content.files.time.sleep'):
                move_path(source,target)
            self.assertEqual(calls,[(source,target)]*3);self.assertFalse(source.exists());self.assertEqual(target.read_bytes(),b'complete')

    def test_permanent_denial_is_bounded_and_nonsharing_errors_are_not_retried(self):
        calls=[]
        def denied():
            calls.append(1);error=PermissionError('permanent');error.winerror=5;raise error
        with patch('xyj_content.files.time.sleep'),self.assertRaises(PermissionError):retry_file_operation(denied)
        self.assertEqual(len(calls),5)
        calls.clear()
        def invalid():calls.append(1);raise OSError('not a Windows sharing error')
        with self.assertRaises(OSError):retry_file_operation(invalid)
        self.assertEqual(len(calls),1)
