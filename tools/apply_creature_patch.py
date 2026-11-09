"""Verify or publish an NPC package; real writes require an explicit stopped-server acknowledgement."""

from argparse import ArgumentParser
from pathlib import Path
import json
import sys

from xyj_content.binary import ContentError
from xyj_content.files import read_json
from xyj_content.creature_editor import verify_patch

ROOT = Path(__file__).resolve().parents[1]


def database_query(document):
    """Keep the historical CLI helper while sharing the private client with GUI publication."""
    from xyj_content.creature_connection import database_query as connect
    return connect(ROOT,document)


def main():
    """Default to read-only verification; require a deliberate CLI flag for publication or recovery."""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--patch', type=Path)
    parser.add_argument('--apply', action='store_true', help='执行真实发布；默认仅校验')
    parser.add_argument('--recover', type=Path, help='恢复已记录的发布目录')
    parser.add_argument('--server-stopped', action='store_true', help='确认相关游戏服务和客户端已停止')
    args = parser.parse_args()
    try:
        if args.apply or args.recover:
            if not args.server_stopped:
                raise ContentError('真实发布或恢复前，请停止所有使用该 NPC 数据库的游戏服务及客户端，再显式传入 --server-stopped。')
            document = read_json(ROOT / 'output/editor/settings.json')
            if args.apply and args.patch:
                verify_patch(args.patch.resolve())
                from xyj_content.publication import assert_unpublished
                assert_unpublished(ROOT,args.patch.resolve())
            from editor_server import EditorState
            from xyj_content.settings import SettingsController
            from xyj_content.creature_auto import apply_workspace, recover_workspace
            controller=SettingsController(ROOT,lambda config:EditorState(ROOT,config=config))
            if args.recover and (args.recover/'manifest.json').exists():
                if args.recover.resolve() != (ROOT/'output/editor/backups/latest').resolve():
                    raise ContentError('统一恢复仅接受程序的 latest 备份目录。')
                result=recover_workspace(controller,server_stopped=True,allow_committed=True)
            elif args.recover:
                config,query=database_query(document)
                from xyj_content.creature_deployment import recover
                result=recover(ROOT,config,query,args.recover)
            elif args.patch:
                verify_patch(args.patch.resolve())
                state=controller.workspace
                if state is None:raise ContentError(controller.setup_error)
                state.load_creature_draft()
                edits=read_json(args.patch/'manifest.json')['edits']
                if state.creature_edits and state.creature_edits != edits:
                    raise ContentError('当前怪物草稿与补丁不同，请先处理已有草稿。')
                state.creature_edits=edits
                result=apply_workspace(controller,state.creature_revision,server_stopped=True,package=args.patch.resolve())
            else:
                raise ContentError('请选择 --patch 补丁目录。')
        elif args.patch:
            result = verify_patch(args.patch.resolve())
        else:
            raise ContentError('请选择 --patch 补丁目录；默认只读校验。')
        print(json.dumps(result,ensure_ascii=False,indent=2)); return 0
    except (ContentError,OSError,ValueError,KeyError,TypeError) as exc:
        print('怪物补丁操作失败：'+str(exc),file=sys.stderr); return 1


if __name__ == '__main__':
    raise SystemExit(main())
