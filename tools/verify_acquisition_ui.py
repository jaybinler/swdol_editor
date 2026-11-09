"""Serve an isolated acquisition acceptance workspace; never deploy into original game directories."""

from argparse import ArgumentParser
from pathlib import Path
import json
import os
import shutil
import tempfile

from editor_server import EditorState, LocalServer, make_handler
from xyj_content.files import output_path, sha256, write_json
from xyj_content.patch import PAYLOAD_PATHS
from xyj_content.settings import SettingsController, default_config


from xyj_content.verification_inputs import paired_fixture


def main():
    """Copy paired fixtures and web assets, then expose the real editor against disposable targets."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18761)
    parser.add_argument('--out', type=Path, default=Path('output/editor/verification-0.11'))
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    output = output_path(project, args.out)
    output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='browser-', dir=output)).resolve()
    baseline = paired_fixture(project) / 'baseline'
    for relative in PAYLOAD_PATHS.values():
        target = root/relative; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(baseline/relative, target)
    region = root/'server/qingfeng'
    shutil.copytree(project/'XYJ_SEVVER/qingfeng/ASM', region/'ASM')
    shutil.copyfile(project/'XYJ_SEVVER/qingfeng/NewLocalServer.exe', region/'NewLocalServer.exe')
    shutil.copytree(project/'editor', root/'editor', ignore=shutil.ignore_patterns('node_modules'))
    for relative in ('build/three.module.js', 'build/three.core.js', 'examples/jsm/controls/OrbitControls.js'):
        target = root/'editor/node_modules/three'/relative; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(project/'editor/node_modules/three'/relative, target)
    server_hash = sha256((region/'Object.dbf').read_bytes())
    snapshots = [project/'output/editor'/p for p in ('npc-snapshot-script-v3-final','npc-snapshot-script-v3','npc-snapshot')]
    snapshot = next(p for p in snapshots if json.loads((p/'manifest.json').read_text(encoding='utf-8'))['server_object_sha256'] == server_hash)
    config = {**default_config(), 'client_root':str(root/'client'), 'server_object':str(region/'Object.dbf'),
              'server_target':str(region/'Object.dbf'), 'map_dir':str(region), 'npc_snapshot':str(snapshot),
              **{f'{key}_dir':str(project/'swdol'/folder) for key,folder in
                 (('act','Act'),('model','MODEL'),('tex','TEX'),('tsw','Tsw'))}}
    write_json(root/'output/editor/settings.json', {'format':'xyj-settings/v1','config':config,'validated_files':[]})
    controller = SettingsController(root, lambda cfg: EditorState(root, config=cfg))
    if controller.workspace is None:
        raise RuntimeError(controller.setup_error)
    server = LocalServer(('127.0.0.1', args.port), make_handler(controller))
    write_json(output/'browser-session.json', {'root':str(root),'url':f'http://127.0.0.1:{server.server_port}',
               'pid':os.getpid(),'source_server_sha256':server_hash,'snapshot':str(snapshot)})
    print(f'Isolated editor: http://127.0.0.1:{server.server_port}\nTargets: {root}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
