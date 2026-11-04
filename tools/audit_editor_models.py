"""Audit first-version preview coverage for local category-2 objects; write research only."""

from collections import Counter
from pathlib import Path
import json
import struct

from xyj_content.binary import ContentError, decode_table, read_bytes
from xyj_content.files import sha256, write_json
from xyj_content.language import decode_language
from xyj_content.models import item_model, texture_png
from xyj_content.objects import view_record


def main():
    """Check every eligible item and every referenced texture, without touching game assets."""
    root = Path(__file__).resolve().parents[1]
    client = root / 'swdol'
    blob = read_bytes(client / 'Main/Object.dbf')
    table = decode_table(blob)
    language = decode_language(read_bytes(client / 'Main/Language.DBF'))
    counts = Counter(struct.unpack_from('<I', r, 4)[0] for r in table.records)
    results, texture_ids = [], set()
    for row, record in enumerate(table.records):
        view = view_record(record, row, language)
        if view.type_code != 2 or not view.object_id or counts[view.object_id] != 1:
            continue
        entry = {'id': view.object_id, 'name': view.name}
        try:
            model = item_model(client, record)
            textures = sorted({mesh['texture'] for mesh in model['meshes'] if mesh['texture']})
            texture_ids.update(textures)
            entry.update(status='geometry_decoded', file=model['file'], textures=textures, warnings=model['warnings'])
        except (ContentError, ValueError, UnicodeError, struct.error) as exc:
            entry.update(status='unsupported', reason=str(exc))
        results.append(entry)
    textures = {}
    for texture in sorted(texture_ids):
        try:
            png = texture_png(client, texture)
            textures[str(texture)] = {'status': 'decoded', 'png_bytes': len(png)}
        except ContentError as exc:
            textures[str(texture)] = {'status': 'unsupported', 'reason': str(exc)}
    summary = {'client_object_sha256': sha256(blob), 'items': len(results),
               'geometry_decoded': sum(e['status'] == 'geometry_decoded' for e in results),
               'unsupported_reasons': dict(Counter(e['reason'] for e in results if e['status'] == 'unsupported')),
               'textures': len(textures), 'textures_decoded': sum(e['status'] == 'decoded' for e in textures.values()),
               'scope': 'Offline parsing coverage; not proof of game rendering equivalence.'}
    write_json(root / 'research/2026-11-02/editor_model_coverage.json', {'summary': summary, 'items': results, 'textures': textures})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
