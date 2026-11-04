"""Audit every NPC template and distinct saved equipment combination without modifying game resources."""

from collections import Counter
from pathlib import Path
import json

from editor_server import EditorState
from xyj_content.binary import ContentError
from xyj_content.models import texture_png
from xyj_content.sprites import sprite_png


def main():
    """Record geometry, texture failures and partial-equipment warnings separately from game acceptance."""
    root = Path(__file__).resolve().parents[1]
    state = EditorState(root, root / 'output/patch-live-baseline-318/server/qingfeng/Object.dbf')
    catalog = state.creatures()
    variants = {}
    for role in catalog.roles.values():
        variants.setdefault((role['template_id'], tuple(role['equipment'][:4])), role['key'])
    cases = [('template', tid, None) for tid in catalog.records]
    cases.extend(('saved_equipment', tid, key) for (tid, _), key in variants.items())
    counts, failures, partial = {}, [], []
    for i, (scope, tid, role) in enumerate(cases):
        counter = counts.setdefault(scope, Counter())
        counter['total'] += 1
        try:
            model = catalog.model(tid, role)
            counter['geometry_decoded'] += 1
            issues = [w for w in model['warnings'] if '未还原：' in w]
            if issues:
                counter['partial_equipment'] += 1
                partial.append({'scope': scope, 'template_id': tid, 'role': role, 'warnings': issues})
            for mesh in model['meshes']:
                if mesh.get('texture_url'):
                    parts = mesh['texture_url'].split('/')
                    sprite_png(catalog.client, int(parts[-2]), int(parts[-1][:-4]))
                elif mesh['texture'] is not None:
                    texture_png(catalog.client, mesh['texture'])
            counter['textures_decoded'] += 1
        except ContentError as exc:
            failures.append({'scope': scope, 'template_id': tid, 'role': role, 'error': str(exc)})
        if (i+1) % 250 == 0:
            print('Checked', i+1, '/', len(cases), flush=True)
    result = {'summary': counts, 'failures': failures, 'partial_equipment': partial,
              'game_runtime_checked': False, 'scope': 'first pose; native special rendering effects not reproduced'}
    path = root / 'research/2026-11-03/creature_model_0_4_audit.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == '__main__':
    main()
