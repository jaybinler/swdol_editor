"""Create an explicit allowlist migration ZIP without passwords, raw SQL tables, drafts or game assets."""

from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import hashlib
import json
import re
import uuid

from xyj_content.database import read_zones, MYSQL_CLIENT_SHA256
from xyj_content.files import output_path, read_json, write_json


def main():
    """Package runtime and sanitized map identities only; game resource locations are selected on the target PC."""
    root = Path(__file__).resolve().parents[1]
    version = read_json(root / 'editor/package.json')['version']
    frontend = root / 'editor/dist'
    assets = sorted(path for path in (frontend / 'workshop-assets').glob('*')
                    if re.fullmatch(r'[\w-]+-[\w-]{8}\.(?:js|css)', path.name, flags=re.ASCII) and path.is_file())
    if not (frontend / 'index.html').is_file() or not assets:
        raise ValueError('请先执行 npm --prefix editor run build。')
    archive = output_path(root, Path(f'output/migration/道具工坊-{version}-Windows-{uuid.uuid4().hex[:8]}.zip'))
    archive.parent.mkdir(parents=True, exist_ok=True)
    files = [*sorted((root / 'tools').rglob('*.py')),
             *[root / name for name in ('启动道具编辑器.cmd','检查运行环境.cmd','requirements.txt','README.md','交接文档.md',
                                        'research/2026-11-03/编辑器0.5设置与迁移说明.md',
                                        'research/2026-11-03/实例携带与生成概率0.5.2修复.md',
                                        'research/2026-11-04/脚本地图索引0.5.4修复.md',
                                        'research/2026-11-04/人物穿戴自动匹配0.5.5修复.md',
                                        'research/2026-11-06/怪物自动覆盖0.13说明.md',
                                        'research/2026-11-06/内置怪物模型编辑0.14说明.md')],
             frontend / 'index.html', frontend / '.vite/license.md', *assets,
             root / 'editor/item-workflow.js', root / 'editor/package.json',
             root / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf',
             root / 'XYJ_SEVVER/qingfeng/map.tb', root / 'XYJ_SEVVER/mysql/bin/mysql.exe']
    client = root / 'XYJ_SEVVER/mysql/bin/mysql.exe'
    assert hashlib.sha256(client.read_bytes()).hexdigest() == MYSQL_CLIENT_SHA256
    # Copy only the selected minimal snapshot, never settings or credential files.
    snapshot = root / 'output/editor/npc-snapshot'
    settings = root / 'output/editor/settings.json'
    if settings.exists():
        from xyj_content.settings import input_path
        snapshot = input_path(root, read_json(settings)['config']['npc_snapshot'])
    manifest = read_json(snapshot / 'manifest.json')
    digest = manifest.get('catalog_sha256', '')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest) or manifest.get('catalog_file') != f'catalog-{digest}.json':
        raise ValueError('NPC 快照文件索引无效，已取消打包。')
    if hashlib.sha256((snapshot / manifest['catalog_file']).read_bytes()).hexdigest() != digest:
        raise ValueError('NPC 快照内容校验失败，已取消打包。')
    files.extend([snapshot / 'manifest.json', snapshot / manifest['catalog_file']])
    zones, _ = read_zones(root / 'XYJ_SEVVER/qingfeng')
    hashes = {}
    with ZipFile(archive, 'x', ZIP_DEFLATED) as bundle:
        for path in files:
            name = ('output/editor/npc-snapshot/' + path.name) if path.parent == snapshot else path.relative_to(root).as_posix()
            raw = path.read_bytes()
            bundle.writestr(name, raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        for zone in zones:
            name = f"XYJ_SEVVER/qingfeng/LocalServer-{zone['id']}.ini"
            raw = f"[Zone]\nZoneID={zone['id']}\nZoneName={zone['name']}\n".encode('gb18030')
            bundle.writestr(name, raw)
            hashes[name] = hashlib.sha256(raw).hexdigest()
        bundle.writestr('migration-manifest.json', json.dumps({'version':version,'files':hashes,
                        'excluded':'passwords, settings, credentials, database tables, drafts, .venv, game client assets'}, ensure_ascii=False, indent=2))
    report = {'archive':str(archive.relative_to(root)), 'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
              'files':len(hashes), 'bytes':archive.stat().st_size}
    write_json(root / 'research/2026-11-03/windows_migration_package.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
