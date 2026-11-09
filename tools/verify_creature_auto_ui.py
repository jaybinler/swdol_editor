"""Run the full GUI publication against copied game files and a TCP-disabled disposable database."""

from pathlib import Path
import json
import os
import shutil
import sys
import uuid

from editor_server import EditorState,LocalServer,make_handler
from xyj_content.settings import default_config
from xyj_content.files import write_json,read_json,sha256
from xyj_content.patch import PAYLOAD_PATHS
from xyj_content.verification_inputs import paired_fixture

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from creature_mysql_fixture import isolated_mysql


def main():
    """Confine all GUI writes to one verification workspace and the randomly named local test pipe."""
    parent=ROOT/'output/editor/verification-0.13';parent.mkdir(parents=True,exist_ok=True)
    root=parent/('browser-'+uuid.uuid4().hex[:8]);root.mkdir()
    (parent/'ui-workspace.txt').write_text(str(root),encoding='utf-8')
    protected=[ROOT/'swdol/Main/Object.dbf',ROOT/'swdol/Main/Language.DBF',ROOT/'swdol/Act/all_char.act',
               ROOT/'XYJ_SEVVER/qingfeng/Object.dbf',ROOT/'output/editor/settings.json']
    protected+=list((ROOT/'output/editor/workspaces').glob('*/draft.json'))
    protected+=list((ROOT/'output/editor/workspaces').glob('*/creatures.json'))
    real_settings=read_json(ROOT/'output/editor/settings.json')
    if real_settings['config'].get('server_target'):protected.append(Path(real_settings['config']['server_target']))
    before={str(p):sha256(p.read_bytes()) for p in protected}
    baseline=paired_fixture(ROOT)/'baseline'
    for relative in PAYLOAD_PATHS.values():
        p=root/relative;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(baseline/relative,p)
    client=root/'client';actor=client/'Act/all_char.act';actor.parent.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(ROOT/'swdol/Act/all_char.act',actor)
    shutil.copyfile(ROOT/'swdol/Act/all_item.act',actor.parent/'all_item.act')
    server=root/PAYLOAD_PATHS['server_object']
    shutil.copyfile(ROOT/'XYJ_SEVVER/qingfeng/NewLocalServer.exe',server.parent/'NewLocalServer.exe')
    shutil.copytree(ROOT/'editor',root/'editor',ignore=shutil.ignore_patterns('node_modules'))
    html=root/'editor/index.html';html.write_text(html.read_text(encoding='utf-8').replace('>本地工作区</span>','>隔离验收副本</span>'),encoding='utf-8')
    for name in ('build/three.module.js','build/three.core.js','examples/jsm/controls/OrbitControls.js'):
        target=root/'editor/node_modules/three'/name;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(ROOT/'editor/node_modules/three'/name,target)
    # The disposable skip-grants server accepts this fixed nonsecret placeholder, never the user's password.
    os.environ['XYJ_MYSQL_PASSWORD']='isolated-fixture'
    try:
        with isolated_mysql(ROOT,root/'mysql') as query:
            config={**default_config(),'client_root':str(client),'server_object':str(server),'server_target':str(server),
                    'npc_snapshot':str(ROOT/'output/editor/database-snapshots/985890871774effb-a45cc8c967781d68'),
                    'npc_mode':'database','map_dir':str(ROOT/'XYJ_SEVVER/qingfeng'),
                    'model_dir':str(ROOT/'swdol/MODEL'),'tex_dir':str(ROOT/'swdol/TEX'),'tsw_dir':str(ROOT/'swdol/Tsw')}
            config['database']={**config['database'],'client':str(ROOT/'XYJ_SEVVER/mysql/bin/mysql.exe'),
                                'transport':'pipe','host':'127.0.0.1','pipe':query.pipe_name,'user':'root','name':'swdol'}
            write_json(root/'output/editor/settings.json',{'format':'xyj-settings/v1','config':config,'validated_files':[]})
            from xyj_content.settings import SettingsController
            controller=SettingsController(root,lambda cfg:EditorState(root,config=cfg))
            if controller.workspace is None:raise RuntimeError(controller.setup_error)
            http=LocalServer(('127.0.0.1',18763),make_handler(controller));http.timeout=.5
            print('Isolated auto-publication: http://127.0.0.1:18763/?library=creatures&npc=20087',flush=True)
            print('Verification workspace: '+str(root),flush=True)
            try:
                while not (root/'stop').exists():http.handle_request()
            finally:http.server_close()
            count=query('SELECT COUNT(*) FROM npcpool WHERE OriginalObjectID=319;').strip()
            write_json(root/'database-result.json',{'template_id':319,'rows':int(count),'isolated_pipe_only':True})
    finally:
        after={str(p):sha256(p.read_bytes()) for p in protected}
        write_json(parent/'source-protection.json',{'before':before,'after':after,'unchanged':before==after})
        print('Protected real files unchanged: '+str(before==after),flush=True)


if __name__=='__main__':main()
