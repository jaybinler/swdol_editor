"""Serve a disposable authoring workspace; source game files are fingerprinted, never deployed."""

from pathlib import Path
import json
import shutil

from editor_server import EditorState, LocalServer, make_handler
from xyj_content.settings import default_config
from xyj_content.files import write_json, sha256
from xyj_content.patch import PAYLOAD_PATHS

ROOT = Path(__file__).resolve().parents[1]


from xyj_content.verification_inputs import paired_fixture


def main():
    """Run isolated HTTP acceptance until a workspace-local stop marker is created."""
    root = ROOT / 'output/editor/verification-0.12/browser'
    root.mkdir(parents=True, exist_ok=True)
    stop = root / 'stop'
    stop.unlink(missing_ok=True)
    source = paired_fixture(ROOT) / 'baseline'
    for relative in PAYLOAD_PATHS.values():
        target = root / relative; target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    shutil.copytree(ROOT / 'editor', root / 'editor', dirs_exist_ok=True, ignore=shutil.ignore_patterns('node_modules'))
    for name in ('build/three.module.js','build/three.core.js','examples/jsm/controls/OrbitControls.js'):
        target = root / 'editor/node_modules/three' / name; target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT / 'editor/node_modules/three' / name,target)
    protected = [ROOT/'swdol/Main/Object.dbf',ROOT/'swdol/Main/Language.DBF',ROOT/'swdol/Act/all_char.act',
                 ROOT/'output/editor/settings.json',ROOT/'XYJ_SEVVER/qingfeng/Object.dbf']
    protected += list((ROOT/'output/editor/workspaces').glob('*/draft.json'))
    protected += list((ROOT/'output/editor/workspaces').glob('*/creatures.json'))
    before = {str(p):sha256(p.read_bytes()) for p in protected}
    config = {**default_config(), 'server_object':str(root/PAYLOAD_PATHS['server_object']),
              'client_root':str(ROOT/'swdol'), 'client_object':str(root/PAYLOAD_PATHS['client_object']),
              'language':str(root/PAYLOAD_PATHS['client_language']),
              'npc_snapshot':str(ROOT/'output/editor/database-snapshots/985890871774effb-a45cc8c967781d68')}
    state = EditorState(root, config=config)
    server = LocalServer(('127.0.0.1',18762),make_handler(state)); server.timeout = .5
    print('Isolated creature acceptance: http://127.0.0.1:18762/?library=creatures&npc=20087',flush=True)
    try:
        while not stop.exists():server.handle_request()
    finally:
        server.server_close()
        after = {str(p):sha256(p.read_bytes()) for p in protected}
        write_json(root/'source-protection.json',{'before':before,'after':after,'unchanged':before==after})
        print('Source files unchanged:',before==after,flush=True)


if __name__ == '__main__':
    main()
