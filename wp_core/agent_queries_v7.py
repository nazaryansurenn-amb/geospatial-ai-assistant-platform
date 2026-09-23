"""Deterministic, read-only land questions over the owner's pinned review data."""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path
import json
import math
import re
import sqlite3
import unicodedata
import uuid

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / 'server_data/agent_v7/parcels.sqlite3'
RESULTS = ROOT / 'server_data/agent_v7/results'
DATE = '2026-08-23'
TOPICS = ['activity', 'history', 'history_summary', 'use_type', 'cycles', 'potential', 'consolidation', 'degradation', 'irrigation_stress']
SCOPES = ['lower_hrazdan', 'stage_1', 'stage_2']
CLASSES = {
    'activity': ['active', 'partial', 'no_current_activity', 'unresolved'],
    'history': ['active', 'partial', 'no_activity', 'unresolved'],
    'history_summary': ['stable_active', 'periodic', 'stable_no_activity', 'insufficient'],
    'use_type': ['annual', 'perennial', 'undetermined', 'household'],
    'cycles': ['single_cycle', 'two_cycle_recurring', 'unassessed'],
    'potential': ['gravity_candidate', 'mechanical_candidate', 'review', 'not_candidate'],
    'consolidation': ['candidate', 'not_candidate'],
    'degradation': ['possible_signs', 'not_flagged', 'unassessed'],
    'irrigation_stress': ['possible_irrigation_stress','not_flagged','unassessed','not_current_vegetation'],
}
LABELS = {
    'possible_irrigation_stress':['Ոռոգման սթրեսի հնարավոր նշաններ','Possible signs of irrigation stress','Возможные признаки оросительного стресса'],
    'not_current_vegetation':['Ընթացիկ աճող բուսականությունը չի հաստատվել','Current growing vegetation not established','Текущая растущая растительность не установлена'],
    'possible_signs': ['Դեգրադացիայի հնարավոր նշաններ', 'Possible signs of degradation', 'Возможные признаки деградации'],
    'not_flagged': ['Գնահատված ցուցանիշներով նշան չի առանձնացվել', 'No sign flagged in assessed indicators', 'Признаки не выделены по оцененным показателям'],
    'outside_mask': ['Այս գնահատման շրջանակից դուրս', 'Outside this assessment', 'Вне этой оценки'],
    'stable_active': ['Կայուն մշակվող', 'Consistently active', 'Устойчиво активные'],
    'periodic': ['Պարբերաբար մշակվող', 'Periodically active', 'Периодически активные'],
    'stable_no_activity': ['Կայուն առանց դիտվող ակտիվության', 'Consistently no observed activity', 'Устойчиво без наблюдаемой активности'],
    'insufficient': ['Լրացուցիչ ստուգում', 'Insufficient observations', 'Недостаточно наблюдений'],
    'household': ['Տնամերձ', 'Household land', 'Приусадебные'],
    'active': ['Ակտիվ', 'Active', 'Активные'], 'partial': ['Մասամբ ակտիվ', 'Partially active', 'Частично активные'],
    'no_current_activity': ['Ակտիվություն չի դիտվել', 'No current activity observed', 'Активность не наблюдалась'],
    'no_activity': ['Ակտիվություն չի դիտվել', 'No activity observed', 'Активность не наблюдалась'],
    'unresolved': ['Չգնահատված', 'Unresolved', 'Не оценено'],
    'annual': ['Միամյա', 'Annual', 'Однолетние'], 'perennial': ['Բազմամյա', 'Perennial', 'Многолетние'],
    'undetermined': ['Տեսակը չի որոշվել', 'Type undetermined', 'Тип не определен'],
    'single_cycle': ['Մեկ ցիկլ', 'Single cycle', 'Один цикл'],
    'two_cycle_recurring': ['Երկու ցիկլ', 'Two cycles', 'Два цикла'],
    'unassessed': ['Չգնահատված', 'Unassessed', 'Не оценено'],
    'gravity_candidate': ['Ինքնահոսի թեկնածու', 'Gravity candidate', 'Кандидат самотечного орошения'],
    'mechanical_candidate': ['Մեխանիկականի թեկնածու', 'Mechanical candidate', 'Кандидат механического орошения'],
    'review': ['Լրացուցիչ ստուգում', 'Further review', 'Дополнительная проверка'],
    'not_candidate': ['Թեկնածու չի առանձնացվել', 'Not selected as a candidate', 'Кандидат не выделен'],
    'candidate': ['Կոնսոլիդացիայի թեկնածու', 'Consolidation candidate', 'Кандидат консолидации'],
    'stage_1': ['I հերթ', 'Stage I', 'I очередь'], 'stage_2': ['II հերթ', 'Stage II', 'II очередь'],
    'all': ['Ընդամենը', 'Total', 'Всего'], 'unassigned': ['Չվերագրված', 'Unassigned', 'Не отнесено'],
}
COMMUNITY_NAMES = [
 ('Աղավնատուն','Aghavnatun','Агавнатун'),('Ակնալիճ','Aknalich','Акналич'),('Ակնաշեն','Aknashen','Акнашен'),
 ('Ամբերդ','Amberd','Амберд'),('Ապագա','Apaga','Апагa'),('Արագած','Aragats','Арагац'),('Արաքս','Araks','Аракс'),
 ('Առատաշեն','Aratashen','Араташен'),('Արևաշատ','Arevashat','Аревашат'),('Արգավանդ','Argavand','Аргаванд'),
 ('Արշալույս','Arshaluys','Аршалуйс'),('Արտիմետ','Artimet','Артимет'),('Այգեկ','Aygek','Айгек'),
 ('Այգեշատ','Aygeshat','Айгешат'),('Բաղրամյան','Baghramyan','Баграмян'),('Դաշտ','Dasht','Дашт'),
 ('Դողս','Doghs','Дохс'),('Ֆերիկ','Ferik','Ферик'),('Գայ','Gai','Гай'),('Գեղակերտ','Geghakert','Гегакерт'),
 ('Գեղանիստ','Geghanist','Геганист'),('Գրիբոեդով','Griboyedov','Грибоедов'),('Հայկաշեն','Haykashen','Айкашен'),
 ('Հայթաղ','Haytagh','Айтаг'),('Հովտամեջ','Hovtamej','Овтамеч'),('Ջրառատ','Jrarat','Джрарат'),
 ('Խորոնք','Khoronk','Хоронк'),('Լեռնամերձ','Lernamerdz','Лернамердз'),('Լուսագյուղ','Lusagyugh','Лусагюх'),
 ('Մեծամոր','Metsamor','Мецамор'),('Մերձավան','Merdzavan','Мердзаван'),('Մոնթեավան','Monteavan','Монтеаван'),
 ('Մրգաստան','Mrgastan','Мргастан'),('Մուսալեռ','Musaler','Мусалер'),('Նորակերտ','Norakert','Норакерт'),
 ('Փարաքար','Parakar','Паракар'),('Պտղունք','Ptghunk','Птхунк'),('Շահումյան','Shahumyan','Шаумян'),
 ('Տարոնիկ','Taronik','Тароник'),('Ծաղկալանջ','Tsaghkalanj','Цахкаландж'),('Ծաղկունք','Tsaghkunk','Цахкунк'),
 ('Ծիածան','Tsiatsan','Циацан'),('Վաղարշապատ','Vagharshapat','Вагаршапат'),('Ոսկեհատ','Voskehat','Воскеат'),
]
EXTRA_ALIASES = {'aknalitch':'Ակնալիճ', 'aknalij':'Ակնալիճ', 'arshaluis':'Արշալույս',
                 'arshaluys':'Արշալույս', 'echmiadzin':'Վաղարշապատ', 'ejmiatsin':'Վաղարշապատ',
                 'էջմիածին':'Վաղարշապատ', 'baghramian':'Բաղրամյան', 'gay':'Գայ', 'musaller':'Մուսալեռ'}


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(value)).casefold() if c.isalnum())


ALIASES = {normalized(v): row[0] for row in COMMUNITY_NAMES for v in row}
ALIASES.update({normalized(k): v for k, v in EXTRA_ALIASES.items()})


class QueryError(ValueError):
    pass


def language_index(language):
    return {'hy': 0, 'en': 1, 'ru': 2}.get(language, 1)


def label(value, language='en'):
    names = LABELS.get(str(value))
    if not names:
        names = next((r for r in COMMUNITY_NAMES if r[0] == value), None)
    return names[language_index(language)] if names else str(value)


def community(value):
    found = ALIASES.get(normalized(value))
    if not found:
        raise QueryError('Unknown community. Choose a name from the platform community list; do not substitute a nearest place.')
    return found


def scalar(value):
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    if hasattr(value, 'item'):
        return value.item()
    return value


def validate(args, context=None):
    context = context or {}
    allowed = {'topic','communities','scope','include_expansion','households','classes','min_area_ha','max_area_ha','group_by','years'}
    if not isinstance(args, dict) or set(args) - allowed:
        raise QueryError('Unsupported query fields')
    q = dict(args)
    q['topic'] = q.get('topic') or 'activity'
    if q['topic'] not in TOPICS:
        raise QueryError('Unsupported analytical topic')
    q['scope'] = q.get('scope') or context.get('scope') or 'lower_hrazdan'
    if q['scope'] not in SCOPES:
        raise QueryError('Only the prepared Lower Hrazdan I/II study scope is available, not the whole WUA.')
    names = q.get('communities') or []
    if not isinstance(names, list) or len(names) > 44 or any(not isinstance(v, str) or len(v) > 80 for v in names):
        raise QueryError('Invalid community list')
    q['communities'] = list(dict.fromkeys(community(n) for n in names))
    expansion = q.get('include_expansion')
    if expansion is not None and not isinstance(expansion, bool):
        raise QueryError('Expansion must be true or false')
    q['include_expansion'] = bool(expansion if expansion is not None else context.get('include_expansion', False))
    if q['topic'] != 'potential' and q['include_expansion']:
        if expansion is True:
            raise QueryError('The 1 km expansion has potential screening only; comparable activity/history/use-type measurements are not available there.')
        q['include_expansion'] = False
    q['households'] = q.get('households') or 'exclude'
    if q['households'] not in ['exclude', 'include', 'only']:
        raise QueryError('Invalid household selection')
    if q['topic'] in ['potential', 'consolidation', 'degradation', 'irrigation_stress'] and q['households'] != 'exclude':
        raise QueryError('Household parcels are excluded from these candidate methods.')
    classes = q.get('classes') or []
    if not isinstance(classes, list) or any(c not in CLASSES[q['topic']] for c in classes):
        raise QueryError('Invalid classes for this topic: ' + ', '.join(CLASSES[q['topic']]))
    if not classes and q['topic'] in ['potential', 'consolidation', 'degradation', 'irrigation_stress']:
        classes = ['gravity_candidate', 'mechanical_candidate'] if q['topic'] == 'potential' else (['possible_signs'] if q['topic']=='degradation' else (['possible_irrigation_stress'] if q['topic']=='irrigation_stress' else ['candidate']))
    q['classes'] = list(dict.fromkeys(classes))
    for name in ['min_area_ha','max_area_ha']:
        value = q.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value) or value < 0 or value > 100000):
            raise QueryError('Area filters must be finite, nonnegative hectares')
        q[name] = value
    if q['min_area_ha'] is not None and q['max_area_ha'] is not None and q['min_area_ha'] > q['max_area_ha']:
        raise QueryError('Minimum area exceeds maximum area')
    q['group_by'] = q.get('group_by') or ('community' if len(names) > 1 else 'class')
    if q['group_by'] not in ['none','class','community','stage','year']:
        raise QueryError('Unsupported grouping')
    years = q.get('years') or ([2026] if q['topic'] in ['activity','irrigation_stress'] else list(range(2021,2026)))
    if not isinstance(years, list) or len(years) > 5 or any(type(y) is not int for y in years):
        raise QueryError('Invalid years')
    if q['topic'] in ['activity','irrigation_stress'] and years != [2026]:
        raise QueryError('Observed active hectares are available for 2026 only. Use history for official parcel areas grouped by 2021-2025 annual states.')
    if q['topic'] not in ['activity','irrigation_stress'] and any(y not in range(2021,2026) for y in years):
        raise QueryError('Completed-season analyses use 2021-2025 only.')
    if q['topic'] in ['history_summary','use_type','cycles','potential','consolidation','degradation'] and sorted(set(years)) != list(range(2021,2026)):
        raise QueryError('This predominant/candidate result is a fixed 2021-2025 analytical version. Use history for individual-year activity comparisons.')
    q['years'] = sorted(set(years))
    return q


def load_frame(q, index=INDEX):
    clauses, params = ['road_excluded=0'], []
    if not q['include_expansion']:
        clauses.append("scope='inside_I_II'")
    if q['scope'] != 'lower_hrazdan':
        clauses.append('activity_stage=?'); params.append(q['scope'])
    if q['topic']=='degradation':
        clauses.append('degradation_included=1')
    if q['communities']:
        clauses.append('community IN (' + ','.join('?' for _ in q['communities']) + ')'); params.extend(q['communities'])
    if q['households'] != 'include':
        clauses.append('household=?'); params.append(int(q['households'] == 'only'))
    for name, operator in [('min_area_ha','>='),('max_area_ha','<=')]:
        if q[name] is not None:
            clauses.append('official_area_ha' + operator + '?'); params.append(q[name])
    with sqlite3.connect(Path(index).resolve().as_uri() + '?mode=ro', uri=True) as c:
        return pd.read_sql_query('SELECT * FROM parcels WHERE ' + ' AND '.join(clauses) + ' ORDER BY cadastre_code', c, params=params)


def area_sum(frame):
    return float(frame.official_area_ha.sum())


def observed(frame):
    support = frame.activity_state.eq('ready') & frame.observed_active_area_ha.notna()
    return float(frame.loc[support, 'observed_active_area_ha'].sum()) if support.any() else None


def build_result(args, context=None, language='en', index=INDEX):
    q = validate(args, context)
    frame = load_frame(q, index)
    topic = q['topic']
    if topic == 'activity':
        frame['class'] = frame.activity_class.where(frame.activity_state.eq('ready'), 'unresolved').fillna('unresolved')
    elif topic == 'history':
        frames=[]
        values=frame.annual_state_codes.map(lambda x: json.loads(x) if isinstance(x,str) and x else [])
        for year in q['years']:
            part=frame.copy();part['year']=year
            part['class']=values.map(lambda v: {1:'active',2:'partial',3:'no_activity'}.get(v[year-2021] if len(v)==5 else 0,'unresolved'))
            frames.append(part)
        frame=pd.concat(frames,ignore_index=True)
    elif topic == 'history_summary':
        frame['class'] = frame.history_class.fillna('insufficient')
    elif topic == 'use_type':
        frame['class'] = frame.crop_type.fillna('undetermined')
        frame.loc[frame.household.eq(1), 'class'] = 'household'
    elif topic == 'cycles':
        frame['class'] = frame.annual_cycle.fillna('unassessed')
    elif topic == 'irrigation_stress':
        frame['class'] = frame.stress_state.replace({'candidate':'possible_irrigation_stress'})
    elif topic == 'degradation':
        frame['class'] = frame.degradation_state.replace({'candidate':'possible_signs'})
    elif topic == 'potential':
        frame['class'] = frame.potential_class
    else:
        frame['class'] = frame.consolidation_group.notna().map({True:'candidate', False:'not_candidate'})
    denominator = frame.drop_duplicates('cadastre_code')
    if q['classes']:
        frame = frame[frame['class'].isin(q['classes'])].copy()
    unique = frame.drop_duplicates('cadastre_code')
    unresolved = frame[frame['class'].isin(['unresolved','undetermined','unassessed','review','insufficient'])].drop_duplicates('cadastre_code')
    keys = {'none':[], 'class':['class'], 'community':['community'], 'stage':['activity_stage'], 'year':[]}[q['group_by']]
    if topic == 'history':
        keys = ['year'] + keys
        if q['group_by'] == 'year':
            keys += ['class']
    elif q['group_by'] == 'year':
        raise QueryError('Year breakdown requires historical annual states. This result is a fixed-period summary.')
    if keys:
        groups = list(frame.groupby(keys, dropna=False, sort=True))
    else:
        groups = [(('all',), frame)]
    rows=[]
    for key, part in groups:
        key = key if isinstance(key,tuple) else (key,)
        annual_base = denominator
        row={'key':'|'.join(str(scalar(v)) for v in key), 'label':' · '.join(label(scalar(v),language) for v in key),
             'parcel_count':int(part.cadastre_code.nunique()), 'official_area_ha':area_sum(part.drop_duplicates('cadastre_code')),
             'observed_active_area_ha':observed(part) if topic == 'activity' else None,
             'boundary_crossing_count':int(part.drop_duplicates('cadastre_code').boundary_crossing.sum())}
        row['official_area_share_percent'] = row['official_area_ha']/area_sum(annual_base)*100 if area_sum(annual_base)>0 else None
        if topic=='consolidation':
            row['group_count']=int(part.consolidation_group.nunique())
        rows.append(row)
    summary = {'parcel_count':len(unique), 'official_area_ha':area_sum(unique),
               'observed_active_area_ha':observed(unique) if topic=='activity' else None,
               'observed_measurement_parcels':int((unique.activity_state.eq('ready') & unique.observed_active_area_ha.notna()).sum()) if topic=='activity' else None,
               'unresolved_parcels':len(unresolved),
               'unresolved_official_area_ha':area_sum(unresolved),
               'boundary_crossing_parcels':int(unique.boundary_crossing.sum()),
               'denominator_parcels':len(denominator), 'denominator_official_area_ha':area_sum(denominator),
               'official_area_share_percent':area_sum(unique)/area_sum(denominator)*100 if area_sum(denominator)>0 else None}
    if topic=='consolidation':
        summary['group_count']=int(unique.consolidation_group.nunique())
        summary['whole_groups_only']=not bool(q['communities'] or q['min_area_ha'] is not None or q['max_area_ha'] is not None)
    if topic=='activity':
        summary['observed_active_percent_of_measured_parcel_area']=None
        measured=unique[unique.activity_state.eq('ready') & unique.observed_active_area_ha.notna()]
        summary['observed_measurement_official_area_ha']=area_sum(measured)
        if area_sum(measured)>0:
            summary['observed_active_percent_of_measured_parcel_area']=observed(measured)/area_sum(measured)*100
    notes = ['Scope covers only the selected prepared Lower Hrazdan I/II parcels, not the whole community or a confirmed hydraulic command area.',
             'Hectares are unchanged official whole-parcel areas. Community membership uses greatest overlap with saved project community areas; cross-boundary parcels are identified.']
    if topic=='activity':
        notes += ['2026 is incomplete; observations are through 2026-08-23. Observed active area is distinct from official parcel area. Missing measurements remain unknown.']
    if topic=='history':
        notes += ['Historical hectares are current official parcel areas grouped by annual activity state, not measured historical active-pixel hectares. Rows refer to their labelled year; do not add the years together.',
                  'Summary counts and areas are unique parcels matching in any selected year. An unresolved year is never treated as inactivity.']
    if topic in ['potential','consolidation']:
        notes += ['These are preliminary screening candidates. Actual feasibility, legal availability, water supply and ownership agreement are not established.']
    if topic=='degradation':
        summary['assessment_unassessed_parcels']=int(denominator.degradation_state.eq('unassessed').sum())
        summary['assessment_unassessed_official_area_ha']=area_sum(denominator[denominator.degradation_state.eq('unassessed')])
        notes += ['Possible vegetation signs only, not confirmed soil degradation, salinity or measured damaged hectares. This first five-year screen has no independent accuracy validation.', 'Historically active or partly active in at least one 2021-2025 year, inside the selected Lower Hrazdan study area, excluding households and roads. All five years are examined. Missing support stays unassessed; absence of a flag does not certify healthy land.', 'Vegetation deterioration and persistent low performance are distinct indicators displayed with one colour. Climate, water access and management remain possible explanations.']
    if topic=='irrigation_stress':
        summary['assessment_unassessed_parcels']=int(denominator.stress_state.eq('unassessed').sum())
        summary['assessment_unassessed_official_area_ha']=area_sum(denominator[denominator.stress_state.eq('unassessed')])
        summary['assessed_parcels']=int(denominator.stress_state.isin(['candidate','not_flagged']).sum())
        dates=unique.stress_observation_date.dropna()
        summary['observation_date_from']=str(dates.min()) if len(dates) else None
        summary['observation_date_to']=str(dates.max()) if len(dates) else None
        notes += ['Possible optical signs of irrigation stress as of 5 September 2026, not confirmed irrigation failure, a root-zone measurement, a water amount or an instruction to irrigate. Weather is ECMWF model estimates, not ERA5 or station observations. Only weather preceding each satellite observation is used; no forecast-risk layer is calculated.', 'Recent growing vegetation is compared with its own earlier observations, comparable nearby vegetation and prior covered seasons. Missing evidence stays unassessed, not healthy. Harvesting, normal seasonal change, soil and management can still explain an apparent signal. No independent field accuracy has been measured.', 'Area-weighted 20 m moisture observations may share boundary pixels across adjacent parcels. They are shared screening evidence, not independent parcel confirmation. Official hectares are whole cadastral parcel areas, not measured stressed vegetation area.']
    if topic=='potential':
        notes += ['Higher terrain than canal reference means mechanical candidate; lower means gravity candidate. This is terrain screening, not operating water-level or hydraulic verification.']
    if topic=='consolidation' and not summary['whole_groups_only']:
        notes += ['The filters select group members: group_count means touched groups, and hectares refer only to selected members, not necessarily complete groups.']
    if topic in ['use_type','cycles']:
        notes += ['Preserved Transitions v3 predominant 2021-2025 classification; annual uncertain cycle maps to single cycle after aggregation. Categories are provisional and do not identify specific crops.']
    if q['households']!='exclude' and topic!='activity':
        notes += ['Household parcels have no equivalent open-field multi-year classification; unresolved household records remain unassessed.']
    return {'query':q,'summary':summary,'rows':rows,'notes':notes,'observation_date':DATE if topic=='activity' else ('2026-09-05' if topic=='irrigation_stress' else None),
            'years':q['years'],'language':language,'_frame':frame,'_unique':unique}


def persist_result(result, directory=RESULTS):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    rid=uuid.uuid4().hex
    out=directory/rid;out.mkdir()
    frame=result['_frame'];unique=result['_unique']
    columns=['cadastre_code','community','official_area_ha','activity_stage','scope','class']
    if result.get('query',{}).get('topic')=='irrigation_stress':
        columns.append('stress_observation_date')
    if 'year' in frame:
        columns.append('year')
    if result.get('query',{}).get('topic')=='activity':
        frame=frame.copy()
        frame['observed_active_area_ha']=frame.observed_active_area_ha.where(frame.activity_state.eq('ready'))
        columns.append('observed_active_area_ha')
    frame[columns].to_csv(out/'parcels.csv',index=False,encoding='utf-8-sig')
    public={k:v for k,v in result.items() if not k.startswith('_')}
    public['result_id']=rid
    public['map_available']=bool(len(unique))
    selection={'ids':[int(x) for x in unique.public_parcel_id], 'codes':unique.cadastre_code.tolist(),
               'bbox':[float(unique.minx.min()),float(unique.miny.min()),float(unique.maxx.max()),float(unique.maxy.max())] if len(unique) else None}
    (out/'map.json').write_text(json.dumps(selection,allow_nan=False))
    (out/'result.json').write_text(json.dumps(public,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    return public


def parcel_result(code, language='en', index=INDEX):
    if not isinstance(code,str) or not re.fullmatch(r'\d{2}-\d{3}-\d{4}-\d{4}',code):
        raise QueryError('Use the complete cadastral code, for example 04-013-0122-0013.')
    with sqlite3.connect(Path(index).resolve().as_uri()+'?mode=ro',uri=True) as c:
        frame=pd.read_sql_query('SELECT * FROM parcels WHERE cadastre_code=?',c,params=[code])
    if frame.empty:
        raise QueryError('This parcel is not in the prepared analysis register. No classification is inferred.')
    row=frame.iloc[0]
    profile={k:scalar(row[k]) for k in ['cadastre_code','community','official_area_ha','scope','household','road_excluded','activity_class','activity_state','observed_active_area_ha','crop_type','annual_cycle','potential_class','consolidation_group']}
    profile['irrigation_stress_state']=scalar(row.stress_state)
    profile['irrigation_stress_observation_date']=scalar(row.stress_observation_date)
    profile['degradation_state']=scalar(row.degradation_state)
    profile['vegetation_deterioration']=bool(row.degradation_deterioration)
    profile['persistent_low_performance']=bool(row.degradation_low_performance)
    if row.activity_state!='ready':
        profile['observed_active_area_ha']=None
    profile['annual_activity_states']=json.loads(row.annual_state_codes) if isinstance(row.annual_state_codes,str) else None
    frame['class']=row.potential_class
    return {'query':{'topic':'parcel','code':code},'profile':profile,'summary':{'parcel_count':1,'official_area_ha':float(row.official_area_ha)},
            'rows':[],'notes':['This is an analytical parcel profile, not verified field truth or proof of irrigation.',
            'Outside the saved I/II area only potential screening is available.',
            'Potential is higher/lower terrain screening; household and road exclusions remain unchanged.'],
            'years':[2021,2022,2023,2024,2025], 'observation_date':DATE,'language':language,'_frame':frame,'_unique':frame}
