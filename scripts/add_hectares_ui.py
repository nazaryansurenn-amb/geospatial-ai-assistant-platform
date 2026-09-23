"""Make count/area presentation changes only in the new UI source copy."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / 'server_data/review/hectares_review_20260906_v1/frontend_source/src'


def once(text, old, new):
    assert text.count(old) == 1, (old[:100], text.count(old))
    return text.replace(old, new, 1)


component = '''import React from "react";
const fmt = new Intl.NumberFormat("hy-AM", {maximumFractionDigits: 2});
const countFmt = new Intl.NumberFormat("hy-AM");
export const formatCountArea = (count, area) => `${countFmt.format(count ?? 0)} · ${Number.isFinite(area) ? fmt.format(area) : "—"} հա`;
export default function AreaMetric({count, area}) {
  return <span className="parcel-count-area"><span>{countFmt.format(count ?? 0)}</span><small title="Կադաստրային մակերես">{Number.isFinite(area) ? fmt.format(area) : "—"} հա</small></span>;
}
'''
(SRC / 'AreaMetric.jsx').write_text(component, encoding='utf-8')
p = SRC / 'App.jsx'
text = p.read_text(encoding='utf-8')
text = once(text, 'import maplibregl from "maplibre-gl";', 'import maplibregl from "maplibre-gl";\nimport AreaMetric, { formatCountArea } from "./AreaMetric";')
pairs = {
 'activitySummary.open_field_parcel_count': 'activitySummary.official_areas_ha?.open_field',
 'activitySummary.activity_review_parcel_count': 'activitySummary.official_areas_ha?.review',
 'historySummary.eligible_parcel_count': 'historySummary.official_areas_ha?.eligible',
 'historySummary.processed_parcel_count': 'historySummary.official_areas_ha?.processed',
 'historySummary.automatic_classified_count': 'historySummary.official_areas_ha?.automatic',
 'historySummary.review_count': 'historySummary.official_areas_ha?.review',
 'cropTypeSummary.eligible_parcel_count': 'cropTypeSummary.official_areas_ha?.eligible',
 'cropTypeSummary.class_counts.annual': 'cropTypeSummary.official_areas_ha?.classes?.annual',
 'cropTypeSummary.class_counts.perennial': 'cropTypeSummary.official_areas_ha?.classes?.perennial',
 'cropTypeSummary.class_counts.undetermined': 'cropTypeSummary.official_areas_ha?.classes?.undetermined',
 'annualCyclesSummary.class_counts.single_cycle': 'annualCyclesSummary.official_areas_ha?.classes?.single_cycle',
 'annualCyclesSummary.class_counts.two_cycle_recurring': 'annualCyclesSummary.official_areas_ha?.classes?.two_cycle_recurring',
}
for field, area in pairs.items():
    pattern = r'\{formatCount\(\s*' + re.escape(field) + r'\s*,?\s*\)\}'
    text, n = re.subn(pattern, f'<AreaMetric count={{{field}}} area={{{area}}}/>', text)
    assert n == 1, (field, n)
text = once(text, '                  const classOption = (', '''                  const classCount = activityControl ? activitySummary?.activity_class_counts?.[landClass.id]
                    : householdControl ? activitySummary?.household_parcel_count
                    : cropTypeControl ? cropTypeSummary?.class_counts?.[landClass.id]
                    : historyControl ? historySummary?.class_counts?.[landClass.id] : null;
                  const classArea = activityControl ? activitySummary?.official_areas_ha?.classes?.[landClass.id]
                    : householdControl ? activitySummary?.official_areas_ha?.household
                    : cropTypeControl ? cropTypeSummary?.official_areas_ha?.classes?.[landClass.id]
                    : historyControl ? historySummary?.official_areas_ha?.classes?.[landClass.id] : null;
                  const classOption = (''')
text = once(text, '<span>{landClass.label}</span>', '<span>{landClass.label}{classCount != null && <small className="parcel-class-metric">{formatCountArea(classCount, classArea)}</small>}</span>')
text = once(text, '''formatCount(
                                          annualCyclesSummary.class_counts[annualCycle],
                                        )''', '''formatCountArea(annualCyclesSummary.class_counts[annualCycle], annualCyclesSummary.official_areas_ha?.classes?.[annualCycle])''')
text = text.replace('${formatCount(activitySummary.household_parcel_count)} հողամաս', '${formatCount(activitySummary.household_parcel_count)} հողամաս (${formatArea(activitySummary.official_areas_ha?.household)} հա)')
text = text.replace('${formatCount(historySummary.class_counts.not_calculated)} հողամասի', '${formatCountArea(historySummary.class_counts.not_calculated, historySummary.official_areas_ha?.classes?.not_calculated)} հողամասի')
text = text.replace('${formatCount(historySummary.class_counts.insufficient)}', '${formatCountArea(historySummary.class_counts.insufficient, historySummary.official_areas_ha?.classes?.insufficient)}')
text = once(text, '              {landMode === "activity_2026" && (', '              <p className="parcel-area-note">Հողամասերի մակերեսը՝ ըստ կադաստրի</p>\n              {landMode === "activity_2026" && (')
p.write_text(text, encoding='utf-8')
p = SRC / 'PotentialView.jsx'
text = p.read_text(encoding='utf-8')
text = text.replace('import React, { useEffect, useState } from "react";', 'import React, { useEffect, useState } from "react";\nimport AreaMetric, { formatCountArea } from "./AreaMetric";')
for field, area in {
 'summary.eligible_parcel_count': 'summary.official_areas_ha?.eligible',
 'summary.candidate_count': 'summary.official_areas_ha?.candidate',
 'inner?.candidate_count': 'inner?.official_areas_ha?.candidate',
 'outer.candidate_count': 'outer.official_areas_ha?.candidate',
 'summary.class_counts.gravity_candidate': 'summary.official_areas_ha?.classes?.gravity_candidate',
 'summary.class_counts.mechanical_candidate': 'summary.official_areas_ha?.classes?.mechanical_candidate',
 'summary.class_counts.review': 'summary.official_areas_ha?.classes?.review',
}.items():
    text = once(text, '{count(' + field + ')}', '<AreaMetric count={' + field + '} area={' + area + '}/>')
text = once(text, '{count(summary.class_counts[kind])}', '{formatCountArea(summary.class_counts[kind], summary.official_areas_ha?.classes?.[kind])}')
p.write_text(text, encoding='utf-8')
p = SRC / 'ConsolidationView.jsx'
text = p.read_text(encoding='utf-8')
text = text.replace('import maplibregl from "maplibre-gl";', 'import maplibregl from "maplibre-gl";\nimport AreaMetric from "./AreaMetric";')
text = once(text, '<dd>{count(groups.length)}</dd>', '<dd><AreaMetric count={groups.length} area={groups.reduce((n, g) => n + g.area_ha, 0)}/></dd>')
text = once(text, '<dd>{count(groups.reduce((n, g) => n + g.parcel_count, 0))}</dd>', '<dd><AreaMetric count={groups.reduce((n, g) => n + g.parcel_count, 0)} area={groups.reduce((n, g) => n + g.area_ha, 0)}/></dd>')
p.write_text(text, encoding='utf-8')
with (SRC / 'styles.css').open('a', encoding='utf-8') as f:
    f.write('''
/* Add hectares without changing the existing panel placement or map layout. */
.parcel-count-area { display: inline-flex; flex-direction: column; gap: 2px; max-width: 100%; }
.parcel-count-area small { font-size: 10px; font-weight: 600; color: #b7d4c6; white-space: nowrap; }
.parcel-class-metric { display: block; margin-top: 3px; font-size: 10px; color: #b7d4c6; font-weight: 600; }
.parcel-area-note { margin: 5px 0; color: #bbcec4; font-size: 10px; }
.activity-summary dd { min-width: 0; }
''')
print('All five analytics include official hectare displays')
