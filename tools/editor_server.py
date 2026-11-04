"""Loopback-only graphical item editor. Reads game data; writes only output/editor."""

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
        self.draft_path = output_path(self.root, Path('output/editor/draft.json'))
        legacy = self.draft_path
        if config is not None:
            identity = json.dumps({'hashes': self.hashes, 'paths': [str(p.resolve()) for p in (paths.server, paths.client, paths.language)]}, sort_keys=True)
            namespace = hashlib.sha256(identity.encode('utf-8')).hexdigest()
            self.draft_path = output_path(self.root, Path('output/editor/workspaces') / namespace / 'draft.json')
        self.edits, self.revision = [], 0
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
        revision = self.revision + 1
        write_json(self.draft_path, {'format': 'xyj-editor-draft/v1', 'sources': self.hashes,
                                    'revision': revision, 'edits': edits})
        self.edits, self.revision = edits, revision
        return {'revision': revision, 'edits': edits}


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
                elif path == '/api/bootstrap' and state is None:
                    self.json({'token': context.token, 'ready': False, 'setup_error': context.setup_error,
                               'workspace_generation': generation})
                elif path.startswith('/api/') and state is None:
                    raise ContentError('尚未配置有效数据源，请打开设置并校验。')
                elif path == '/api/bootstrap':
                    with state.lock:
                        self.json({'token': state.token, 'items': state.items, 'fields': field_schema(),
                                   'sources': state.sources, 'revision': state.revision, 'edits': state.edits,
                                   'categories': category_options(), 'profiles': profile_options(), 'ready': True,
                                   'workspace_generation': generation})
                elif path == '/api/item-ids':
                    with state.lock:
                        self.json({**item_id_availability(state.loaded, state.edits),
                                   'revision': state.revision, 'workspace_generation': generation})
                elif path == '/api/creatures/catalog':
                    self.json(state.creatures().metadata())
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
                               '/settings.js': 'settings.js',
                               '/item-workflow.js': 'item-workflow.js',
                               '/item-drops.js': 'item-drops.js',
                               '/viewer.js': 'viewer.js', '/style.css': 'style.css',
                               '/vendor/three.module.js': 'node_modules/three/build/three.module.js',
                               '/vendor/three.core.js': 'node_modules/three/build/three.core.js',
                               '/vendor/OrbitControls.js': 'node_modules/three/examples/jsm/controls/OrbitControls.js'}
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
                with context.lock:
                    state = context.workspace if isinstance(context, SettingsController) else context
                    if state is None:
                        raise ContentError('请先校验并保存数据源设置。')
                    if isinstance(context, SettingsController) and self.headers.get('X-Workspace-Generation') != str(context.generation):
                        raise ContentError('数据源已切换，请刷新页面后操作草稿。')
                    self._write_workspace(state, value)
            except (ContentError, ValueError, UnicodeError, OSError) as exc:
                self.json({'error': str(exc)}, 400)
            except Exception as exc:
                # Never render unexpected exception text from settings or a database utility.
                print(f'POST 失败：{type(exc).__name__}', file=sys.stderr)
                self.json({'error': '操作失败，原配置保留。'}, 500)

        def _write_workspace(self, state, value):
            with state.lock:
                if self.path == '/api/draft':
                    self.json(state.save(value))
                elif self.path == '/api/export':
                    if set(value) != {'revision'} or type(value['revision']) is not int or value['revision'] != state.revision:
                        raise ContentError("导出版本已过期，请刷新。")
                    self.json(export_editor_patch(state.root, state.loaded, state.edits))
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
        print(f'轩辕剑道具编辑器：{url}\n输出：{ROOT / "output/editor"}\n未配置或路径失效时，请打开设置。\n按 Ctrl+C 退出。', flush=True)
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
