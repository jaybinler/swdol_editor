"""Exercise database settings against disposable copies of NPC tables, with no TCP listener."""

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import time
import uuid

from editor_server import EditorState
from xyj_content.settings import SettingsController, default_config
from xyj_content.files import write_json


def main():
    """Start only a local isolated reader, validate/save its projection, and stop that child in all cases."""
    root = Path(__file__).resolve().parents[1]
    destination = root / 'output/settings-database-check' / uuid.uuid4().hex
    data = destination / 'data/swdol'
    data.mkdir(parents=True)
    temporary = destination / 'tmp'
    temporary.mkdir()
    sources = sorted((root / 'output/npc-investigation-20261103/npc-source').glob('npcpool*'))
    sources = [p for p in sources if p.suffix.lower() in ('.frm', '.myd', '.myi')]
    assert len(sources) == 6
    hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    for source in sources:
        shutil.copyfile(source, data / source.name)
    # Three.js is inspected during validation; this small fixture needs just these files.
    for relative in ('editor/node_modules/three/build/three.module.js', 'editor/node_modules/three/build/three.core.js',
                     'editor/node_modules/three/examples/jsm/controls/OrbitControls.js'):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    pipe = 'CodexSettings' + uuid.uuid4().hex[:12]
    mysql = root / 'XYJ_SEVVER/mysql'
    args = [str(mysql / 'bin/mysqld-nt.exe'), '--no-defaults', '--standalone', '--console',
            '--basedir=' + str(mysql), '--datadir=' + str(data.parent), '--skip-grant-tables', '--skip-networking',
            '--enable-named-pipe', '--socket=' + pipe, '--skip-innodb', '--skip-bdb',
            '--pid-file=' + str(destination / 'reader.pid'), '--tmpdir=' + str(temporary),
            '--log-error=' + str(destination / 'reader.err')]
    log = (destination / 'reader.log').open('wb')
    process = subprocess.Popen(args, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 15
        while not any(b'ready for connections' in path.read_bytes() for path in destination.glob('reader.*') if path.suffix in ('.log', '.err')):
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('隔离数据库未能启动；请检查本次 reader 日志。')
            time.sleep(.2)
        config = default_config()
        for key in ('client_root', 'server_object', 'map_dir', 'npc_snapshot'):
            config[key] = str(root / config[key])
        config['npc_mode'] = 'database'
        config['remember_password'] = True
        config['database'].update(client=str(mysql / 'bin/mysql.exe'), user='root', transport='pipe', pipe=pipe)
        factory = lambda cfg: EditorState(destination, config=cfg)
        controller = SettingsController(destination, factory)
        synthetic = 'fixture-password-only-123'
        result = controller.validate({'config': config, 'password': synthetic})
        if not result['valid']:
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        controller.apply({'config': result['config'], 'receipt': result['receipt']})
        reopened = SettingsController(destination, factory)
        assert reopened.workspace is not None, reopened.setup_error
        assert reopened.public()['password_state'] == 'saved'
        assert reopened._saved_password(reopened.config) == synthetic
        assert synthetic not in json.dumps(reopened.export())
        assert synthetic.encode() not in controller.path.read_bytes()
        meta = reopened.workspace.creatures().metadata()
        assert meta['instances'] == 10429, meta
        after = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
        assert after == hashes
        report = {'passed': True, 'transport': 'local named pipe; networking disabled', 'instances': meta['instances'],
                  'templates': meta['templates'], 'checks': result['checks'], 'source_hashes_unchanged': hashes,
                  'production_database_accessed': False, 'fixture': str(destination.relative_to(root))}
        report['password_protected_and_export_excludes_secret'] = True
        write_json(root / 'research/2026-11-03/settings_database_verification.json', report)
        print(json.dumps({key: report[key] for key in ('passed','instances','templates','production_database_accessed')}, ensure_ascii=False))
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        log.close()


if __name__ == '__main__':
    main()
