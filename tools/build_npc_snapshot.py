"""Compile an already verified NPC research projection into the editor's minimal read-only snapshot."""

from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math

from xyj_content.binary import ContentError, read_bytes, decode_table
from xyj_content.files import atomic_write, output_path
from xyj_content.plot_maps import build_script_index, PLOT_FIELDS


def build(root: Path, source: Path, *, destination: Path = Path('output/editor/npc-snapshot'),
          map_directory: Path | None = None) -> dict:
    """Validate source evidence and publish sanitized data before its hash-bound manifest; never access a server."""
    root, source = root.resolve(), source.resolve()
    if not source.is_relative_to(root / 'output'):
        raise ContentError('NPC 来源必须是项目 output 内的已核验快照。')
    verification = json.loads(read_bytes(source / 'npc-source/remote_verification.json').decode('utf-8-sig'))
    if len(verification) != 6 or not all(r['matches_remote'] and r['mtime_stable'] for r in verification):
        raise ContentError('NPC 源文件复核未通过。')
    for entry in verification:
        if Path(entry['name']).name != entry['name']:
            raise ContentError('源文件名无效。')
        if hashlib.sha256(read_bytes(source / 'npc-source' / entry['name'])).hexdigest() != entry['sha256']:
            raise ContentError('NPC 源副本已变化。')
    projection = json.loads(read_bytes(source / 'npc-projection.json'))
    if not projection.get('verified_at'):
        raise ContentError('NPC 投影缺少复核时间。')
    zones = json.loads(read_bytes(source / 'zones.json'))
    remote = json.loads(read_bytes(source / 'remote-static/snapshot.json'))
    native = next(r for r in remote['files'] if r['path'] == 'qingfeng/NewLocalServer.exe')
    if native['sha256'] != '6b78293ec1bd221152ea38a143f992e38978ed62fd8caafc45e5e8510206e982':
        raise ContentError('服务端程序版本已变化，需重新核验携带生成规则。')
    object_entry = next(r for r in remote['files'] if r['path'] == 'qingfeng/Object.dbf')
    if hashlib.sha256(read_bytes(source / 'remote-static/server/qingfeng/Object.dbf')).hexdigest() != object_entry['sha256']:
        raise ContentError('NPC 物件基线已变化。')
    def integer(row, key):
        value = int(row[key])
        if not -2147483648 <= value <= 2147483647:
            raise ContentError('NPC 整数字段越界。')
        return value
    instances = []
    for name in ('npcpool', 'npcpool_add_on'):
        for row in projection['tables'][name]['rows']:
            if integer(row, 'Removed') != 0:
                continue
            position = [float(row['Revive' + axis]) for axis in ('X', 'Y', 'Z')]
            if not all(math.isfinite(v) and abs(v) < 1e9 for v in position):
                raise ContentError('NPC 重生坐标无效。')
            instances.append({'key': f"{name}:{integer(row, 'RoleID')}",
                              'id': integer(row, 'RoleID'), 'template_id': integer(row, 'OriginalObjectID'),
                              'map_id': integer(row, 'SectionID'), 'name': str(row['RoleName'])[:200],
                              'nickname': str(row['NickName'])[:200],
                              'equipment': [integer(row, f'EQArr_{i:02d}') for i in range(10)],
                              'hand_items': [integer(row, f'Hand_ItemArr_{i:02d}') for i in range(9)],
                              'hand_counts': [integer(row, f'Hand_CountArr_{i:02d}') for i in range(9)],
                              'plots': [integer(row, field) for field in PLOT_FIELDS],
                              'revive_map_id': integer(row, 'ReviveSectionID'), 'revive_position': position,
                              'hair': integer(row, 'Hair'), 'face': integer(row, 'Face')})
    catalog = {'zones': [{'id': int(z['ZoneID']), 'name': z['ZoneName']} for z in zones], 'instances': instances}
    records = decode_table(read_bytes(source / 'remote-static/server/qingfeng/Object.dbf')).records
    catalog['script_index'], _ = build_script_index(instances, records, map_directory or root / 'XYJ_SEVVER/qingfeng')
    raw = json.dumps(catalog, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    manifest = {'format': 'xyj-npc-snapshot/v3', 'catalog_sha256': hashlib.sha256(raw).hexdigest(),
                'server_object_sha256': object_entry['sha256'],
                'source': {'server': remote['share'], 'captured_at': projection['verified_at'],
                           'scope': '服务端存档与已解析脚本生成配置；不代表实时活跃数量或完整脚本分布',
                           'consistency': '六个源文件哈希复核通过；未锁库，不是运行时原子快照'},
                'built_at': datetime.now(timezone.utc).isoformat()}
    manifest['catalog_file'] = f"catalog-{manifest['catalog_sha256']}.json"
    dest = output_path(root, destination)
    atomic_write(dest / manifest['catalog_file'], raw)
    atomic_write(dest / 'manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
    return {'instances': len(instances), 'zones': len(zones),
            'script_relations': len(catalog['script_index']['relations']), 'path': str(dest)}


def main() -> int:
    """Build a local artifact and report errors without printing any credentials or database rows."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('output/npc-investigation-20261103'))
    parser.add_argument('--out', type=Path, default=Path('output/editor/npc-snapshot'))
    parser.add_argument('--map-dir', type=Path, help='区域目录，包含 NewLocalServer.exe 与 ASM/TALK*.EXE')
    args = parser.parse_args()
    try:
        print(json.dumps(build(Path(__file__).resolve().parents[1], args.source, destination=args.out,
                               map_directory=args.map_dir), ensure_ascii=False))
        return 0
    except (ContentError, OSError, ValueError, KeyError, StopIteration, TypeError) as exc:
        print(f'NPC 快照生成失败：{exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
