"""Guarded NPC point changes for stopped legacy servers; MyISAM is never called transactional."""

from pathlib import Path
import re
import struct

from .binary import ContentError
from .files import write_json, read_json

TABLES = ('npcpool', 'npcpool_add_on')
REQUIRED = {'RoleID', 'ObjectType', 'OriginalObjectID', 'SectionID', 'ReviveSectionID',
            'ReviveX', 'ReviveY', 'ReviveZ', 'X', 'Y', 'Z', 'Dir', 'ReviveDir', 'Removed',
            'Quantity', 'RoleName', 'OwnerDBID', 'AccountID', 'LaborID', 'Level', 'Sex',
            *(f'NextNPC_{i:02d}' for i in range(5))}


def point_plan(edits, catalog):
    """Freeze source keys and positional preconditions without exposing full database rows."""
    from .creature_editor import normalize_spawns
    operations = [{'target_id': e['target_id'], 'name': e['name'], 'level': e['level'], 'sex': e['sex'],
             'attributes': e['attributes'], 'reset_candidates': e['reset_candidates'],
             'point': p}
            for e in edits for p in normalize_spawns(e['spawns'], e['target_id'], e['source_id'], catalog)]
    # Copy seeds before moving/removing them in this same reviewed package.
    return sorted(operations, key=lambda operation: operation['point']['action'] != 'add')


def schema(query):
    """Require both verified NPC tables, numeric identity and safe column identifiers."""
    result = {}
    for table in TABLES:
        rows = [line.split('\t') for line in query(f'SHOW COLUMNS FROM `{table}`;').splitlines()]
        columns = {r[0]: r[1] for r in rows if len(r) == 6}
        if (not REQUIRED.issubset(columns) or len(columns) > 200 or
                any(not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}', k) for k in columns) or
                any(not re.fullmatch(r'(?:tinyint|smallint|mediumint|int|bigint)(?:\(\d+\))?(?: unsigned)?|float|double|(?:var)?char\(\d+\)(?: binary)?|(?:tiny|medium|long)?blob|datetime', t) for t in columns.values()) or
                not any(r[0] == 'RoleID' and r[5] == 'auto_increment' for r in rows)):
            raise ContentError('NPC 数据库结构不兼容，已停止写入。')
        result[table] = columns
    return result


def literal(value, kind):
    """Serialize a typed hex image using numeric literals or binary hex, never SQL string interpolation."""
    if value is None:
        return 'NULL'
    if not isinstance(value, str) or not re.fullmatch(r'(?:[0-9A-F]{2})*', value):
        raise ContentError('数据库备份值无效。')
    if re.match(r'(tinyint|smallint|mediumint|int|bigint|float|double)', kind):
        text = bytes.fromhex(value).decode('ascii')
        if not re.fullmatch(r'-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?', text):
            raise ContentError('数据库数值备份无效。')
        return text
    return "''" if value == '' else '0x' + value


def image(query, table, role, columns):
    """Read one NPC only, using hex for every column so binary fields cannot corrupt delimiters."""
    if table not in TABLES or type(role) is not int or not 1 <= role <= 2147483647:
        raise ContentError('NPC 存档键无效。')
    names = list(columns)
    data = query('SELECT ' + ','.join(expression(k, columns[k]) for k in names) + f' FROM `{table}` WHERE RoleID={role};')
    lines = data.splitlines()
    if len(lines) != 1:
        raise ContentError('NPC 存档不存在或不唯一。')
    values = lines[0].split('\t')
    if len(values) != len(names):
        raise ContentError('NPC 存档返回字段数不一致。')
    row = {k: None if v == 'NULL' else v for k, v in zip(names, values)}
    for key, value in row.items():
        literal(value, columns[key])
    return row


def number(row, key):
    """Decode only a previously validated numeric field from its lossless row image."""
    return float(bytes.fromhex(row[key]).decode('ascii'))


def encoded(value):
    """Encode a finite module-owned number as the same hex representation returned by HEX(column)."""
    return str(value).encode('ascii').hex().upper()


def expression(key, kind):
    """Read complete float32 precision instead of MySQL 4's short default float formatting."""
    text = f"REPLACE(FORMAT(`{key}`,16),',','')" if kind in ('float', 'double') else f"CONCAT('',`{key}`)"
    return f'HEX({text})'


def coordinate(value):
    """Represent the actual native float32 value before writing or journaling it."""
    number = struct.unpack('<f', struct.pack('<f', value))[0]
    return format(number, '.16f').encode('ascii').hex().upper()


def matches_snapshot(row, expected):
    """Compare identity and stored float32 positions; never treat a changed point as a fresh seed."""
    if (number(row, 'OriginalObjectID') != expected['template_id'] or
            number(row, 'SectionID') != expected['map_id'] or
            number(row, 'ReviveSectionID') != expected['revive_map_id'] or number(row, 'Removed') != 0):
        return False
    if 'plots' in expected and [int(number(row, field)) for field in ('BKAutoPlotID','BKTouchPlotID','BKRangePlotID','BKDeadPlotID')] != expected['plots']:
        return False
    return all(struct.pack('<f', number(row, 'Revive'+axis)) == struct.pack('<f', value)
               for axis, value in zip('XYZ', expected['position']))


def predicate(row, columns):
    """Build a full-row compare-and-swap predicate, including NULL and opaque binary fields."""
    return ' AND '.join(expression(key, columns[key]) + (' IS NULL' if value is None else "='" + value + "'")
                        for key, value in row.items())


def apply_points(query, plan, journal_path: Path):
    """Apply reviewed point mutations with durable before/after images; retain a recovery log on failure."""
    if journal_path.exists():
        raise ContentError('存档恢复日志已存在；请先核对或恢复上次操作。')
    columns = schema(query)
    journal = {'format': 'xyj-npc-point-journal/v1', 'phase': 'applying', 'entries': []}
    write_json(journal_path, journal)
    try:
        for operation in plan:
            p = operation['point']
            table, role_text = p['key'].split(':')
            if table not in TABLES or not role_text.isdecimal():
                raise ContentError('刷新点来源键无效。')
            role = int(role_text)
            before = image(query, table, role, columns[table])
            if number(before, 'ObjectType') != 0 or not matches_snapshot(before, p['expected']):
                raise ContentError('刷新点存档与快照不一致，请重新采集。')
            if any(number(before, key) not in (-1, 0) for key in ('OwnerDBID', 'AccountID', 'LaborID')):
                raise ContentError('该存档属于账号、主人或组织，不能作为普通刷怪点修改。')
            for _ in range(p['count'] if p['action'] == 'add' else 1):
                after = dict(before)
                if p['action'] == 'remove':
                    after['Removed'] = encoded(1)
                else:
                    for key in ('SectionID', 'ReviveSectionID'):
                        after[key] = encoded(p['map_id'])
                    for axis, value in zip('XYZ', p['position']):
                        after[axis] = after['Revive'+axis] = coordinate(value)
                    after['Dir'] = after['ReviveDir'] = coordinate(p['direction'])
                if p['action'] == 'add':
                    # Preserve unknown NPC state from the explicit source; never clone ownership.
                    for key in ('OwnerDBID', 'AccountID', 'LaborID'):
                        after[key] = encoded(-1)
                    for key in ('LiveTime', 'BodyState', 'Removed', 'PlayTime'):
                        if key in after: after[key] = encoded(0)
                    after['OriginalObjectID'] = encoded(operation['target_id'])
                    after['Quantity'] = encoded(1)
                    after['RoleName'] = operation['name'].encode('gbk').hex().upper()
                    after['Level'] = encoded(operation['level']); after['Sex'] = encoded(operation['sex'])
                    for index, candidate in enumerate(operation['reset_candidates']):
                        after[f'NextNPC_{index:02d}'] = encoded(operation['target_id'] if candidate == -1 else candidate)
                    for key, field in zip(('constitution','strength','stamina','agility','dexterity','intelligence','charisma','luck'),
                                          ('CON','STR','STA','AGI','DEX','INT_','ChA','Luck')):
                        if field in after: after[field] = encoded(operation['attributes'][key])
                    # Allocate explicitly before INSERT so crash recovery always knows the new row ID.
                    maxima = [query(f'SELECT IFNULL(MAX(RoleID),0) FROM `{name}`;').strip() for name in TABLES]
                    if not all(value.isdecimal() for value in maxima):
                        raise ContentError('无法分配安全的 NPC 存档编号。')
                    target_role = max(map(int, maxima)) + 1
                    if target_role >= 2147483647:
                        raise ContentError('NPC 存档编号已耗尽。')
                    after['RoleID'] = encoded(target_role)
                    entry = {'table': table, 'role': target_role, 'before': None, 'after': after, 'status': 'prepared'}
                else:
                    entry = {'table': table, 'role': role, 'before': before, 'after': after, 'status': 'prepared'}
                journal['entries'].append(entry); write_json(journal_path, journal)
                if p['action'] == 'add':
                    names = list(after)
                    sql = f'INSERT INTO `{table}` (' + ','.join(f'`{k}`' for k in names) + ') VALUES ('
                    sql += ','.join(literal(after[k], columns[table][k]) for k in names) + ');'
                else:
                    changed = [key for key in after if after[key] != before[key]]
                    if not changed:
                        entry['status'] = 'unchanged'; write_json(journal_path, journal); continue
                    sql = f'UPDATE `{table}` SET ' + ','.join(f'`{k}`={literal(after[k],columns[table][k])}' for k in changed)
                    sql += ' WHERE ' + predicate(before, columns[table]) + ';'
                query(sql)
                entry['status'] = 'write_acknowledged'; write_json(journal_path, journal)
                actual = image(query, table, entry['role'], columns[table])
                # MySQL stores single precision floats; record the exact read-back image for safe rollback.
                for key in actual:
                    if columns[table][key] in ('float', 'double'):
                        if struct.pack('<f', number(actual,key)) != struct.pack('<f', number(after,key)):
                            raise ContentError('NPC 坐标写入读回不一致。')
                    elif actual[key] != after[key]:
                        raise ContentError('NPC 存档写入读回不一致。')
                entry['after'] = actual; entry['status'] = 'applied'; write_json(journal_path, journal)
        journal['phase'] = 'committed'; write_json(journal_path, journal)
        return {'status': 'committed', 'rows': len(journal['entries'])}
    except Exception:
        journal['phase'] = 'recovery_required'; write_json(journal_path, journal)
        raise


def rollback_points(query, journal_path):
    """Restore only rows still matching this journal; never overwrite later external changes."""
    journal = read_json(journal_path)
    if journal.get('format') != 'xyj-npc-point-journal/v1':
        raise ContentError('NPC 存档恢复日志格式无效。')
    columns = schema(query)
    for entry in reversed(journal['entries']):
        table, role = entry['table'], entry['role']
        if table not in TABLES or type(role) is not int or not 1 <= role <= 2147483647:
            raise ContentError('NPC 恢复存档键无效。')
        for row in (entry['before'], entry['after']):
            if row is None:
                continue
            if not isinstance(row, dict) or set(row) != set(columns[table]):
                raise ContentError('NPC 恢复行字段与数据库结构不一致。')
            for key, value in row.items():
                literal(value, columns[table][key])
        try:
            actual = image(query, table, role, columns[table])
        except ContentError:
            if entry['before'] is None and not query(f'SELECT RoleID FROM `{table}` WHERE RoleID={role};').strip():
                continue
            raise
        if actual == entry['before']:
            continue
        if actual != entry['after']:
            raise ContentError('NPC 存档被外部修改，已停止恢复以保护新内容。')
        if entry['before'] is None:
            sql = f'DELETE FROM `{table}` WHERE ' + predicate(actual, columns[table]) + ';'
        else:
            sql = f'UPDATE `{table}` SET ' + ','.join(f'`{k}`={literal(v,columns[table][k])}' for k,v in entry['before'].items())
            sql += ' WHERE ' + predicate(actual, columns[table]) + ';'
        query(sql)
        if entry['before'] is None:
            if query(f'SELECT RoleID FROM `{table}` WHERE RoleID={role};').strip():
                raise ContentError('NPC 新增行恢复删除失败。')
        elif image(query, table, role, columns[table]) != entry['before']:
            raise ContentError('NPC 恢复期间存档发生变化。')
    journal['phase'] = 'rolled_back'; write_json(journal_path, journal)
