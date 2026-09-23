"""Human-facing vocabulary and a bounded response check; no numerical changes."""
import re

AREA_NAMES = {
 'hy': {'lower_hrazdan':'Ստորին Հրազդան I + II','stage_1':'Ստորին Հրազդան I հերթ','stage_2':'Ստորին Հրազդան II հերթ'},
 'en': {'lower_hrazdan':'Lower Hrazdan I + II','stage_1':'Lower Hrazdan Stage I','stage_2':'Lower Hrazdan Stage II'},
 'ru': {'lower_hrazdan':'Нижний Раздан I + II','stage_1':'Нижний Раздан, I очередь','stage_2':'Нижний Раздан, II очередь'},
}
TAB_NAMES = {
 'hy': {'activity_2026':'2026 ակտիվություն','land_use_type':'Օգտագործման տեսակ','history_2021_2025':'2021–2025 պատմություն','potential':'Ներուժ','consolidation':'Հողատարածքների կոնսոլիդացիայի հնարավորություն','activity_change':'Մշակման փոփոխություն 2021–2025','degradation':'Հողերի դեգրադացիայի հնարավոր նշաններ','irrigation_stress':'Ոռոգման սթրեսի հնարավոր նշաններ'},
 'en': {'activity_2026':'2026 activity','land_use_type':'Use type','history_2021_2025':'2021–2025 history','potential':'Potential','consolidation':'Land consolidation opportunity','activity_change':'Cultivation changes 2021-2025','degradation':'Possible signs of land degradation','irrigation_stress':'Possible signs of irrigation stress'},
 'ru': {'activity_2026':'Активность в 2026 году','land_use_type':'Тип использования','history_2021_2025':'История за 2021–2025 годы','potential':'Потенциал','consolidation':'Возможность консолидации земель','activity_change':'Изменения обработки 2021–2025','degradation':'Возможные признаки деградации земель','irrigation_stress':'Возможные признаки оросительного стресса'},
}

def reader_context(context):
    language=context['language']
    area=AREA_NAMES[language][context['scope']];tab=TAB_NAMES[language][context['mode']]
    labels={
      'hy': ('Ընտրված տարածք','Բաց բաժին','Ներուժի հարակից 1 կմ գոտի','ներառված է','ներառված չէ','Ընտրված հողամաս'),
      'en': ('Selected area','Open section','Adjacent 1 km search for potential','included','not included','Selected parcel'),
      'ru': ('Выбранная территория','Открытый раздел','Прилегающая полоса 1 км для потенциала','включена','не включена','Выбранный участок'),
    }[language]
    a,t,e,yes,no,p=labels
    text=f'{a}: {area}.\n{t}: {tab}.\n{e}: {yes if context["include_expansion"] else no}.'
    if context.get('selected_code'):text+=f'\n{p}: {context["selected_code"]}.'
    return 'Use this current map selection only when relevant. Describe it in ordinary language, never as software configuration.\n'+text

def presentation_issues(answer, language):
    normalized=answer.replace('\\_', '_').replace('**','')
    # Mixed/transliterated requests may produce Armenian even when the UI guessed English.
    latin=len(re.findall('[A-Za-z]',normalized))
    if len(re.findall('[Ա-ֆ]',normalized))>max(20,latin):language='hy'
    elif len(re.findall('[А-Яа-яЁё]',normalized))>max(20,latin):language='ru'
    issues=[]
    if re.search(r'(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])',normalized):
        issues.append('internal identifier')
    if re.search(r'\b(?:scope|mode|context)\s*=',normalized,re.I):
        issues.append('configuration dump')
    if language in ['hy','ru'] and re.search(r'(?<![A-Za-z])(?:scope|mode|context|active|partial|unresolved|annual|perennial|undetermined|household|review|candidate|screening|expansion|historical|pixel|true|false|potential|consolidation|activity|history|parcels?|EO|Show on map)(?![A-Za-z])',normalized,re.I):
        issues.append('untranslated interface or implementation term')
    if re.search(r'ապացուցված\s+(?:2026|օգտագործ|արբանյակ|դիտ|EO)|հաստատված\s+(?:2026.*օգտագործ|իրական\s+օգտագործ)',normalized,re.I):
        issues.append('unsupported claim of proven use')
    return issues

def rewrite_request(language):
    name={'hy':'Armenian','en':'English','ru':'Russian'}[language]
    return f'''Rewrite the preceding answer for a normal platform user, entirely in {name}.
Use the visible interface names and natural prose; remove all software identifiers,
configuration dumps, English class names in Armenian/Russian, and implementation jargon.
Describe satellite-observed activity without calling actual land use proven or confirmed.
Preserve quantities, dates, uncertainty and meaning; add no new calculations or facts.
Do not call tools. Return only the corrected user-facing answer, with no editing commentary.'''
