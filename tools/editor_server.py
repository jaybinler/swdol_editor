"""Loopback editor with explicit saved-target deployment, local backup and recovery."""

from argparse import ArgumentParser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit, parse_qs
import json
import mimetypes
import secrets
import struct
import sys
import webbrowser
import hashlib
import socket
import os
import subprocess

from xyj_content.binary import ContentError, read_bytes
from xyj_content.catalog import Inputs, load_inputs, source_hashes
from xyj_content.editor import (export_editor_patch, field_schema, item_detail,
                                item_id_availability, validate_edits, verify_editor_patch)
from xyj_content.files import output_path, read_json, write_json
from xyj_content.models import item_model, texture_png
from xyj_content.sprites import icon_reference, sprite_png
from xyj_content.appearance import equipped_model, profile_options
from xyj_content.taxonomy import item_categories, category_options
from xyj_content.objects import find_unique, view_record
from xyj_content.creatures import CreatureCatalog
from xyj_content.settings import SettingsController

ROOT = Path(__file__).resolve().parents[1]


class EditorState:
    """Immutable source tables with a revision-controlled, baseline-bound draft."""

    def __init__(self, root: Path, server: Path | None = None, *, config: dict | None = None):
        self.root = root.resolve()
        if config is None:
            server = server.resolve()
            if not server.is_relative_to(self.root):
                raise ContentError("服务端基线必须是项目内的本地副本。")
            self.client = self.root / 'swdol'
            paths = Inputs(server, self.client / 'Main/Object.dbf', self.client / 'Main/Language.DBF')
            self.npc_directory = self.root / 'output/editor/npc-snapshot'
        else:
            from xyj_content.settings import resolve_paths
            paths, self.client, self.npc_directory, _ = resolve_paths(self.root, config)
        self.loaded = load_inputs(paths)
        self.sources = source_hashes(self.loaded)
        self.hashes = {key: value['sha256'] for key, value in self.sources.items()}
        self.token = secrets.token_urlsafe(32)
        self.lock = RLock()
        self._creatures = None
        self._acquisition = None
        self.map_directory = (config or {}).get('map_dir', 'XYJ_SEVVER/qingfeng')
        self.draft_path = output_path(self.root, Path('output/editor/draft.json'))
        legacy = self.draft_path
        if config is not None:
            identity = json.dumps({'hashes': self.hashes, 'paths': [str(p.resolve()) for p in (paths.server, paths.client, paths.language)]}, sort_keys=True)
            namespace = hashlib.sha256(identity.encode('utf-8')).hexdigest()
            self.draft_path = output_path(self.root, Path('output/editor/workspaces') / namespace / 'draft.json')
        self.edits, self.revision = [], 0
        self.creature_edits = None
        self.creature_revision = 0
        self.creature_draft_path = self.draft_path.with_name('creatures.json')
        read_draft = self.draft_path
        if (not read_draft.exists() and config is not None and legacy.exists()
                and paths.server.resolve() == (self.root / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf').resolve()
                and paths.client.resolve() == (self.root / 'swdol/Main/Object.dbf').resolve()
                and paths.language.resolve() == (self.root / 'swdol/Main/Language.DBF').resolve()):
            read_draft = legacy
        if read_draft.exists():
            draft = read_json(read_draft)
            if draft.get('format') != 'xyj-editor-draft/v1' or draft.get('sources') != self.hashes:
                if config is None or read_draft != legacy:
                    raise ContentError("已有草稿的基线与当前文件不同，请在设置中核对对应工作区。")
                # A changed source receives its own workspace; keep the unmatched legacy file untouched.
            else:
                self.edits = validate_edits(self.loaded, draft.get('edits'))
                self.revision = draft.get('revision', 0)
                if type(self.revision) is not int or self.revision < 0:
                    raise ContentError("草稿版本号无效。")
        self.items = []
        ids = Counter(struct.unpack_from('<I', r, 4)[0] for r in self.loaded.client.records)
        for row, record in enumerate(self.loaded.client.records):
            view = view_record(record, row, self.loaded.language)
            if view.object_id and ids[view.object_id] == 1:
                actor = struct.unpack_from('<H', record, 60)[0]
                category = item_categories(record)
                has_icon = False
                if category['is_item']:
                    try:
                        icon_reference(self.client, record)
                        has_icon = True
                    except ContentError:
                        pass
                self.items.append({'id': view.object_id, 'name': view.name, 'type': view.type_code,
                                   'item_asset': view.type_code == 2 and 3001 <= actor <= 5999,
                                   'has_icon': has_icon, **category})
        self.items.sort(key=lambda item: (not item['is_item'], not item['has_icon'], item['id']))

    def creatures(self):
        """Lazily validate and load the NPC snapshot; an unavailable snapshot never blocks item editing."""
        with self.lock:
            if self._creatures is None:
                self._creatures = CreatureCatalog(self.root, self.loaded, client=self.client, directory=self.npc_directory)
            return self._creatures

    def save(self, value: dict) -> dict:
        """Atomically replace a validated draft, rejecting stale browser-tab revisions."""
        if set(value) != {'revision', 'edits'} or type(value['revision']) is not int or value['revision'] != self.revision:
            raise ContentError("草稿已在其他页面修改，请刷新后再操作。")
        edits = validate_edits(self.loaded, value['edits'])
        self.load_creature_draft()
        reserved = {e['target_id'] for e in self.creature_edits}
        if any(e['target_id'] in reserved for e in edits):
            raise ContentError('编号与怪物草稿冲突，请修改目标编号。')
        from xyj_content.acquisition import has_acquisition
        if has_acquisition(edits):
            self.acquisition().validate(edits)
        revision = self.revision + 1
        write_json(self.draft_path, {'format': 'xyj-editor-draft/v1', 'sources': self.hashes,
                                    'revision': revision, 'edits': edits})
        self.edits, self.revision = edits, revision
        return {'revision': revision, 'edits': edits}

    def load_creature_draft(self):
        """Load an independently versioned NPC draft only when the authoring feature needs it."""
        from xyj_content.creature_editor import FORMAT, validate
        if self.creature_edits is not None:
            return
        edits, revision = [], 0
        if self.creature_draft_path.exists():
            data = read_json(self.creature_draft_path)
            if data.get('format') != FORMAT or data.get('sources') != self.hashes:
                raise ContentError('怪物草稿基线不匹配，请核对原工作区。')
            revision = data.get('revision')
            if type(revision) is not int or revision < 0:
                raise ContentError('怪物草稿版本无效。')
            edits = validate(self.loaded, data.get('edits'), self.creatures())
        self.creature_edits, self.creature_revision = edits, revision

    def save_creatures(self, value):
        """Atomically replace NPC operations; item drafts and their revisions remain independent."""
        from xyj_content.creature_editor import FORMAT, validate
        self.load_creature_draft()
        if (set(value) != {'revision', 'edits'} or type(value['revision']) is not int or
                value['revision'] != self.creature_revision):
            raise ContentError('怪物草稿已在其他页面修改，请重新读取草稿。')
        if not self.creatures().reset_coverage['available']:
            raise ContentError('怪物编辑需要已核验原生版本的 v3 NPC 快照。')
        edits = validate(self.loaded, value['edits'], self.creatures(), {e['target_id'] for e in self.edits})
        revision = self.creature_revision + 1
        write_json(self.creature_draft_path, {'format': FORMAT, 'sources': self.hashes, 'revision': revision, 'edits': edits})
        self.creature_edits, self.creature_revision = edits, revision
        return {'revision': revision, 'edits': edits}

    def acquisition(self):
        """Load optional acquisition choices lazily; never connect to the production database."""
        from xyj_content.acquisition import AcquisitionCatalog
        from xyj_content.settings import input_path
        with self.lock:
            if self._acquisition is None:
                directory = input_path(self.root, self.map_directory) if self.map_directory else None
                self._acquisition = AcquisitionCatalog(self.loaded, self.creatures(), directory)
            return self._acquisition


def make_handler(context):
    """Create a handler with strict Host/Origin checks and allowlisted static resources."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Do not log request bodies, source data or the session token.
            print(fmt % args)

        def send_data(self, data: bytes, kind: str, status: int = 200):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def json(self, value, status=200):
            self.send_data(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'), 'application/json; charset=utf-8', status)

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            if not self.valid_host():
                self.json({'error': 'Host 不受信任。'}, 403)
                return
            try:
                with context.lock:
                    state = context.workspace if isinstance(context, SettingsController) else context
                    generation = context.generation if isinstance(context, SettingsController) else None
                path = urlsplit(self.path).path
                if path in ('/api/settings', '/api/settings/export') and isinstance(context, SettingsController):
                    self.json(context.export() if path.endswith('/export') else context.public())
                elif path == '/api/publication-result' and isinstance(context, SettingsController):
                    from xyj_content.publication import request_result
                    self.json(request_result(context.root,parse_qs(urlsplit(self.path).query).get('request_id',[''])[0]))
                elif path == '/api/bootstrap' and state is None:
                    from xyj_content.creature_auto import recovery_pending
                    self.json({'token': context.token, 'ready': False, 'setup_error': context.setup_error,
                               'publication_recovery_pending': recovery_pending(context.root),
                               'workspace_generation': generation})
                elif path.startswith('/api/') and state is None:
                    raise ContentError('尚未配置有效数据源，请打开设置并校验。')
                elif path == '/api/bootstrap':
                    with state.lock:
                        self.json({'token': state.token, 'items': state.items, 'fields': field_schema(),
                                   'sources': state.sources, 'revision': state.revision, 'edits': state.edits,
                                   'categories': category_options(), 'profiles': profile_options(), 'ready': True,
                                   'export_mode': 'automatic' if isinstance(context, SettingsController) else 'offline',
                                   'workspace_generation': generation})
                elif path == '/api/item-ids':
                    with state.lock:
                        state.load_creature_draft()
                        self.json({**item_id_availability(state.loaded, state.edits + state.creature_edits),
                                   'revision': state.revision, 'workspace_generation': generation})
                elif path == '/api/creatures/catalog':
                    self.json(state.creatures().metadata())
                elif path == '/api/creature-editor':
                    from xyj_content.creature_editor import ATTRIBUTES
                    from xyj_content.creatures import generation_chance
                    with state.lock:
                        state.load_creature_draft()
                        catalog = state.creatures()
                        self.json({'revision': state.creature_revision, 'edits': state.creature_edits,
                            'availability': item_id_availability(state.loaded, state.edits + state.creature_edits),
                            'maps': [{'id': k, 'name': v} for k, v in catalog.zones.items()],
                            'appearances': [{'id': k, 'name': catalog.definitions[k].name} for k in catalog.records],
                            'items': [{'id': k, 'name': v.name} for k,v in catalog.definitions.items() if v.type_code in (1,2,4,5,8)],
                            'skills': [{'id': k, 'name': v.name} for k,v in catalog.definitions.items() if v.type_code == 3],
                            'attributes': [{'key': k, 'name': v[0]} for k,v in ATTRIBUTES.items()],
                            'probabilities': [{'threshold': t, 'percent': generation_chance(t)} for t in range(-1,100)],
                            'native_verified': catalog.reset_coverage['available'],
                            'export_mode': 'automatic' if isinstance(context, SettingsController) else 'offline'})
                elif path == '/api/model-edit/open':
                    from xyj_content.model_workspace import open_document
                    query=parse_qs(urlsplit(self.path).query)
                    with state.lock:
                        self.json(open_document(state,int(query.get('template_id',['0'])[0]),query.get('reference',[None])[0],fresh=query.get('fresh',['0'])[0]=='1'))
                elif path == '/api/model-edit/blob-status':
                    from xyj_content.model_workspace import blob_path
                    query=parse_qs(urlsplit(self.path).query);key=query.get('id',[''])[0]
                    dest=blob_path(state.root,key);part=dest.with_suffix('.part')
                    self.json({'id':key,'bytes':dest.stat().st_size if dest.exists() else part.stat().st_size if part.exists() else 0,'complete':dest.exists()})
                elif path == '/api/model-edit/texture':
                    from xyj_content.model_workspace import blob_path,image_pixels,bundle_path,load_bundle,bundle_image
                    from xyj_content import native_texture
                    query=parse_qs(urlsplit(self.path).query)
                    tint=[float(x) for x in query.get('tint',['1,1,1'])[0].split(',')]
                    folder=None
                    if 'reference' in query:
                        folder=bundle_path(state.root,query['reference'][0]);bundle=load_bundle(folder,query['reference'][0])
                    if 'image' in query:
                        key=query['image'][0]
                        image = (bundle_image(folder, bundle['images'], key)
                                 if folder is not None and key in bundle['images'] else blob_path(state.root, key))
                        width,height,pixels=image_pixels(image)
                    else:
                        texture=int(query.get('texture',['-1'])[0])
                        if not 0<=texture<=99999:raise ContentError('贴图编号无效。')
                        relative=f'TEX/ALL{texture:05d}.TEX'
                        source=folder/'sources'/relative if folder is not None and relative in bundle['sources'] else state.client/'TEX'/f'ALL{texture:05d}.TEX'
                        width,height,pixels=native_texture.rgba(read_bytes(source))
                    self.send_data(native_texture.png(native_texture.encode(width,height,pixels,tint)),'image/png')
                elif path == '/api/creature-seeds':
                    query = parse_qs(urlsplit(self.path).query)
                    map_id = int(query.get('map', ['0'])[0])
                    source_id = int(query.get('source', ['0'])[0])
                    catalog = state.creatures()
                    direct = [r for r in catalog.roles.values() if r['map_id'] == r['revive_map_id']]
                    own = [r for r in direct if r['template_id'] == source_id]
                    rows = own + [r for r in direct if r['map_id'] == map_id and r['template_id'] != source_id]
                    if not rows:
                        rows = direct
                    self.json({'points': [{'key': r['key'], 'template_id': r['template_id'],
                                          'name': catalog.definitions[r['template_id']].name,
                                          'position': r['revive_position'], 'map_id': r['map_id'],
                                          'plots': r.get('plots')} for r in rows[:200]], 'total': len(rows)})
                elif path.startswith('/api/creature-edit/'):
                    from xyj_content.creature_editor import detail
                    template=int(path.rsplit('/',1)[1]);data=detail(state.loaded,template,state.creatures())
                    if isinstance(context,SettingsController):
                        from xyj_content.model_workspace import project_for
                        project=project_for(state,context.document,template)
                        if project:data['model_project']=project
                    self.json(data)
                elif path == '/api/acquisition/catalog':
                    self.json({**state.acquisition().public(), 'workspace_generation': generation})
                elif path == '/api/creatures':
                    query = parse_qs(urlsplit(self.path).query)
                    self.json(state.creatures().listing(query.get('q', [''])[0], query.get('map', ['all'])[0],
                                                        int(query.get('page', ['0'])[0]),
                                                        int(query['template_id'][0]) if 'template_id' in query else None))
                elif path.startswith('/api/creatures/'):
                    parts = path.strip('/').split('/')
                    if len(parts) not in (3, 4) or (len(parts) == 4 and parts[3] != 'model'):
                        raise ContentError('NPC 接口路径无效。')
                    template_id = int(parts[2])
                    query = parse_qs(urlsplit(self.path).query)
                    if len(parts) == 4:
                        role = query.get('role', [None])[0]
                        self.json(state.creatures().model(template_id, role))
                    else:
                        self.json(state.creatures().detail(template_id, query.get('map', ['all'])[0],
                                                           int(query.get('page', ['0'])[0])))
                elif path.startswith('/api/item/'):
                    parts = path.strip('/').split('/')
                    if len(parts) == 3:
                        self.json(item_detail(state.loaded, int(parts[2])))
                    elif len(parts) == 4 and parts[3] == 'drop-sources':
                        query = parse_qs(urlsplit(self.path).query)
                        self.json({**state.creatures().drop_sources(int(parts[2]), int(query.get('page', ['0'])[0])),
                                   'workspace_generation': generation})
                    else:
                        raise ContentError('道具接口路径无效。')
                elif path.startswith('/api/model/'):
                    _, record = find_unique(state.loaded.client, int(path.rsplit('/', 1)[1]))
                    query = parse_qs(urlsplit(self.path).query)
                    mode = query.get('mode', ['single'])[0]
                    if mode == 'equipped':
                        self.json(equipped_model(state.client, state.loaded.client, record, query.get('profile', ['auto'])[0]))
                    elif mode == 'single':
                        try:
                            model = item_model(state.client, record)
                            model['mode_label'] = '单件装备模型'
                        except ContentError:
                            model = item_model(state.client, record, display=True)
                            model['mode_label'] = '单件展示（落地外观）'
                            model['warnings'].insert(0, '这是该物品的落地展示外观，穿戴效果请切换人物穿戴。')
                        self.json(model)
                    else:
                        raise ContentError("预览方式无效。")
                elif path.startswith('/api/icon/') and path.endswith('.png'):
                    _, record = find_unique(state.loaded.client, int(path.rsplit('/', 1)[1][:-4]))
                    reference = icon_reference(state.client, record)
                    self.send_data(sprite_png(state.client, **reference), 'image/png')
                elif path.startswith('/api/sprite/') and path.endswith('.png'):
                    parts = path.split('/')
                    if len(parts) != 5:
                        raise ContentError("图像路径无效。")
                    self.send_data(sprite_png(state.client, int(parts[3]), int(parts[4][:-4])), 'image/png')
                elif path.startswith('/api/texture/') and path.endswith('.png'):
                    texture = int(path.rsplit('/', 1)[1][:-4])
                    self.send_data(texture_png(state.client, texture), 'image/png')
                else:
                    allowed = {'/': 'index.html', '/index.html': 'index.html', '/app.js': 'app.js',
                               '/creatures.js': 'creatures.js',
                               '/creature-editor.js': 'creature-editor.js',
                               '/model-editor.js':'model-editor.js', '/model-operations.js':'model-operations.js',
                               '/model-viewport.js':'model-viewport.js',
                               '/settings.js': 'settings.js',
                               '/item-workflow.js': 'item-workflow.js',
                               '/item-drops.js': 'item-drops.js',
                               '/acquisition.js': 'acquisition.js',
                               '/viewer.js': 'viewer.js', '/style.css': 'style.css',
                               '/vendor/three.module.js': 'node_modules/three/build/three.module.js',
                               '/vendor/three.core.js': 'node_modules/three/build/three.core.js',
                               '/vendor/OrbitControls.js': 'node_modules/three/examples/jsm/controls/OrbitControls.js'}
                    allowed['/vendor/TransformControls.js']='node_modules/three/examples/jsm/controls/TransformControls.js'
                    if path not in allowed:
                        self.json({'error': '路径不存在。'}, 404)
                        return
                    file = context.root / 'editor' / allowed[path]
                    if not file.resolve().is_relative_to(context.root / 'editor'):
                        raise ContentError("静态资源链接越界。")
                    data = read_bytes(file)
                    kind = 'text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
                    self.send_data(data, kind + ('; charset=utf-8' if kind.startswith('text/') else ''))
            except (ContentError, ValueError, UnicodeError, struct.error) as exc:
                self.json({'error': str(exc)}, 400)
            except Exception as exc:
                print(f'GET 失败：{type(exc).__name__}: {exc}', file=sys.stderr)
                self.json({'error': '读取失败，请查看编辑器控制台。'}, 500)

        def do_POST(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if (not self.valid_host() or self.headers.get('Origin') != origin or
                    not secrets.compare_digest(self.headers.get('X-Editor-Token', ''), context.token)):
                self.json({'error': '请求来源或会话令牌无效。'}, 403)
                return
            try:
                if self.headers.get('Content-Type') != 'application/json':
                    raise ContentError("仅接受 JSON 请求。")
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024 * 1024:
                    raise ContentError("请求体必须在 1 MiB 内。")
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ContentError("请求必须是对象。")
                if self.path.startswith('/api/settings/') and isinstance(context, SettingsController):
                    if self.path == '/api/settings/validate':
                        self.json(context.validate(value))
                    elif self.path == '/api/settings/save':
                        self.json(context.apply(value))
                    elif self.path == '/api/settings/browse':
                        if set(value) != {'kind'} or value['kind'] not in ('file', 'directory'):
                            raise ContentError('路径选择类型无效。')
                        try:
                            result = subprocess.run([sys.executable, str(context.root / 'tools/pick_path.py'), value['kind']],
                                                    capture_output=True, timeout=90,
                                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                            if result.returncode:
                                raise ContentError('Windows 路径选择不可用，请直接粘贴路径。')
                            self.json({'path': json.loads(result.stdout.decode('utf-8'))})
                        except subprocess.TimeoutExpired as exc:
                            raise ContentError('路径选择超时，可直接粘贴路径。') from exc
                    else:
                        self.json({'error': '设置接口不存在。'}, 404)
                    return
                if self.path == '/api/recover-publication' and isinstance(context, SettingsController):
                    if set(value) != {'server_stopped'} or value['server_stopped'] is not True:
                        raise ContentError('恢复前请确认相关游戏服务和客户端已停止。')
                    from xyj_content.creature_auto import recover_workspace
                    with context.lock:
                        self.json(recover_workspace(context,server_stopped=True))
                    return
                with context.lock:
                    state = context.workspace if isinstance(context, SettingsController) else context
                    if state is None:
                        raise ContentError('请先校验并保存数据源设置。')
                    if isinstance(context, SettingsController) and self.headers.get('X-Workspace-Generation') != str(context.generation):
                        raise ContentError('数据源已切换，请刷新页面后操作草稿。')
                    if self.path == '/api/export' and isinstance(context, SettingsController):
                        if set(value) != {'revision'}:
                            raise ContentError('覆盖请求格式无效。')
                        from xyj_content.deployment import apply_workspace
                        with state.lock:
                            self.json(apply_workspace(context, value['revision']))
                        return
                    if self.path == '/api/creature-export' and isinstance(context, SettingsController):
                        if not {'revision','server_stopped'}.issubset(value) or set(value)-{'revision','server_stopped','request_id'}:
                            raise ContentError('怪物自动发布请求格式无效。')
                        from xyj_content.creature_auto import apply_workspace
                        with state.lock:
                            self.json(apply_workspace(context,value['revision'],server_stopped=value['server_stopped'],request_id=value.get('request_id')))
                        return
                    self._write_workspace(state, value)
            except (ContentError, ValueError, UnicodeError, OSError) as exc:
                self.json({'error': str(exc)}, 400)
            except Exception as exc:
                # Never render unexpected exception text from settings or a database utility.
                print(f'POST 失败：{type(exc).__name__}', file=sys.stderr)
                self.json({'error': '操作失败，原配置保留。'}, 500)

        def _write_workspace(self, state, value):
            with state.lock:
                if self.path == '/api/model-edit/upload':
                    from xyj_content.model_workspace import upload
                    self.json(upload(state,value))
                elif self.path == '/api/model-edit/save':
                    from xyj_content.model_workspace import save_document
                    self.json(save_document(state,value))
                elif self.path == '/api/creature-draft':
                    self.json(state.save_creatures(value))
                elif self.path == '/api/creature-preview':
                    import math
                    if set(value)-{'model_ref'} != {'appearance_id', 'scale'} or type(value['appearance_id']) is not int:
                        raise ContentError('怪物预览参数无效。')
                    scale = value['scale']
                    if not isinstance(scale, list) or len(scale) != 3 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not .25 <= v <= 4 for v in scale):
                        raise ContentError('三轴比例须为 0.25 至 4。')
                    if value.get('model_ref'):
                        from xyj_content.model_workspace import preview_model
                        model=preview_model(state,value['model_ref'],value['appearance_id'])
                    else:model = state.creatures().model(value['appearance_id'])
                    model['action']['scale'] = [a*b for a,b in zip(model['action'].get('scale',[1,1,1]),scale)]
                    self.json(model)
                elif self.path == '/api/creature-export':
                    from xyj_content.creature_editor import export_patch
                    state.load_creature_draft()
                    if set(value) != {'revision'} or type(value['revision']) is not int or value['revision'] != state.creature_revision:
                        raise ContentError('怪物导出版本已过期，请重新读取草稿。')
                    if state.edits:
                        raise ContentError('请先完成道具草稿的发布或移除，防止两类补丁覆盖同一基线。')
                    self.json(export_patch(state.root, state.loaded, state.creature_edits, catalog=state.creatures(),
                                          npc_directory=state.npc_directory, client=state.client))
                elif self.path == '/api/draft':
                    self.json(state.save(value))
                elif self.path == '/api/export':
                    if set(value) != {'revision'} or type(value['revision']) is not int or value['revision'] != state.revision:
                        raise ContentError("导出版本已过期，请刷新。")
                    from xyj_content.acquisition import has_acquisition
                    self.json(export_editor_patch(state.root, state.loaded, state.edits,
                              acquisition=state.acquisition() if has_acquisition(state.edits) else None))
                else:
                    self.json({'error': '路径不存在。'}, 404)
    return Handler


class LocalServer(ThreadingHTTPServer):
    """Bind one exclusive loopback listener, preventing old and new handlers from sharing a Windows port."""

    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        """Fail clearly on a busy port instead of silently competing with another local editor."""
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    """Start the local editor, or verify an exported patch without starting a web server."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--server-object', type=Path)
    parser.add_argument('--port', type=int, default=18760)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--verify', type=Path)
    args = parser.parse_args()
    try:
        if args.verify:
            print(json.dumps(verify_editor_patch(ROOT, args.verify), ensure_ascii=False, indent=2))
            return 0
        settings = SettingsController(ROOT, lambda cfg: EditorState(ROOT, config=cfg), args.server_object)
        server = LocalServer(('127.0.0.1', args.port), make_handler(settings))
        url = f'http://127.0.0.1:{server.server_port}'
        print(f'轩辕剑道具编辑器：{url}\n本地备份与记录：{ROOT / "output/editor"}\n覆盖操作会写入已保存的双端目标；未配置或路径失效时请打开设置。\n按 Ctrl+C 退出。', flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return 0
    except KeyboardInterrupt:
        return 0
    except (ContentError, OSError) as exc:
        print(f'启动失败：{exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
