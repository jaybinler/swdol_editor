"""Audit item categories, inventory icons and equipped previews across current local data."""

from collections import Counter
from pathlib import Path
import json

from editor_server import EditorState
from xyj_content.appearance import equipped_model
from xyj_content.binary import ContentError
from xyj_content.files import write_json
from xyj_content.models import item_model
from xyj_content.objects import find_unique
from xyj_content.sprites import icon_reference, sprite_png


def main():
    """Write bounded coverage evidence under research; never modify assets or editor drafts."""
    root = Path(__file__).resolve().parents[1]
    state = EditorState(root, root / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf')
    rows, icons, equipped, singles = [], Counter(), Counter(), Counter()
    for item in state.items:
        if not item['is_item']:
            continue
        record = find_unique(state.loaded.client, item['id'])[1]
        row = {'id': item['id'], 'name': item['name'], 'categories': item['categories']}
        try:
            reference = icon_reference(state.client, record)
            sprite_png(state.client, **reference)
            row['icon'] = reference
            icons['decoded'] += 1
        except ContentError as exc:
            row['icon_error'] = str(exc)
            icons[str(exc)] += 1
        if item['type'] == 2:
            try:
                model = item_model(state.client, record)
                row['single'] = model['file']
                singles['equipped_mesh'] += 1
            except ContentError:
                try:
                    model = item_model(state.client, record, display=True)
                    row['single_display'] = model['file']
                    singles['ground_mesh'] += 1
                except ContentError as exc:
                    singles[str(exc)] += 1
            try:
                model = equipped_model(state.client, state.loaded.client, record)
                for mesh in model['meshes']:
                    if mesh.get('texture_url'):
                        parts = mesh['texture_url'].split('/')
                        sprite_png(state.client, int(parts[-2]), int(parts[-1][:-4]))
                row['equipped'] = model['profile']
                equipped['decoded'] += 1
            except ContentError as exc:
                row['equipped_error'] = str(exc)
                equipped[str(exc)] += 1
        rows.append(row)
    summary = {'items': len(rows), 'icons': dict(icons), 'single': dict(singles), 'equipped': dict(equipped)}
    write_json(root / 'research/2026-11-03/editor_asset_coverage.json', {'summary': summary, 'items': rows})
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
