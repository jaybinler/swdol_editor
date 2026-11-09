"""Run resource-backed editor tests with all temporary files confined to the project output directory."""

from argparse import ArgumentParser
from pathlib import Path
import sys
import tempfile
import unittest


def main():
    """Support focused or full runs; propagate failures and avoid restricted operating-system temp locations."""
    parser=ArgumentParser(description=__doc__)
    parser.add_argument('names',nargs='*')
    parser.add_argument('--pattern',default='test_*.py')
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'tests'))
    parent=root/'output/editor/test-tmp';parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent) as directory:
        tempfile.tempdir=directory
        suite=unittest.defaultTestLoader.loadTestsFromNames(args.names) if args.names else unittest.defaultTestLoader.discover(str(root/'tests'),pattern=args.pattern)
        result=unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    raise SystemExit(main())
