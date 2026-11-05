"""Validated item drafts and reproducible offline editor patches (never live deployment)."""

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import shutil
import struct
import tempfile
import uuid

from .binary import ContentError, decode_table, encode_table, read_bytes
from .catalog import Inputs, Loaded, load_inputs, source_hashes
from .files import atomic_write, output_path, read_json, sha256, write_json
from .language import decode_language, encode_language
from .objects import encode_fixed_text, find_unique, view_record, without_text
from .patch import PAYLOAD_PATHS, insertion_position
from .taxonomy import item_categories
from .item_properties import RESISTANCES, template_properties

# SWDOL.exe 0x581900..0x581e7b: direct Object reads and Language label lookups.
FIELDS = {
    "constitution": ("体质", 86, "h"), "strength": ("力量", 88, "h"),
    "stamina": ("耐力", 90, "h"), "agility": ("敏捷", 92, "h"),
    "dexterity": ("灵巧", 94, "h"), "intelligence": ("智慧", 96, "h"),
    "charisma": ("魅力", 98, "h"), "luck": ("运气", 100, "h"),
    "attack": ("攻击力", 102, "h"), "damage": ("伤害力", 104, "h"),
    "defense": ("防御力", 108, "h"), "armor": ("装甲防御力", 110, "h"),
    "hp": ("体力上限", 112, "h"), "mp": ("灵力上限", 114, "h"),
    "hit": ("命中率", 124, "h"), "evasion": ("回避率", 126, "h"),
    # SWDOL.exe 0x583aa9..0x583ad3 reads Object+0x8c as a signed word.
    "range": ("距离", 140, "h"), "delay": ("延迟", 144, "i"), "weight": ("重量", 172, "i"),
    "hp_recovery": ("体力回复", 130, "h"), "mp_recovery": ("灵力回复", 132, "h"),
    **{f"resist_{key}": (label, offset, "b") for key, label, offset in RESISTANCES},
}


def field_schema() -> list[dict]:
    """Expose only fields whose label, width and signedness have native-code evidence."""
    fields = []
    for key, (label, offset, fmt) in FIELDS.items():
        bits = struct.calcsize('<' + fmt) * 8
        group = ('weapon' if key in ('damage', 'delay', 'range') else
                 'resistances' if key.startswith('resist_') else
                 'recovery' if key.endswith('_recovery') else 'basic')
        fields.append({"key": key, "label": label, "offset": offset,
                       "min": -(2**(bits - 1)), "max": 2**(bits - 1) - 1, "group": group})
    return fields


def stats(record: bytes) -> dict:
    """Project the confirmed signed attribute values without changing the record."""
    return {key: struct.unpack_from('<' + fmt, record, offset)[0]
            for key, (_, offset, fmt) in FIELDS.items()}


def item_id_availability(loaded: Loaded, edits: list) -> dict:
    """Suggest an unreserved ID, excluding every record on either side and current drafts."""
    baseline = {struct.unpack_from('<I', record, 4)[0]
                for table in (loaded.server, loaded.client) for record in table.records}
    drafted = {edit['target_id'] for edit in edits}
    occupied = baseline | drafted
    return {'baseline_ids': sorted(baseline), 'draft_ids': sorted(drafted),
            'suggested_id': next((value for value in range(1, 32768) if value not in occupied), None)}


def item_detail(loaded: Loaded, object_id: int) -> dict:
    """Read client values and explain whether this exact server/client pair is editable."""
    row, record = find_unique(loaded.client, object_id)
    view = view_record(record, row, loaded.language)
    actor, mode = struct.unpack_from('<HH', record, 60)
    reason = ""
    try:
        sr, server = find_unique(loaded.server, object_id)
        sv = view_record(server, sr)
        if (without_text(server) != without_text(record) or
                (sv.name, sv.description) != (view.name, view.description)):
            reason = "双端该记录不一致，暂时只读。"
    except ContentError as exc:
        reason = str(exc)
    if view.type_code != 2:
        reason = "第一版仅编辑分类码 2 的道具，其他记录只读。"
    elif not 3001 <= actor <= 5999:
        reason = "此记录不使用道具外观脚本，第一版暂时只读。"
    return {"id": object_id, "name": view.name, "description": view.description,
            "type": view.type_code, "stats": stats(record), "actor": actor, "mode": mode,
            "editable": not reason, "readonly_reason": reason,
            "template_properties": template_properties(record), **item_categories(record)}


def validate_edits(loaded: Loaded, edits: list) -> list[dict]:
    """Reject ambiguous IDs, unknown fields, lossy text and conflicting draft operations."""
    if not isinstance(edits, list) or len(edits) > 200:
        raise ContentError("草稿必须是最多 200 个操作的列表。")
    seen, normalized = set(), []
    ids = {struct.unpack_from('<I', r, 4)[0] for table in (loaded.client, loaded.server) for r in table.records}
    schema = {v['key']: v for v in field_schema()}
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) - {'source_id', 'target_id', 'name', 'description', 'stats', 'acquisition'} or not {'source_id', 'target_id', 'name', 'description', 'stats'}.issubset(edit):
            raise ContentError("道具操作字段不完整或含未支持的字段。")
        source, target = edit['source_id'], edit['target_id']
        if any(type(v) is not int or not 1 <= v <= 32767 for v in (source, target)):
            raise ContentError("道具 ID 必须是 1 至 32767 的整数。")
        if target in seen or (target != source and target in ids):
            raise ContentError(f"目标 ID {target} 重复或已经存在。")
        seen.add(target)
        detail = item_detail(loaded, source)
        if not detail['editable']:
            raise ContentError(detail['readonly_reason'])
        if not isinstance(edit['name'], str) or not edit['name'].strip() or not isinstance(edit['description'], str):
            raise ContentError("名称必须非空，说明必须是文本。")
        encode_fixed_text(edit['name'], 16)
        encode_fixed_text(edit['description'], 200)
        values = edit['stats']
        if not isinstance(values, dict) or not set(values).issubset(FIELDS):
            raise ContentError("草稿包含尚未验证的属性字段。")
        for key, value in values.items():
            if type(value) is not int or not schema[key]['min'] <= value <= schema[key]['max']:
                raise ContentError(f"{schema[key]['label']} 超出整数范围。")
        operation = {**edit, 'stats': dict(values)}
        if 'acquisition' in edit:
            from .acquisition import normalize_acquisition
            operation['acquisition'] = normalize_acquisition(edit['acquisition'])
        normalized.append(operation)
    if any('acquisition' in edit for edit in normalized):
        from .acquisition import validate_drop_edits
        validate_drop_edits(loaded, normalized)
    return normalized


def prepare_edits(loaded: Loaded, edits: list) -> tuple[dict, list[dict]]:
    """Apply validated operations to copies, append changed strings and preserve opaque bytes."""
    edits = validate_edits(loaded, edits)
    server, client, language, changes = loaded.server, loaded.client, loaded.language, []
    for edit in edits:
        source, target = edit['source_id'], edit['target_id']
        si, sr = find_unique(loaded.server, source)
        ci, cr = find_unique(loaded.client, source)
        before = item_detail(loaded, source)
        ns, nc = bytearray(sr), bytearray(cr)
        differences = []
        if target != source:
            differences.append({'field': 'ID（复制）', 'before': source, 'after': target})
            struct.pack_into('<I', ns, 4, target)
            struct.pack_into('<I', nc, 4, target)
        for key, offset, width in (('name', 12, 16), ('description', 600, 200)):
            if edit[key] != before[key]:
                differences.append({'field': '名称' if key == 'name' else '说明', 'before': before[key], 'after': edit[key]})
                ns[offset:offset+width] = encode_fixed_text(edit[key], width)
                struct.pack_into('<I', nc, offset, len(language.entries))
                language = language.append_texts(edit[key])
        for key, value in edit['stats'].items():
            label, offset, fmt = FIELDS[key]
            if before['stats'][key] != value:
                differences.append({'field': label, 'before': before['stats'][key], 'after': value})
                struct.pack_into('<' + fmt, ns, offset, value)
                struct.pack_into('<' + fmt, nc, offset, value)
        if target == source:
            # Look up current rows: previous insertions can shift their positions.
            si, _ = find_unique(server, source)
            ci, _ = find_unique(client, source)
            server = replace(server, records=server.records[:si] + (bytes(ns),) + server.records[si+1:])
            client = replace(client, records=client.records[:ci] + (bytes(nc),) + client.records[ci+1:])
        else:
            server = server.insert(insertion_position(server, before['type']), bytes(ns))
            client = client.insert(insertion_position(client, before['type']), bytes(nc))
        changes.append({'source_id': source, 'target_id': target, 'name': edit['name'], 'fields': differences})
    from .acquisition import apply_drop_edits
    server, drop_changes = apply_drop_edits(loaded, server, edits)
    for change in changes:
        edit = next(e for e in edits if e['target_id'] == change['target_id'])
        if any(e['item_id'] == change['target_id'] for e in drop_changes):
            change['fields'].append({'field': '怪物掉落', 'before': None,
                                     'after': next(e for e in drop_changes if e['item_id'] == change['target_id'])})
        for key, label in (('shops', '商店购买'), ('rewards', '任务奖励')):
            if edit.get('acquisition', {}).get(key):
                change['fields'].append({'field': label, 'before': None, 'after': edit['acquisition'][key]})
    return {'server_object': encode_table(server), 'client_object': encode_table(client),
            'client_language': encode_language(language)}, changes


def verify_editor_patch(root: Path, directory: Path) -> dict:
    """Rebuild expected semantics from immutable baselines and compare all records and strings."""
    directory = output_path(root, directory)
    manifest = read_json(directory / 'manifest.json')
    if manifest.get('format') != 'xyj-editor-patch/v1':
        raise ContentError("不是编辑器补丁格式。")
    paths = []
    for key, relative in PAYLOAD_PATHS.items():
        source = directory / 'baseline' / relative
        if not source.resolve().is_relative_to(directory):
            raise ContentError("基线路径越界。")
        if sha256(read_bytes(source)) != manifest['sources'][key]['sha256']:
            raise ContentError("补丁基线校验失败。")
        paths.append(source)
    loaded = load_inputs(Inputs(*paths))
    expected, changes = prepare_edits(loaded, manifest['edits'])
    from .acquisition import build_script_payloads, touched_scripts, SCRIPT_NAME
    script_sources = {}
    script_meta = manifest.get('scripts', {})
    if not isinstance(script_meta, dict) or set(script_meta) != touched_scripts(manifest['edits']):
        raise ContentError('获取途径脚本清单不完整。')
    if script_meta:
        from .plot_maps import NATIVE_SHA256
        if manifest.get('script_native_sha256') != NATIVE_SHA256:
            raise ContentError('获取脚本的服务端版本标记无效。')
    for filename, meta in script_meta.items():
        if (not SCRIPT_NAME.fullmatch(filename) or not isinstance(meta, dict) or
                set(meta) != {'path', 'source_sha256', 'sha256', 'bytes'} or
                type(meta['bytes']) is not int or not 520 <= meta['bytes'] <= 4 * 1024 * 1024):
            raise ContentError('脚本名称无效。')
        relative = f'server/qingfeng/ASM/{filename}'
        if meta.get('path') != relative:
            raise ContentError('脚本补丁路径无效。')
        source = directory / 'baseline' / relative
        target = directory / relative
        if not source.resolve().is_relative_to(directory) or not target.resolve().is_relative_to(directory):
            raise ContentError('脚本补丁路径越界。')
        script_sources[filename] = read_bytes(source)
        if sha256(script_sources[filename]) != meta['source_sha256']:
            raise ContentError('脚本基线校验失败。')
    for filename, blob in build_script_payloads(script_sources, manifest['edits']).items():
        actual = read_bytes(directory / script_meta[filename]['path'])
        if actual != blob or sha256(actual) != script_meta[filename]['sha256'] or len(actual) != script_meta[filename]['bytes']:
            raise ContentError('脚本补丁包含获取配置之外的修改。')
    for key, relative in PAYLOAD_PATHS.items():
        path = directory / relative
        if not path.resolve().is_relative_to(directory):
            raise ContentError("补丁路径越界。")
        blob = read_bytes(path)
        if sha256(blob) != manifest['files'][key]['sha256']:
            raise ContentError("补丁负载校验失败。")
        decoder = decode_language if key == 'client_language' else decode_table
        if decoder(blob) != decoder(expected[key]):
            raise ContentError("补丁包含草稿之外的修改。")
    if changes != manifest.get('changes'):
        raise ContentError("补丁差异清单不一致。")
    return {'status': 'offline_validated_runtime_pending', 'operations': len(changes),
            'all_records_checked': True, 'opaque_bytes_preserved': True,
            'acquisition_scripts_checked': len(script_meta),
            'original_language_entries_preserved': True, 'gameplay_verified': False}


def export_editor_patch(root: Path, loaded: Loaded, edits: list, *, acquisition=None) -> dict:
    """Publish a new verified patch below output/editor/patches, including rollback baselines."""
    if not edits:
        raise ContentError("草稿为空，请先暂存道具修改。")
    for path, blob in zip((loaded.paths.server, loaded.paths.client, loaded.paths.language), loaded.blobs):
        if read_bytes(path) != blob:
            raise ContentError("源文件在编辑期间发生变化，请重启编辑器重新加载。")
    blobs, changes = prepare_edits(loaded, edits)
    from .acquisition import build_script_payloads, touched_scripts
    script_sources = acquisition.export_sources(edits) if acquisition is not None else {}
    if touched_scripts(edits) and acquisition is None:
        raise ContentError('商店和任务奖励导出需要经过校验的脚本数据源。')
    scripts = build_script_payloads(script_sources, edits)
    if not any(c['fields'] for c in changes):
        raise ContentError("草稿没有实际修改。")
    name = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    destination = output_path(root, Path('output/editor/patches') / name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.editor-', dir=destination.parent)).resolve()
    try:
        sources = source_hashes(loaded)
        for key, path in zip(PAYLOAD_PATHS, (loaded.paths.server, loaded.paths.client, loaded.paths.language)):
            # Manifest paths are provenance only. Verification always uses the hash-checked embedded baseline.
            sources[key]['path'] = path.resolve().relative_to(root.resolve()).as_posix() if path.resolve().is_relative_to(root.resolve()) else str(path.resolve())
        manifest = {'format': 'xyj-editor-patch/v1', 'created_at': datetime.now(timezone.utc).isoformat(),
                    'status': 'offline_validated_runtime_pending', 'sources': sources, 'edits': edits, 'changes': changes,
                    'files': {key: {'path': PAYLOAD_PATHS[key], 'sha256': sha256(blob), 'bytes': len(blob)} for key, blob in blobs.items()}}
        if scripts:
            from .plot_maps import NATIVE_SHA256
            manifest['script_native_sha256'] = NATIVE_SHA256
            manifest['scripts'] = {}
            for filename, blob in scripts.items():
                relative = f'server/qingfeng/ASM/{filename}'
                atomic_write(staging / 'baseline' / relative, script_sources[filename])
                atomic_write(staging / relative, blob)
                manifest['scripts'][filename] = {'path': relative, 'source_sha256': sha256(script_sources[filename]),
                                                 'sha256': sha256(blob), 'bytes': len(blob)}
        for (key, relative), original in zip(PAYLOAD_PATHS.items(), loaded.blobs):
            atomic_write(staging / 'baseline' / relative, original)
            atomic_write(staging / relative, blobs[key])
        write_json(staging / 'manifest.json', manifest)
        result = verify_editor_patch(root, staging)
        write_json(staging / 'verification.json', result)
        atomic_write(staging / '部署说明.txt', (
            '编辑器补丁：仅通过离线校验，游戏内效果待验收。\n'
            'server/qingfeng/Object.dbf 用于实际服务端；client/Main 两文件必须配套更新。\n'
            '部署前停止两端并核对来源哈希；baseline 是对应的回退原文件。\n'
            'manifest.json 记录修改字段与来源。此工具不会部署、发道具或重启服务器。\n'
            '如含 server/qingfeng/ASM 编号脚本，须与道具文件配套更新。奖励沿用原任务条件；原生发奖按顺序执行，非事务。\n'
        ).encode('utf-8-sig'))
        os.rename(staging, destination)
        return {'path': str(destination), 'verification': result, 'changes': changes}
    except OSError as exc:
        raise ContentError(f"导出补丁失败：{exc.strerror}") from exc
    finally:
        if staging.exists() and staging.is_relative_to(destination.parent) and staging.name.startswith('.editor-'):
            shutil.rmtree(staging)
