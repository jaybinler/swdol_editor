"""Validated portable input configuration, user-bound credentials and atomic workspace activation."""

from datetime import datetime, timezone
from pathlib import Path
from threading import RLock, Lock
import hashlib
import importlib.util
import json
import os
import platform
import re
import secrets
import sys
import time

from .binary import ContentError, decode_table, read_bytes
from .catalog import Inputs
from .database import connection_binding, read_npc_rows, read_zones, write_snapshot
from .plot_maps import build_script_index
from .files import atomic_write, output_path, read_json, sha256
from .models import item_model, texture_png
from .objects import find_unique
from .resources import ResourcePaths, clear_resource_caches
from .secrets_windows import protect_password, unprotect_password
from .sprites import icon_reference, sprite_path, sprite_png

PATH_FIELDS = (
    ('client_root', '客户端根目录', 'directory'), ('server_object', '服务端 Object.dbf（读取基线）', 'file'),
    ('server_target', '服务端 Object.dbf 覆盖目标', 'file'),
    ('client_object', '客户端 Object（留空自动定位）', 'file'), ('language', 'Language（留空自动定位）', 'file'),
    ('act_dir', 'Act 目录（留空使用客户端目录）', 'directory'),
    ('model_dir', 'MODEL 目录（留空使用客户端目录）', 'directory'),
    ('tex_dir', 'TEX 目录（留空使用客户端目录）', 'directory'),
    ('tsw_dir', 'Tsw 目录（留空使用客户端目录）', 'directory'),
    ('map_dir', '区域与脚本目录（数据库模式必填，含 ASM）', 'directory'),
    ('map_table', 'map.tb（留空使用地图目录）', 'file'),
    ('npc_snapshot', 'NPC 离线快照目录', 'directory'),
)
DB_DEFAULTS = {'host': '127.0.0.1', 'port': 3306, 'name': 'swdol', 'user': '', 'timeout': 5,
               'client': 'XYJ_SEVVER/mysql/bin/mysql.exe', 'transport': 'tcp', 'pipe': 'MYSQL'}


def default_config() -> dict:
    """Return migration-friendly defaults without credentials or machine-specific drive names."""
    return {**{name: '' for name, _, _ in PATH_FIELDS}, 'client_root': 'swdol',
            'server_object': 'output/patch-live-baseline-318/server/qingfeng/Object.dbf',
            'map_dir': 'XYJ_SEVVER/qingfeng', 'npc_snapshot': 'output/editor/npc-snapshot',
            'npc_mode': 'offline', 'database': dict(DB_DEFAULTS), 'remember_password': False}


def input_path(root: Path, value: str) -> Path:
    """Resolve a user-selected read-only path; never use it as an output location."""
    value = value.strip().strip('"')
    if not value or len(value) > 4096 or any(c in value for c in ('\0', '\n', '\r')):
        raise ContentError('文件路径为空、过长或包含无效字符。')
    path = Path(os.path.expandvars(value)).expanduser()
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


def portable_path(root: Path, value: str | Path) -> str:
    """Keep paths inside the tool relative so moving the complete tool directory remains possible."""
    path = input_path(root, str(value))
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


def normalize_config(root: Path, value: dict) -> dict:
    """Validate field names/types before any filesystem, native utility or database operation."""
    defaults = default_config()
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise ContentError('设置包含未支持的字段。')
    result = {**defaults, **value}
    for name, _, _ in PATH_FIELDS:
        text = result[name]
        if not isinstance(text, str):
            raise ContentError('路径必须是文本。')
        result[name] = portable_path(root, text) if text.strip() else ''
    if not result['client_root'] or not result['server_object']:
        raise ContentError('请配置客户端根目录和服务端 Object。')
    if result['npc_mode'] not in ('offline', 'database') or type(result['remember_password']) is not bool:
        raise ContentError('NPC 模式或密码保存选项无效。')
    db = result['database']
    if not isinstance(db, dict) or set(db) - set(DB_DEFAULTS):
        raise ContentError('数据库设置包含未支持的字段。')
    db = {**DB_DEFAULTS, **db}
    for name in ('host', 'name', 'user', 'client', 'transport', 'pipe'):
        if not isinstance(db[name], str) or len(db[name]) > 4096 or any(ord(c) < 32 for c in db[name]):
            raise ContentError('数据库连接字段格式无效。')
        db[name] = db[name].strip()
    if not re.fullmatch(r'[A-Za-z0-9.\-:_]+', db['host']):
        raise ContentError('数据库主机格式无效。')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_$-]{0,63}', db['name']):
        raise ContentError('数据库名格式无效。')
    if type(db['port']) is not int or not 1 <= db['port'] <= 65535:
        raise ContentError('数据库端口必须为 1 至 65535。')
    if type(db['timeout']) is not int or not 1 <= db['timeout'] <= 15:
        raise ContentError('连接超时必须为 1 至 15 秒。')
    if db['transport'] not in ('tcp', 'pipe') or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', db['pipe']):
        raise ContentError('数据库连接方式或命名管道名称无效。')
    if db['transport'] == 'pipe' and db['host'] not in ('127.0.0.1', 'localhost', '::1'):
        raise ContentError('命名管道只支持本机；连接远端请使用 TCP。')
    db['client'] = portable_path(root, db['client'])
    if result['npc_mode'] == 'database' and not db['user']:
        raise ContentError('数据库模式必须填写数据库账号。')
    result['database'] = db
    return result


def resolve_paths(root: Path, config: dict):
    """Provide the same explicit roots to all runtime consumers, including external and UNC inputs."""
    client = input_path(root, config['client_root'])
    pick = lambda name, fallback: input_path(root, config[name]) if config[name] else fallback
    paths = Inputs(input_path(root, config['server_object']), pick('client_object', client / 'Main/Object.dbf'),
                   pick('language', client / 'Main/Language.DBF'))
    resources = ResourcePaths(client, pick('act_dir', client / 'Act'), pick('model_dir', client / 'MODEL'),
                              pick('tex_dir', client / 'TEX'), pick('tsw_dir', client / 'Tsw'))
    snapshot = input_path(root, config['npc_snapshot']) if config['npc_snapshot'] else root / 'output/editor/npc-snapshot'
    maps = input_path(root, config['map_dir']) if config['map_dir'] else None
    return paths, resources, snapshot, maps


class SettingsController:
    """Own configuration transactions while keeping the last valid workspace usable on every failure."""

    def __init__(self, root: Path, factory, server_override: Path | None = None):
        self.root = root.resolve()
        self.factory = factory
        self.token = secrets.token_urlsafe(32)
        self.lock, self.validation_lock = RLock(), Lock()
        self.generation = 0
        self.workspace, self.pending = None, None
        self.path = output_path(self.root, Path('output/editor/settings.json'))
        self.document = None
        self.config = default_config()
        self.setup_error = ''
        try:
            from .deployment import recover_deployment
            recover_deployment(self.root)
            if self.path.exists():
                document = read_json(self.path)
                if document.get('format') != 'xyj-settings/v1':
                    raise ContentError('设置文件版本无效。')
                self.config = normalize_config(self.root, document['config'])
                self.document = document
                self._check_files(document.get('validated_files', []))
            if server_override:
                self.config['server_object'] = portable_path(self.root, server_override)
            self.workspace = factory(self.config)
            self.workspace.token = self.token
        except (ContentError, OSError, KeyError, TypeError, ValueError) as exc:
            self.workspace = None
            self.setup_error = f'请校验数据源设置：{exc}'

    def _check_files(self, fingerprints):
        for entry in fingerprints:
            path = input_path(self.root, entry['path'])
            if sha256(read_bytes(path)) != entry['sha256']:
                raise ContentError('校验后的源文件已变化，请重新校验。')

    def _saved_password(self, config):
        document = self.document or {}
        filename = document.get('secret_file')
        if not filename:
            return ''
        if connection_binding(config['database']) != connection_binding(document['config']['database']):
            raise ContentError('数据库目标或账号已改变，请为新目标重新输入密码。')
        if not re.fullmatch(r'[0-9a-f]{32}\.bin', filename):
            raise ContentError('密码保护文件索引无效。')
        return unprotect_password(read_bytes(self.path.parent / 'secrets' / filename), connection_binding(config['database']))

    def public(self) -> dict:
        """Expose only editable nonsecret fields and a password-presence status, never encrypted bytes."""
        with self.lock:
            if self.pending and self.pending['expires'] < time.monotonic():
                self.pending = None
            password_state = 'missing'
            if self.document and self.document.get('secret_file'):
                try:
                    self._saved_password(self.config)
                    password_state = 'saved'
                except ContentError:
                    password_state = 'unavailable'
            return {'config': self.config, 'password_state': password_state,
                    'saved': self.document is not None, 'ready': self.workspace is not None,
                    'setup_error': self.setup_error, 'paths': [{'key': k, 'label': label, 'kind': kind} for k, label, kind in PATH_FIELDS]}

    def export(self) -> dict:
        """Export only portable, nonsecret settings; activation still requires validation on the target computer."""
        return {'format': 'xyj-settings-export/v1', 'config': self.config,
                'note': '导入后重新校验；密码不包含在此文件中。'}

    def validate(self, value: dict) -> dict:
        """Prepare and fingerprint a candidate workspace; a failure never changes the active configuration."""
        if not self.validation_lock.acquire(blocking=False):
            raise ContentError('另一项配置正在校验，请稍后重试。')
        checks = []
        with self.lock:
            self.pending = None
            generation = self.generation
            old = self.workspace
            revision = old.revision if old else None
        try:
            if set(value) != {'config', 'password'} or not isinstance(value['password'], str) or len(value['password']) > 4096:
                raise ContentError('校验请求格式无效。')
            config = normalize_config(self.root, value['config'])
            password = value['password']
            checks.append({'name': '配置格式', 'status': 'passed', 'message': '路径及连接字段格式有效。'})
            if sys.version_info < (3, 11) or not importlib.util.find_spec('lzokay'):
                raise ContentError('需要 Python 3.11 或更新版本及 lzokay 依赖。')
            for path in ('three.module.js', 'three.core.js'):
                read_bytes(self.root / 'editor/node_modules/three/build' / path)
            read_bytes(self.root / 'editor/node_modules/three/examples/jsm/controls/OrbitControls.js')
            checks.append({'name': '运行环境', 'status': 'passed', 'message': f'Python {platform.python_version()}、lzokay 与 Three.js 可读取。'})
            clear_resource_caches()
            candidate = self.factory(config)
            _, resources, _, maps = resolve_paths(self.root, config)
            probe_files = [candidate.loaded.paths.server, candidate.loaded.paths.client, candidate.loaded.paths.language]
            probe_files += [resources / folder / filename for folder, filename in
                            [('Act', 'all_char.act'), ('Act', 'all_item.act'), ('MODEL', 'ALL_CHAR.IDX'),
                             ('MODEL', 'ALL_ITEM.IDX'), ('Tsw', 'all_item.tsw')]]
            find_unique(candidate.loaded.server, 10)
            item = find_unique(candidate.loaded.client, 10)[1]
            model = item_model(resources, item)
            reference = icon_reference(resources, item)
            sprite_png(resources, **reference)
            probe_files.extend([resources / 'MODEL' / model['file'], sprite_path(resources, reference['frame'])])
            for mesh in model['meshes']:
                if mesh['texture'] is not None:
                    texture_png(resources, mesh['texture'])
                    probe_files.append(resources / 'TEX' / f"ALL{mesh['texture']:05d}.TEX")
            # A real patch eligibility check proves that the representative server/client template agrees.
            from .editor import item_detail
            if not item_detail(candidate.loaded, 10)['editable']:
                raise ContentError('两端桃木剑模板不兼容，不能保证编辑与补丁功能。')
            checks.append({'name': '游戏文件与三维资源', 'status': 'passed', 'message': 'Object、Language、桃木剑模型／贴图／图标及双端模板核对通过。'})
            if config['server_target']:
                from .deployment import deployment_targets
                targets = deployment_targets(self.root, config)
                for key, original in zip(targets, candidate.loaded.blobs):
                    if read_bytes(targets[key]) != original:
                        raise ContentError(f'覆盖目标与读取基线不一致：{targets[key]}')
                probe_files.extend(targets.values())
                checks.append({'name': '自动覆盖目标', 'status': 'passed', 'message': '三份目标文件与读取基线一致；覆盖时再次核对并验证写入权限。'})
            else:
                checks.append({'name': '自动覆盖目标', 'status': 'warning', 'message': '未配置覆盖目标，仍可编辑；自动覆盖前需填写并重新校验。'})
            if config['npc_mode'] == 'database':
                if not password and self.document and self.document.get('secret_file'):
                    password = self._saved_password(config)
                db = {**config['database'], 'client': str(input_path(self.root, config['database']['client']))}
                if maps is None:
                    raise ContentError('数据库模式需要区域地图配置目录。')
                zones, ini_files = read_zones(maps)
                table = input_path(self.root, config['map_table']) if config['map_table'] else maps / 'map.tb'
                if decode_table(read_bytes(table)).stride != 270:
                    raise ContentError('map.tb 格式不兼容。')
                probe_files.extend([table, *ini_files, Path(db['client'])])
                instances = read_npc_rows(db, password)
                checks.append({'name': '数据库认证与只读查询', 'status': 'passed', 'message': f'两张 NPC 表结构通过，读取 {len(instances)} 条记录；未执行写 SQL。'})
                script_index, script_files = build_script_index(instances, candidate.loaded.server.records, maps)
                probe_files.extend(script_files)
                checks.append({'name': '脚本地图关系', 'status': 'passed',
                               'message': f"已解析 {len(script_index['files'])} 份脚本，保存 {len(script_index['relations'])} 条生成关系。"})
                source = {'server': config['database']['host'], 'captured_at': datetime.now(timezone.utc).isoformat(),
                          'scope': '存档与已解析脚本配置；不是实时实例', 'consistency': '白名单只读查询生成；未锁库，不是跨表原子快照'}
                directory = write_snapshot(self.root, instances, zones, candidate.hashes['server_object'], source,
                                           script_index=script_index)
                config['npc_snapshot'] = portable_path(self.root, directory)
                candidate.npc_directory = directory
            else:
                checks.append({'name': '数据库连接', 'status': 'skipped', 'message': '当前为离线模式，不要求数据库在线。'})
                if maps and maps.is_dir():
                    zones, ini_files = read_zones(maps)
                    probe_files.extend(ini_files)
                    table = input_path(self.root, config['map_table']) if config['map_table'] else maps / 'map.tb'
                    if table.is_file():
                        if decode_table(read_bytes(table)).stride != 270:
                            raise ContentError('map.tb 格式不兼容。')
                        probe_files.append(table)
                else:
                    checks.append({'name': '外部地图目录', 'status': 'warning', 'message': '离线查看使用快照地图名称，外部地图目录不可用。'})
            catalog = candidate.creatures()
            for tid in (20000, 20135):
                if tid not in catalog.records:
                    if tid == 20000:
                        raise ContentError('缺少用于核对角色资源版本的蝙蝠模板。')
                    continue
                character = catalog.model(tid)
                probe_files.append(resources / 'MODEL' / character['file'])
                for mesh in character['meshes']:
                    if mesh.get('texture_url'):
                        parts = mesh['texture_url'].split('/')
                        frame, pose = int(parts[-2]), int(parts[-1][:-4])
                        sprite_png(resources, frame, pose)
                        probe_files.append(sprite_path(resources, frame))
                    elif mesh['texture'] is not None:
                        texture_png(resources, mesh['texture'])
                        probe_files.append(resources / 'TEX' / f"ALL{mesh['texture']:05d}.TEX")
            meta = catalog.metadata()
            checks.append({'name': 'NPC快照与角色预览', 'status': 'passed', 'message': f"{meta['templates']} 个模板、{meta['instances']} 条实例；蝙蝠模型加载通过。"})
            if not meta['script_map_coverage']['available']:
                checks.append({'name': '脚本地图覆盖', 'status': 'warning',
                               'message': '旧快照仅含存档地图关系，更新 v3 快照可补充脚本生成地图。'})
            if config['npc_mode'] == 'database':
                expected_scripts = {entry['file']: entry['sha256'] for entry in script_index['files']}
                expected_scripts['NEWLOCALSERVER.EXE'] = script_index['native_sha256']
                for path in script_files:
                    if sha256(read_bytes(path)) != expected_scripts[path.name.upper()]:
                        raise ContentError('校验过程中脚本发生变化，请重新校验。')
            manifest_path = candidate.npc_directory / 'manifest.json'
            manifest = read_json(manifest_path)
            probe_files.extend([manifest_path, candidate.npc_directory / manifest['catalog_file']])
            fingerprints = [{'path': portable_path(self.root, path), 'sha256': sha256(read_bytes(path))}
                            for path in dict.fromkeys(probe_files)]
            # Ensure the actual loaded sources did not change while probes or database work ran.
            for path, blob in zip((candidate.loaded.paths.server, candidate.loaded.paths.client, candidate.loaded.paths.language), candidate.loaded.blobs):
                if read_bytes(path) != blob:
                    raise ContentError('校验过程中物件或语言文件发生变化，请重新校验。')
            if config['remember_password'] and password and config['npc_mode'] == 'database':
                protect_password(password, connection_binding(config['database']))
            with self.lock:
                if generation != self.generation:
                    raise ContentError('工作区已改变，请重新校验。')
                receipt = secrets.token_urlsafe(32)
                self.pending = {'receipt': receipt, 'config': config, 'password': password,
                                'workspace': candidate, 'expires': time.monotonic() + 300,
                                'generation': generation, 'revision': revision, 'fingerprints': fingerprints}
            return {'valid': True, 'checks': checks, 'receipt': receipt, 'config': config,
                    'expires_in': 300, 'message': '校验通过，可保存并应用。少量既有资源的特殊效果仍以当前支持范围为准。'}
        except (ContentError, OSError, ValueError, KeyError, TypeError) as exc:
            checks.append({'name': '校验未完成', 'status': 'failed', 'message': str(exc) if isinstance(exc, ContentError) else '配置或数据格式无效，请检查所填位置。'})
            return {'valid': False, 'checks': checks}
        finally:
            self.validation_lock.release()

    def apply(self, value: dict) -> dict:
        """Save only an unexpired, unchanged validation receipt, then atomically activate its prepared workspace."""
        with self.lock:
            pending = self.pending
            if set(value) != {'config', 'receipt'} or pending is None or not isinstance(value['receipt'], str):
                raise ContentError('请先校验配置。')
            if not secrets.compare_digest(value['receipt'], pending['receipt']) or pending['expires'] < time.monotonic():
                self.pending = None
                raise ContentError('校验凭据无效或已过期，请重新校验。')
            if normalize_config(self.root, value['config']) != pending['config']:
                raise ContentError('校验后配置发生变化，请重新校验。')
            old = self.workspace
            guard = old.lock if old else RLock()
            with guard:
                if pending['generation'] != self.generation or (old and old.revision != pending['revision']):
                    raise ContentError('校验后工作区或草稿发生变化，请重新校验。')
                self._check_files(pending['fingerprints'])
                config = pending['config']
                secret_file = None
                if config['npc_mode'] == 'database' and config['remember_password'] and pending['password']:
                    cipher = protect_password(pending['password'], connection_binding(config['database']))
                    secret_file = secrets.token_hex(16) + '.bin'
                    atomic_write(output_path(self.root, self.path.parent / 'secrets' / secret_file), cipher)
                document = {'format': 'xyj-settings/v1', 'config': config, 'secret_file': secret_file,
                            'validated_files': pending['fingerprints'], 'saved_at': datetime.now(timezone.utc).isoformat()}
                atomic_write(self.path, json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8'))
                self.workspace = pending['workspace']
                self.workspace.token = self.token
                self.config, self.document = config, document
                self.generation += 1
                self.setup_error = ''
                self.pending = None
                clear_resource_caches()
            return {'saved': True, 'ready': True, 'message': '设置已保存并应用，旧工作区草稿已保留。'}
