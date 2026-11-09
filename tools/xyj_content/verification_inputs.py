"""Preserve the specific legacy test dataset separately from user-managed package history."""

from pathlib import Path
import shutil

from .binary import ContentError,read_bytes
from .files import output_path,sha256,read_json,write_json


def paired_fixture(root):
    """Materialize the known test package once, checking every byte; never move or delete its original."""
    directory=output_path(root,Path('output/editor/test-fixtures/original-paired'))
    marker=directory/'fixture-files.json'
    if not marker.exists():
        source=output_path(root,Path('output/editor/patches/20261104-165921-7d73b3ee'))
        if not source.is_dir():
            raise ContentError('缺少已独立保存的原始测试数据集。')
        expected={p.relative_to(source).as_posix():sha256(read_bytes(p)) for p in source.rglob('*') if p.is_file()}
        shutil.copytree(source,directory,dirs_exist_ok=True)
        for name,digest in expected.items():
            if sha256(read_bytes(directory/name))!=digest:raise ContentError('测试数据迁移校验失败。')
        write_json(marker,{'format':'xyj-verification-input/v1','files':expected})
    for name,digest in read_json(marker)['files'].items():
        path=(directory/name).resolve()
        if not path.is_relative_to(directory) or sha256(read_bytes(path))!=digest:
            raise ContentError('独立测试数据集已变化。')
    return directory
