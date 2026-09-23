"""Add official-area summaries without altering any existing analytical result."""
from copy import deepcopy


def attach_hectares(payload, path, areas):
    sections = {'/api/land/activity-2026': 'activity', '/api/land/history-2021-2025': 'history',
                '/api/land/use-type': 'land_use_type'}
    if path != '/api/land/delivery' and path not in sections:
        return payload
    result = deepcopy(payload)
    targets = result if path == '/api/land/delivery' else {sections[path]: result}
    if path == '/api/land/use-type' and 'annual_cycles' in result:
        targets['annual_cycles'] = result['annual_cycles']
    for topic in ['activity', 'history', 'land_use_type', 'annual_cycles']:
        if topic in targets:
            for scope, summary in targets[topic].get('summaries', {}).items():
                summary['official_areas_ha'] = areas[topic][scope]
    if 'potential' in targets:
        p = targets['potential']
        for scope, summary in p['summaries'].items():
            summary['official_areas_ha'] = areas['potential']['inner'][scope]
        for name, key in [('summaries', 'outside'), ('combined_summaries', 'combined')]:
            for scope, summary in p['expansion'][name].items():
                summary['official_areas_ha'] = areas['potential'][key][scope]
    return result
