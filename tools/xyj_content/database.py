"""Pinned legacy MySQL client, fixed read-only SQL, and minimal NPC snapshot generation."""

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import math
import os
import re
import subprocess

from .binary import ContentError, read_bytes
from .files import atomic_write, output_path

MYSQL_CLIENT_SHA256 = '240639f00a54aa6ea7e4e8f312f884678832a19891f0cf38cd3145515c6180a7'
FIELDS = ('RoleID', 'OriginalObjectID', 'SectionID', 'RoleName', 'NickName',
          *(f'EQArr_{i:02d}' for i in range(10)), 'ReviveSectionID', 'ReviveX', 'ReviveY', 'ReviveZ', 'Hair', 'Face',
          *(f'Hand_ItemArr_{i:02d}' for i in range(9)), *(f'Hand_CountArr_{i:02d}' for i in range(9)))


def connection_binding(config: dict) -> bytes:
    """Bind saved credentials to the exact endpoint, user, database and client; never include the password."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode('utf-8')).digest()


def _query(config: dict, password: str, sql: str) -> str:
    """Execute only module-owned SHOW/SELECT statements with bounded time and sanitized diagnostics."""
    expressions = [f'HEX(`{field}`)' if field in ('RoleName', 'NickName') else f'`{field}`' for field in FIELDS]
    allowed = {statement for table in ('npcpool', 'npcpool_add_on') for statement in
               (f'SHOW COLUMNS FROM {table};',
                f'SELECT {",".join(expressions)} FROM {table} WHERE Removed = 0 ORDER BY RoleID LIMIT 100001;')}
    if sql not in allowed:
        raise ContentError('只允许预定义的只读查询。')
    executable = Path(config['client'])
    if hashlib.sha256(read_bytes(executable)).hexdigest() != MYSQL_CLIENT_SHA256:
        raise ContentError('MySQL 客户端版本未核验；请选择原工具包中兼容 MySQL 4.0 的 mysql.exe。')
    args = [str(executable), '--no-defaults', '--user=' + config['user'], '--batch', '--raw',
            '--skip-column-names', '--connect-timeout=' + str(config['timeout'])]
    if config['transport'] == 'pipe':
        args += ['--pipe', '--socket=' + config['pipe']]
    else:
        args += ['--host=' + config['host'], '--port=' + str(config['port'])]
    args += [config['name'], '--execute=' + sql]
    env = os.environ.copy()
    for name in ('MYSQL_PWD', 'MYSQL_HOST', 'MYSQL_TCP_PORT', 'MYSQL_UNIX_PORT', 'MYSQL_HOME'):
        env.pop(name, None)
    env['MYSQL_PWD'] = password
    try:
        result = subprocess.run(args, env=env, capture_output=True, timeout=config['timeout'] + 20,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as exc:
        raise ContentError('数据库连接或查询超时，原配置未改变。') from exc
    except OSError as exc:
        raise ContentError('兼容 MySQL 客户端不能运行，请检查 Windows 环境及文件权限。') from exc
    if result.returncode:
        # Do not return stderr: it can contain endpoint/user values or injected server text.
        match = re.search(rb'ERROR\s+(\d+)', result.stderr)
        code = match.group(1).decode() if match else ''
        messages = {'1045': '数据库认证被拒绝（1045）：检查账号、密码及该电脑的访问授权。',
                    '1049': '目标数据库不存在（1049）。', '1146': 'NPC 表不存在（1146）。',
                    '1054': 'NPC 表字段不兼容（1054）。', '2002': '本机命名管道连接失败（2002）。',
                    '2003': '数据库主机或端口不能连接（2003）。'}
        raise ContentError(messages.get(code, f'数据库客户端执行失败{f"（{code}）" if code else ""}，未修改数据库。'))
    if len(result.stdout) > 32 * 1024 * 1024:
        raise ContentError('NPC 查询结果超过 32 MiB 限制。')
    try:
        return result.stdout.decode('ascii')
    except UnicodeError as exc:
        raise ContentError('数据库返回格式不兼容。') from exc


def read_npc_rows(config: dict, password: str) -> list[dict]:
    """Validate both schemas and project NPC fields only; account/player tables are never selected."""
    instances = []
    for table in ('npcpool', 'npcpool_add_on'):
        schema = _query(config, password, f'SHOW COLUMNS FROM {table};')
        names = {line.split('\t', 1)[0] for line in schema.splitlines()}
        if not set((*FIELDS, 'Removed')).issubset(names):
            raise ContentError(f'{table} 缺少 NPC 必需字段。')
        expressions = [f'HEX(`{field}`)' if field in ('RoleName', 'NickName') else f'`{field}`' for field in FIELDS]
        rows = _query(config, password, f'SELECT {",".join(expressions)} FROM {table} WHERE Removed = 0 ORDER BY RoleID LIMIT 100001;')
        for line in rows.splitlines():
            values = line.split('\t')
            if len(values) != len(FIELDS):
                raise ContentError('NPC 查询返回列数无效。')
            row = dict(zip(FIELDS, values))
            try:
                position = [float(row['Revive' + axis]) for axis in ('X', 'Y', 'Z')]
                if not all(math.isfinite(v) and abs(v) < 1e9 for v in position):
                    raise ValueError()
                instances.append({'key': f"{table}:{int(row['RoleID'])}", 'id': int(row['RoleID']),
                                  'template_id': int(row['OriginalObjectID']), 'map_id': int(row['SectionID']),
                                  'name': bytes.fromhex(row['RoleName']).decode('gb18030'),
                                  'nickname': bytes.fromhex(row['NickName']).decode('gb18030'),
                                  'equipment': [int(row[f'EQArr_{i:02d}']) for i in range(10)],
                                  'hand_items': [int(row[f'Hand_ItemArr_{i:02d}']) for i in range(9)],
                                  'hand_counts': [int(row[f'Hand_CountArr_{i:02d}']) for i in range(9)],
                                  'revive_map_id': int(row['ReviveSectionID']), 'revive_position': position,
                                  'hair': int(row['Hair']), 'face': int(row['Face'])})
            except (ValueError, UnicodeError) as exc:
                raise ContentError('NPC 查询中的数值或名称编码无效。') from exc
        if len(instances) > 100000:
            raise ContentError('NPC 实例超过 100000 条限制。')
    return instances


def read_zones(directory: Path) -> tuple[list[dict], list[Path]]:
    """Read only map identity keys from region INIs; never retain database or login configuration."""
    zones, paths = [], []
    try:
        candidates = sorted(directory.glob('LocalServer*.ini'))
        if not candidates:
            raise ContentError('地图目录没有 LocalServer 区域配置。')
        for path in candidates:
            values = {}
            for line in read_bytes(path).decode('gb18030').splitlines():
                if '=' in line:
                    key, value = line.split('=', 1)
                    if key.strip() in ('ZoneID', 'ZoneName'):
                        values[key.strip()] = value.split(';', 1)[0].strip()
            zones.append({'id': int(values['ZoneID']), 'name': values['ZoneName']})
            paths.append(path)
    except (OSError, ValueError, KeyError, UnicodeError) as exc:
        if isinstance(exc, ContentError):
            raise
        raise ContentError('区域地图配置格式无效或不可读取。') from exc
    return zones, paths


def write_snapshot(root: Path, instances: list[dict], zones: list[dict], server_sha: str, source: dict) -> Path:
    """Write an immutable candidate snapshot; activation happens only when its settings validation is saved."""
    raw = json.dumps({'zones': zones, 'instances': instances}, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    identity = hashlib.sha256(raw + server_sha.encode('ascii')).hexdigest()
    # Each validation owns its artifact: even identical rows must never rewrite an active manifest.
    import secrets
    directory = output_path(root, Path('output/editor/database-snapshots') / (identity[:16] + '-' + secrets.token_hex(8)))
    manifest = {'format': 'xyj-npc-snapshot/v2', 'catalog_sha256': digest, 'catalog_file': f'catalog-{digest}.json',
                'server_object_sha256': server_sha, 'source': source, 'built_at': datetime.now(timezone.utc).isoformat()}
    atomic_write(directory / manifest['catalog_file'], raw)
    atomic_write(directory / 'manifest.json', json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
    return directory
