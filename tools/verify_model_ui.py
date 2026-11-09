"""Serve full model editing on a copied client/server, never starting a real database or game."""

from pathlib import Path
import json
import shutil
import uuid

from editor_server import EditorState,LocalServer,make_handler
from xyj_content.settings import SettingsController,default_config
from xyj_content.files import read_json,write_json,sha256
from xyj_content.patch import PAYLOAD_PATHS
from xyj_content.verification_inputs import paired_fixture
from xyj_content.model_workspace import ACT_FILES


def main():
    """Confine publication to a labelled disposable workspace and verify real source hashes on shutdown."""
    project=Path(__file__).resolve().parents[1];parent=project/'output/editor/verification-0.14';parent.mkdir(parents=True,exist_ok=True)
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--resume',type=Path);args=parser.parse_args()
    root=args.resume.resolve() if args.resume else parent/('browser-'+uuid.uuid4().hex[:8])
    if root.parent!=parent.resolve() or not root.name.startswith('browser-'):raise ValueError('Invalid isolated workspace')
    if not args.resume:root.mkdir()
    (parent/'ui-workspace.txt').write_text(str(root),encoding='utf-8')
    protected=[project/'swdol/Main/Object.dbf',project/'swdol/Main/Language.DBF',project/'swdol/Act/all_char.act',
               project/'swdol/MODEL/ALL_CHAR.IDX',project/'swdol/TEX/ALL_TEX.IDX',project/'swdol/MODEL/CHR00239.PLY',
               project/'XYJ_SEVVER/qingfeng/Object.dbf',project/'output/editor/settings.json']
    protected+=list((project/'output/editor/workspaces').glob('*/*.json'))
    before={str(p):sha256(p.read_bytes()) for p in protected}
    if args.resume:
        marker=root/'stop';marker.unlink(missing_ok=True)
        controller=SettingsController(root,lambda cfg:EditorState(root,config=cfg))
        if not controller.workspace:raise RuntimeError(controller.setup_error)
        _serve(root,parent,controller,protected,before)
        return
    baseline=paired_fixture(project)/'baseline'
    for relative in PAYLOAD_PATHS.values():
        p=root/relative;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(baseline/relative,p)
    client=root/'client';server=root/PAYLOAD_PATHS['server_object']
    for name in (*('Act/'+name for name in ACT_FILES),'MODEL/ALL_CHAR.IDX','TEX/ALL_TEX.IDX'):
        p=client/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(project/'swdol'/name,p)
    shutil.copyfile(project/'XYJ_SEVVER/qingfeng/NewLocalServer.exe',server.parent/'NewLocalServer.exe')
    shutil.copytree(project/'editor',root/'editor',ignore=shutil.ignore_patterns('node_modules'))
    for name in ('build/three.module.js','build/three.core.js','examples/jsm/controls/OrbitControls.js','examples/jsm/controls/TransformControls.js'):
        p=root/'editor/node_modules/three'/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(project/'editor/node_modules/three'/name,p)
    html=root/'editor/index.html';html.write_text(html.read_text(encoding='utf-8').replace('>本地工作区</span>','>隔离模型验收副本</span>'),encoding='utf-8')
    config={**default_config(),'client_root':str(client),'server_object':str(server),'server_target':str(server),
            'npc_snapshot':str(project/'output/editor/database-snapshots/985890871774effb-a45cc8c967781d68'),
            'map_dir':str(project/'XYJ_SEVVER/qingfeng'),'act_dir':str(project/'swdol/Act'),
            'model_dir':str(project/'swdol/MODEL'),'tex_dir':str(project/'swdol/TEX'),'tsw_dir':str(project/'swdol/Tsw')}
    write_json(root/'output/editor/settings.json',{'format':'xyj-settings/v1','config':config,'validated_files':[]})
    controller=SettingsController(root,lambda cfg:EditorState(root,config=cfg))
    if not controller.workspace:raise RuntimeError(controller.setup_error)
    # Original textures still referenced by unmodified materials must exist after activation switches roots.
    from xyj_content.model_workspace import open_document
    data=open_document(controller.workspace,20000)
    for model in data['models']:
        for material in model['document']['materials']:
            texture=material['texture']
            if texture>=0:
                p=client/'TEX'/f'ALL{texture:05d}.TEX';shutil.copyfile(project/'swdol/TEX'/p.name,p)
    _serve(root,parent,controller,protected,before)


def _serve(root,parent,controller,protected,before):
    """Serve one validated disposable workspace and record protection evidence on every shutdown."""
    service=LocalServer(('127.0.0.1',18764),make_handler(controller));service.timeout=.4
    print(json.dumps({'url':'http://127.0.0.1:18764/?library=creatures&npc=20000','fixture':str(root)},ensure_ascii=False),flush=True)
    try:
        while not (root/'stop').exists():service.handle_request()
    finally:
        service.server_close()
        after={str(p):sha256(p.read_bytes()) for p in protected}
        write_json(parent/'source-protection.json',dict(before=before,after=after,unchanged=before==after))
        if before!=after:raise RuntimeError('Protected original files changed')


if __name__=='__main__':main()
