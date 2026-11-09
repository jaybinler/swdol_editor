"""Version-pinned NPC authoring; edits are applied to copies and exported for review."""

from dataclasses import replace
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import math
import struct

from .binary import ContentError, decode_table, encode_table, read_bytes
from .catalog import Loaded, Inputs, load_inputs, source_hashes
from .files import atomic_write, output_path, read_json, sha256, write_json
from .language import encode_language, decode_language
from .objects import encode_fixed_text, find_unique, view_record
from .patch import PAYLOAD_PATHS, insertion_position
from .plot_maps import NATIVE_SHA256

FORMAT = 'xyj-creature-draft/v1'
PATCH_FORMAT = 'xyj-creature-patch/v1'
# SetNPCInitData copies these eight signed words to the runtime base attributes.
ATTRIBUTES = {'constitution': ('体质', 86), 'strength': ('力量', 88),
              'stamina': ('耐力', 90), 'agility': ('敏捷', 92),
              'dexterity': ('灵巧', 94), 'intelligence': ('智慧', 96),
              'charisma': ('魅力', 98), 'luck': ('运气', 100)}
MAX_TICKS = 18_000_000  # 119 * ticks must remain inside the native signed i32 multiply.


def integer(value, low, high, label):
    """Reject booleans, fractional values and native integer overflow."""
    if type(value) is not int or not low <= value <= high:
        raise ContentError(f'{label}必须是 {low} 至 {high} 的整数。')
    return value


def respawn(value) -> dict:
    """Convert seconds to native 100 ms ticks without silently rounding a user request."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ContentError('基础间隔必须是秒数。')
    try:
        ticks = Decimal(str(value)) * 10
        if not ticks.is_finite() or ticks != ticks.to_integral_value() or not 7 <= ticks <= MAX_TICKS:
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise ContentError('基础间隔须为 0.7 至 1800000 秒，精度为 0.1 秒。') from None
    ticks = int(ticks)
    return {'ticks': ticks, 'seconds': ticks / 10,
            'minimum_seconds': (ticks * 80 // 100) / 10,
            'maximum_seconds': (ticks * 119 // 100) / 10,
            'corpse_seconds': 60, 'default_tick_ms': 100}


def creature_record(loaded, object_id):
    """Select one role template on both ends; never reinterpret an item or skill as an NPC."""
    sr, server = find_unique(loaded.server, object_id)
    cr, client = find_unique(loaded.client, object_id)
    if any(struct.unpack_from('<H', row, 8)[0] != 0 for row in (server, client)):
        raise ContentError('只能新增或编辑角色模板，不能覆盖道具或技能。')
    return server, client, view_record(server, sr), view_record(client, cr, loaded.language)


def detail(loaded, object_id, catalog=None):
    """Expose raw slots and field ownership, preserving missing references and repeated skills."""
    server, client, sv, cv = creature_record(loaded, object_id)
    ticks = struct.unpack_from('<i', server, 332)[0]
    definitions = {struct.unpack_from('<I', r, 4)[0]: view_record(r, i)
                   for i, r in enumerate(loaded.server.records) if struct.unpack_from('<I', r, 4)[0]}
    drops = []
    from .creatures import generation_chance
    for slot, (item, threshold) in enumerate(zip(struct.unpack_from('<12h', server, 254),
                                                struct.unpack_from('<12h', server, 278))):
        drops.append({'slot': slot, 'id': item, 'threshold': threshold,
                      'name': definitions[item].name if item in definitions else '',
                      'percent': generation_chance(threshold)})
    skills = list(struct.unpack_from('<16h', server, 382))
    editable = server[60:64] == client[60:64] and server[74:76] == client[74:76] and server[80] == client[80]
    return {'source_id': object_id, 'target_id': object_id, 'name': sv.name, 'description': sv.description,
            'level': struct.unpack_from('<h', server, 74)[0], 'sex': server[80],
            'appearance_id': object_id, 'scale': [1, 1, 1],
            'respawn_seconds': ticks / 10,
            'timing': respawn(ticks / 10) if 7 <= ticks <= MAX_TICKS else None,
            'attributes': {k: struct.unpack_from('<h', server, offset)[0] for k, (_, offset) in ATTRIBUTES.items()},
            'drops': drops, 'skills': skills, 'reset_candidates': list(struct.unpack_from('<5h', server, 516)),
            'spawns': [], 'editable': editable,
            'readonly_reason': '' if editable else '双端外观、性别或等级基线不一致，请先核对来源。',
            'client_name': cv.name,
            'points': [{'key': p['key'], 'map_id': p['map_id'], 'revive_map_id': p['revive_map_id'],
                        'position': list(p['revive_position']), 'plots': p.get('plots')}
                       for p in (catalog.instances[object_id] if catalog else [])]}


def normalize_spawns(value, target, source, catalog):
    """Validate explicit point mutations against immutable saved keys; quantity expands to separate rows."""
    if not isinstance(value, list) or len(value) > 200:
        raise ContentError('每个怪物最多配置 200 项刷新点修改。')
    result, seen, count = [], set(), 0
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {'action', 'key', 'map_id', 'position', 'count', 'direction'}:
            raise ContentError('刷新点字段无效。')
        action, key = entry['action'], entry['key']
        if action not in ('add', 'update', 'remove') or not isinstance(key, str) or key not in catalog.roles:
            raise ContentError('刷新点必须选择已采集的存档作为来源。')
        old = catalog.roles[key]
        if action != 'add' and (old['template_id'] != source or source != target):
            raise ContentError('只能修改当前原编号的直接存档；新增变体须新增自己的刷新点。')
        if action != 'add' and key in seen:
            raise ContentError('同一刷新点不能重复修改。')
        seen.add(key)
        zone = integer(entry['map_id'], 0, 32767, '地图编号')
        if zone not in catalog.zones:
            raise ContentError('刷新点地图不存在。')
        position = entry['position']
        if (not isinstance(position, list) or len(position) != 3 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 1e7 for v in position)):
            raise ContentError('刷新坐标必须是三个有限数值，绝对值不超过 10000000。')
        quantity = integer(entry['count'], 1, 50, '刷新数量')
        if action != 'add' and quantity != 1:
            raise ContentError('已有点一次对应一个存档；增加数量请选择新增点。')
        direction = entry['direction']
        if isinstance(direction, bool) or not isinstance(direction, (int, float)) or not math.isfinite(direction) or not 0 <= direction < 360:
            raise ContentError('朝向必须是 0 至不足 360 度。')
        count += quantity
        if count > 500:
            raise ContentError('单个草稿最多涉及 500 个存档对象。')
        result.append({**entry, 'position': list(position), 'expected': {
            'template_id': old['template_id'], 'map_id': old['map_id'],
            'revive_map_id': old['revive_map_id'], 'position': list(old['revive_position'])}})
        if 'plots' in old:
            result[-1]['expected']['plots'] = list(old['plots'])
    return result


def validate(loaded, edits, catalog=None, reserved=()):
    """Validate complete bounded NPC operations without changing files, catalog state or raw records."""
    if not isinstance(edits, list) or len(edits) > 100:
        raise ContentError('怪物草稿最多包含 100 个操作。')
    occupied = {struct.unpack_from('<I', r, 4)[0] for t in (loaded.server, loaded.client) for r in t.records}
    counts = Counter(struct.unpack_from('<I', r, 4)[0] for r in loaded.server.records)
    definitions = {struct.unpack_from('<I', r, 4)[0]: struct.unpack_from('<H', r, 8)[0]
                   for r in loaded.server.records if counts[struct.unpack_from('<I',r,4)[0]] == 1}
    targets, result = set(reserved), []
    required = {'source_id', 'target_id', 'name', 'description', 'level', 'sex', 'respawn_seconds',
                'appearance_id', 'scale', 'attributes', 'drops', 'skills', 'reset_candidates', 'spawns'}
    for edit in edits:
        if not isinstance(edit, dict) or set(edit)-{'model_ref'} != required:
            raise ContentError('怪物操作字段不完整或含未支持字段。')
        if 'model_ref' in edit:
            from .model_workspace import HASH
            if not isinstance(edit['model_ref'],str) or not HASH.fullmatch(edit['model_ref']):
                raise ContentError('独立模型草稿编号无效。')
        source = integer(edit['source_id'], 1, 32767, '来源编号')
        target = integer(edit['target_id'], 1, 32767, '目标编号')
        if target in targets or target != source and target in occupied:
            raise ContentError(f'怪物编号 {target} 已占用或与其他草稿冲突。')
        targets.add(target)
        old = detail(loaded, source)
        if not old['editable']:
            raise ContentError(old['readonly_reason'])
        if not isinstance(edit['name'], str) or not edit['name'].strip() or not isinstance(edit['description'], str):
            raise ContentError('怪物名称不能为空，说明须为文本。')
        # NPC native initialization copies only thirteen bytes, including the terminator.
        encode_fixed_text(edit['name'], 13)
        encode_fixed_text(edit['description'], 200)
        integer(edit['level'], 1, 100, '等级')
        integer(edit['sex'], 0, 1, '性别编码')
        timing = respawn(edit['respawn_seconds'])
        creature_record(loaded, integer(edit['appearance_id'], 1, 32767, '外观模板编号'))
        scale = edit['scale']
        if (not isinstance(scale, list) or len(scale) != 3 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not .25 <= v <= 4 for v in scale)):
            raise ContentError('外观三轴比例须为 0.25 至 4 的有限数值。')
        attrs = edit['attributes']
        if not isinstance(attrs, dict) or set(attrs) != set(ATTRIBUTES):
            raise ContentError('基础属性字段不完整。')
        for k, v in attrs.items():
            integer(v, -32768, 32767, ATTRIBUTES[k][0])
        drops = edit['drops']
        if not isinstance(drops, list) or len(drops) != 12:
            raise ContentError('须完整保留 12 个掉落槽。')
        active, normalized_drops = 0, []
        for slot, drop in enumerate(drops):
            if not isinstance(drop, dict) or set(drop) != {'id', 'threshold'}:
                raise ContentError('掉落槽字段无效。')
            item = integer(drop['id'], -1, 32767, '掉落物品编号')
            threshold = integer(drop['threshold'], -32768, 32767, '掉落阈值')
            unchanged = (item, threshold) == (old['drops'][slot]['id'], old['drops'][slot]['threshold'])
            if not unchanged and item not in (-1, 0) and definitions.get(item) not in (1, 2, 4, 5, 8):
                raise ContentError('掉落引用必须是已有物品，不能选择怪物或技能。')
            if not unchanged and not -1 <= threshold <= 99:
                raise ContentError('修改后的掉率须为原生可表达的 0% 至 100%。')
            active += item > 0 and threshold >= 0
            normalized_drops.append(dict(drop))
        if active > 8 and normalized_drops != [{'id': d['id'], 'threshold': d['threshold']} for d in old['drops']]:
            raise ContentError('修改后的有效掉落不能超过 8 个，避免原生容量抑制后续掉落。')
        skills = edit['skills']
        if not isinstance(skills, list) or len(skills) != 16:
            raise ContentError('须完整保留 16 个技能槽。')
        for slot, skill in enumerate(skills):
            integer(skill, -1, 32767, '技能编号')
            if skill > 0 and definitions.get(skill) != 3 and skill != old['skills'][slot]:
                raise ContentError('技能引用不存在或不是技能定义。')
        candidates = edit['reset_candidates']
        if not isinstance(candidates, list) or len(candidates) != 5:
            raise ContentError('须保留五个刷新候选。')
        for item in candidates:
            integer(item, -1, 32767, '刷新候选编号')
            if item != -1 and item != target and definitions.get(item) != 0:
                raise ContentError('刷新候选必须是自身或已有角色模板。')
        if edit['spawns'] and catalog is None:
            raise ContentError('地图编辑需要经过核验的 NPC 快照。')
        spawns = normalize_spawns(edit['spawns'], target, source, catalog) if edit['spawns'] else []
        result.append({**edit, 'respawn_seconds': timing['seconds'], 'scale': list(scale),
                       'drops': normalized_drops, 'skills': list(skills),
                       'reset_candidates': list(candidates), 'attributes': dict(attrs),
                       'spawns': [{k: v for k, v in point.items() if k != 'expected'} for point in spawns]})
    return result


def prepare(loaded, edits, *, catalog=None, appearances=None, model_selectors=None):
    """Apply only documented NPC fields to both tables; drop, skills and timing belong to the server."""
    edits = validate(loaded, edits, catalog)
    server, client, language, changes = loaded.server, loaded.client, loaded.language, []
    for edit in edits:
        source, target = edit['source_id'], edit['target_id']
        sr, cr, _, _ = creature_record(loaded, source)
        ns, nc = bytearray(sr), bytearray(cr)
        old = detail(loaded, source)
        for row in (ns, nc):
            struct.pack_into('<I', row, 4, target)
            struct.pack_into('<h', row, 74, edit['level'])
            row[80] = edit['sex']
            for key, (_, offset) in ATTRIBUTES.items():
                struct.pack_into('<h', row, offset, edit['attributes'][key])
        for key, start, width in [('name', 12, 16), ('description', 600, 200)]:
            if target != source or edit[key] != old[key]:
                ns[start:start+width] = encode_fixed_text(edit[key], width)
                struct.pack_into('<I', nc, start, len(language.entries))
                language = language.append_texts(edit[key])
        appearance_server, appearance_client, _, _ = creature_record(loaded, edit['appearance_id'])
        for row, appearance in [(ns, appearance_server), (nc, appearance_client)]:
            row[60:64] = appearance[60:64]
            row[218:254] = appearance[218:254]
            if appearances and target in appearances:
                struct.pack_into('<H', row, 60, appearances[target])
            if model_selectors and target in model_selectors:
                struct.pack_into('<H',row,62,model_selectors[target])
        struct.pack_into('<i', ns, 332, respawn(edit['respawn_seconds'])['ticks'])
        struct.pack_into('<12h', ns, 254, *(d['id'] for d in edit['drops']))
        struct.pack_into('<12h', ns, 278, *(d['threshold'] for d in edit['drops']))
        struct.pack_into('<16h', ns, 382, *edit['skills'])
        struct.pack_into('<5h', ns, 516, *edit['reset_candidates'])
        for side, new in [('server', ns), ('client', nc)]:
            table = server if side == 'server' else client
            if source == target:
                index, _ = find_unique(table, source)
                table = replace(table, records=table.records[:index] + (bytes(new),) + table.records[index+1:])
            else:
                table = table.insert(insertion_position(table, 0), bytes(new))
            if side == 'server': server = table
            else: client = table
        changes.append({'source_id': source, 'target_id': target, 'name': edit['name'],
                        'new': source != target, 'server_changed_bytes': [i for i in range(832) if ns[i] != sr[i]],
                        'client_changed_bytes': [i for i in range(832) if nc[i] != cr[i]],
                        'timing': respawn(edit['respawn_seconds']), 'spawns': edit['spawns']})
    return {'server_object': encode_table(server), 'client_object': encode_table(client),
            'client_language': encode_language(language)}, changes


def verify_patch(directory: Path):
    """Independently rebuild every NPC table, resource and point plan from embedded immutable sources."""
    from .creatures import CreatureCatalog
    from .creature_assets import build_variants, ACT_PATH
    from .creature_database import point_plan
    manifest = read_json(directory / 'manifest.json')
    if manifest.get('format') != PATCH_FORMAT or manifest.get('native_sha256') != NATIVE_SHA256:
        raise ContentError('怪物补丁格式或原生版本不受支持。')
    paths = []
    for key, relative in PAYLOAD_PATHS.items():
        path = directory / 'baseline' / relative
        if not path.resolve().is_relative_to(directory.resolve()) or sha256(read_bytes(path)) != manifest['sources'][key]['sha256']:
            raise ContentError('怪物补丁基线校验失败。')
        paths.append(path)
    loaded = load_inputs(Inputs(*paths))
    catalog = CreatureCatalog(directory, loaded, directory=directory / 'baseline/npc-snapshot')
    if not catalog.reset_coverage['available']:
        raise ContentError('怪物补丁需要已核验原生版本的 v3 快照。')
    edits = validate(loaded, manifest['edits'], catalog)
    resource_meta = manifest.get('resources', {})
    wanted = any(e['scale'] != [1, 1, 1] or e.get('model_ref') for e in edits)
    raw = read_bytes(directory / 'baseline' / ACT_PATH) if wanted else None
    resources, actors = build_variants(Path('.'), loaded, edits, source=raw,
                                      bundles=lambda ref:directory/'model-bundles'/ref)
    if set(resource_meta) != set(resources):
        raise ContentError('怪物资源清单不完整或含额外文件。')
    from .model_workspace import selectors
    expected, changes = prepare(loaded, edits, catalog=catalog, appearances=actors,
                               model_selectors=selectors(edits,lambda ref:directory/'model-bundles'/ref))
    if set(manifest['files']) != set(PAYLOAD_PATHS):
        raise ContentError('怪物双端表清单无效。')
    for key, relative in PAYLOAD_PATHS.items():
        meta = manifest['files'][key]
        if meta.get('path') != relative or sha256(read_bytes(directory / relative)) != meta.get('sha256'):
            raise ContentError('怪物表补丁负载校验失败。')
        decoder = decode_table if key != 'client_language' else decode_language
        if decoder(read_bytes(directory / relative)) != decoder(expected[key]):
            raise ContentError('怪物表包含草稿之外的修改。')
    for relative, resource in resources.items():
        meta = resource_meta[relative]
        if (meta.get('source_sha256') != (sha256(resource['source']) if resource['source'] is not None else None) or
                meta.get('sha256') != sha256(resource['data']) or
                read_bytes(directory / relative) != resource['data']):
            raise ContentError('怪物资源包含草稿之外的修改。')
    plan = point_plan(edits, catalog)
    if read_json(directory / 'npc-points.json') != {'format': 'xyj-npc-point-plan/v1', 'operations': plan} or changes != manifest['changes']:
        raise ContentError('怪物点位计划或差异清单不一致。')
    return {'status': 'offline_validated_runtime_pending', 'operations': len(edits),
            'point_changes': len(plan), 'resources': len(resources), 'all_records_checked': True,
            'opaque_bytes_preserved': True, 'gameplay_verified': False}


def export_patch(root, loaded, edits, *, catalog, npc_directory, client, destination=None):
    """Export a complete reproducible NPC package; never write real game files or a database."""
    import shutil
    import tempfile
    import uuid
    from datetime import datetime, timezone
    from .creature_assets import build_variants
    from .creature_database import point_plan
    if not edits:
        raise ContentError('请先暂存怪物修改。')
    edits = validate(loaded, edits, catalog)
    if not catalog.reset_coverage['available']:
        raise ContentError('怪物导出需要已核验原生版本的 v3 快照。')
    for path, original in zip((loaded.paths.server, loaded.paths.client, loaded.paths.language), loaded.blobs):
        if read_bytes(path) != original:
            raise ContentError('游戏基线已变化，请重新校验数据源。')
    from .model_workspace import bundle_path,copy_bundle
    resources, actors = build_variants(client, loaded, edits,bundles=lambda ref:bundle_path(root,ref))
    from .model_workspace import selectors
    blobs, changes = prepare(loaded, edits, catalog=catalog, appearances=actors,
                             model_selectors=selectors(edits,lambda ref:bundle_path(root,ref)))
    if not any(c['new'] or c['server_changed_bytes'] or c['client_changed_bytes'] or c['spawns'] for c in changes):
        raise ContentError('怪物草稿没有实际修改。')
    destination = output_path(root, destination if destination is not None else Path('output/editor/creature-patches') /
                              (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.creature-', dir=destination.parent))
    try:
        manifest = {'format': PATCH_FORMAT, 'native_sha256': NATIVE_SHA256,
                    'created_at': datetime.now(timezone.utc).isoformat(), 'sources': source_hashes(loaded),
                    'edits': edits, 'changes': changes, 'files': {}, 'resources': {}}
        for (key, relative), original in zip(PAYLOAD_PATHS.items(), loaded.blobs):
            atomic_write(staging / 'baseline' / relative, original)
            atomic_write(staging / relative, blobs[key])
            manifest['files'][key] = {'path': relative, 'sha256': sha256(blobs[key])}
        for relative, resource in resources.items():
            if resource['source'] is not None:
                atomic_write(staging / 'baseline' / relative, resource['source'])
            atomic_write(staging / relative, resource['data'])
            manifest['resources'][relative] = {'source_sha256': sha256(resource['source']) if resource['source'] is not None else None, 'sha256': sha256(resource['data'])}
        for reference in {e['model_ref'] for e in edits if e.get('model_ref')}:
            copy_bundle(bundle_path(root,reference),staging/'model-bundles'/reference,reference)
        snapshot = read_json(npc_directory / 'manifest.json')
        atomic_write(staging / 'baseline/npc-snapshot' / snapshot['catalog_file'], read_bytes(npc_directory / snapshot['catalog_file']))
        write_json(staging / 'baseline/npc-snapshot/manifest.json', snapshot)
        write_json(staging / 'npc-points.json', {'format': 'xyj-npc-point-plan/v1', 'operations': point_plan(edits, catalog)})
        write_json(staging / 'manifest.json', manifest)
        verification = verify_patch(staging)
        write_json(staging / 'verification.json', verification)
        atomic_write(staging / '部署说明.txt', (
            '怪物补丁仅通过离线重建校验，真实游戏效果待验收。\n'
            '服务端 Object、客户端 Object/Language 和可选 Act 文件必须配套。\n'
            'npc-points.json 是存档变更计划，手工复制文件不会执行该计划。图形工坊的“导出并覆盖双端”会配套发布。\n'
            '执行 tools/apply_creature_patch.py --patch 补丁目录 --apply --server-stopped 前，须停止所有使用该数据库的游戏服务及客户端。\n'
            '发布器先核对真实文件及存档，持久保存备份并提供失败恢复；旧版 MyISAM 不具备跨文件原子事务。\n'
            '整个程序仅保留最近一次备份和补丁包；禁止重复执行已经发布的补丁。\n'
            '新增点复制明确选择的普通 NPC 存档，写入新模板、名称、等级、性别、基础属性、坐标及候选；完整派生战斗状态与技能效果须首次入图、死亡刷新和重登验收。\n'
            '不同地图独立间隔使用用户可见的独立模板 ID；候选若指向其他模板，后续间隔遵从实际候选模板。\n'
        ).encode('utf-8'))
        from .files import move_path
        move_path(staging,destination)
        return {'path': str(destination), 'changes': changes, 'verification': verification}
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
