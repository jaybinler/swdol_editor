"""Reviewed paired NPC publication with durable file and MyISAM recovery journals."""

from pathlib import Path
import uuid

from .binary import ContentError, read_bytes
from .files import atomic_write, output_path, read_json, write_json, sha256
from .patch import PAYLOAD_PATHS
from .plot_maps import NATIVE_SHA256
from .creature_assets import ACT_PATH
from .creature_editor import verify_patch
from .creature_database import apply_points, rollback_points, image, schema, matches_snapshot, number


def targets(root, config, manifest):
    """Resolve only explicit configured game locations and the fixed supported actor resource."""
    from .settings import input_path
    if not config.get('server_target'):
        raise ContentError('请在设置中保存服务端覆盖目标。')
    client = input_path(root, config['client_root'])
    result = dict(zip(PAYLOAD_PATHS.values(), (input_path(root, config['server_target']),
                    client / 'Main/Object.dbf', client / 'Main/Language.DBF')))
    from .model_compiler import resource_relative
    for relative,meta in manifest.get('resources',{}).items():
        resource_relative(relative)
        target=client/relative[7:]
        if not target.resolve().is_relative_to(client.resolve()):raise ContentError('资源目标通过链接离开客户端根目录。')
        if meta.get('source_sha256') is None and relative.endswith('.IDX'):
            raise ContentError('模型和贴图索引必须已有可核验基线。')
        result[relative]=target
    resolved = [p.resolve() for p in result.values()]
    if len(set(resolved)) != len(resolved) or any(not p.is_file() for rel,p in result.items()
        if rel not in manifest.get('resources',{}) or manifest['resources'][rel].get('source_sha256') is not None):
        raise ContentError('怪物发布目标缺失或存在路径冲突。')
    if sha256(read_bytes(result[PAYLOAD_PATHS['server_object']].parent / 'NewLocalServer.exe')) != NATIVE_SHA256:
        raise ContentError('实际服务端程序版本不匹配，已停止发布。')
    return result


def recover(root, config, query, directory):
    """Recover this publisher's known targets only; protect later external file and row changes."""
    directory = output_path(root, Path(directory))
    journal = read_json(directory / 'journal.json')
    package = Path(journal['package'])
    if sha256(read_bytes(package / 'manifest.json')) != journal['manifest_sha256']:
        raise ContentError('恢复对应补丁清单已改变。')
    verify_patch(package)
    expected = targets(root, config, read_json(package / 'manifest.json'))
    for entry in reversed(journal['files']):
        relative = entry['relative']
        if relative not in expected or str(expected[relative].resolve()) != entry['target']:
            raise ContentError('恢复日志目标与已保存设置不一致。')
        original = read_bytes(directory / 'files' / relative)
        if sha256(original) != entry['before']:
            raise ContentError('发布前备份校验失败。')
        actual = sha256(read_bytes(expected[relative]))
        if actual == entry['before']:
            continue
        if actual != entry['after']:
            raise ContentError('游戏文件被外部修改，已保留备份并停止自动恢复。')
        atomic_write(expected[relative], original)
        if sha256(read_bytes(expected[relative])) != entry['before']:
            raise ContentError('恢复文件读回不一致。')
    if (directory / 'npc-journal.json').exists():
        rollback_points(query, directory / 'npc-journal.json')
    journal['phase'] = 'rolled_back'; write_json(directory / 'journal.json', journal)
    return {'status': 'rolled_back', 'journal': str(directory)}


def apply(root, config, query, package):
    """Apply one verified package with servers stopped; rollback ordinary failures and retain crash recovery."""
    from .deployment import deployment_lock
    package = Path(package).resolve()
    verification = verify_patch(package)
    manifest = read_json(package / 'manifest.json')
    if set(manifest.get('resources',{}))-{ACT_PATH}:
        raise ContentError('完整模型补丁须通过当前工坊自动发布器执行。')
    destinations = targets(root, config, manifest)
    plan = read_json(package / 'npc-points.json')['operations']
    parent = output_path(root, Path('output/editor/creature-deployments'))
    with deployment_lock(root):
        for pending in parent.glob('*/journal.json') if parent.exists() else ():
            if read_json(pending).get('phase') not in ('committed', 'rolled_back'):
                raise ContentError(f'存在未完成发布，请先恢复：{pending.parent}')
        for relative, path in destinations.items():
            if read_bytes(path) != read_bytes(package / 'baseline' / relative):
                raise ContentError(f'真实目标与补丁基线不一致：{relative}')
        # Preflight every point before changing either game files or the first database row.
        if plan:
            columns = schema(query)
            for operation in plan:
                p = operation['point']; table, role = p['key'].split(':')
                row = image(query, table, int(role), columns[table])
                if not matches_snapshot(row, p['expected']) or number(row, 'ObjectType') != 0:
                    raise ContentError('点位来源已经变化，请重新采集快照。')
                if any(number(row, k) not in (-1, 0) for k in ('OwnerDBID','AccountID','LaborID')):
                    raise ContentError('有主存档不能作为普通刷新点修改。')
        directory = parent / uuid.uuid4().hex
        files = []
        for relative, path in destinations.items():
            before, after = read_bytes(path), read_bytes(package / relative)
            atomic_write(directory / 'files' / relative, before)
            files.append({'relative': relative, 'target': str(path.resolve()), 'before': sha256(before), 'after': sha256(after)})
        journal = {'format': 'xyj-creature-deploy/v1', 'phase': 'prepared', 'package': str(package),
                   'manifest_sha256': sha256(read_bytes(package / 'manifest.json')), 'files': files}
        write_json(directory / 'journal.json', journal)
        try:
            if plan:
                apply_points(query, plan, directory / 'npc-journal.json')
            journal['phase'] = 'applying_files'; write_json(directory / 'journal.json', journal)
            for entry in files:
                path = Path(entry['target'])
                if sha256(read_bytes(path)) != entry['before']:
                    raise ContentError('文件在发布过程中被其他程序修改。')
                atomic_write(path, read_bytes(package / entry['relative']))
                if sha256(read_bytes(path)) != entry['after']:
                    raise ContentError('文件发布读回核验失败。')
            journal['phase'] = 'committed'; write_json(directory / 'journal.json', journal)
        except Exception as exc:
            journal['phase'] = 'recovery_required'; write_json(directory / 'journal.json', journal)
            try:
                recover(root, config, query, directory)
            except Exception as recovery_error:
                raise ContentError(f'发布中断，需恢复：{directory}；恢复未完成：{recovery_error}') from exc
            raise ContentError(f'发布失败，已恢复原文件与存档：{exc}') from exc
        return {'status': 'committed_runtime_pending', 'journal': str(directory), 'verification': verification,
                'next': '重新打开工坊设置，校验并保存数据源以更新怪物快照和工作区；再启动游戏进行验收。旧草稿保留在原基线工作区。'}
