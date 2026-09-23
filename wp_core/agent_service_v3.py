"""Bounded Responses API orchestration; only deterministic, read-only tools."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request

from wp_core.agent_knowledge_v3 import instructions
from wp_core.agent_presentation import reader_context, presentation_issues, rewrite_request
from wp_core.agent_queries import CLASSES, TOPICS, SCOPES, COMMUNITY_NAMES, QueryError, build_result, parcel_result, persist_result

ROOT = Path(__file__).resolve().parents[1]
TTL = 7200

def nullable_enum(values):
    return {'type':['string','null'], 'enum':values+[None]}

QUERY_PROPERTIES = {
    'topic': {'type':'string','enum':TOPICS},
    'communities': {'type':'array','items':{'type':'string'},'description':'Community names, e.g. Aknalich or Ակնալիճ; [] means all within selected scope.'},
    'scope': nullable_enum(SCOPES),
    'include_expansion': {'type':['boolean','null'],'description':'Only potential supports nearby 1 km; null inherits UI context.'},
    'households': nullable_enum(['exclude','include','only']),
    'classes': {'type':'array','items':{'type':'string'}, 'description':'[] for all (potential/consolidation default candidates). Valid by topic: '+json.dumps(CLASSES)},
    'min_area_ha': {'type':['number','null']},
    'max_area_ha': {'type':['number','null']},
    'group_by': nullable_enum(['none','class','community','stage','year']),
    'years': {'type':'array','items':{'type':'integer'},'description':'Activity [2026]; history subset 2021-2025; other topics full 2021-2025. [] uses default.'},
}

TOOLS = [
    {'type':'function','name':'measure_active_area','description':'Use for "how many hectares are actively used" or measured/observed active hectares in 2026. Includes measured active portions across ALL activity classes, plus official area, coverage and class breakdown. Never substitute the active-class subset for this measurement.',
     'strict':True,'parameters':{'type':'object','properties':{k:v for k,v in QUERY_PROPERTIES.items() if k in ['communities','scope','households','min_area_ha','max_area_ha','group_by']},
     'required':['communities','scope','households','min_area_ha','max_area_ha','group_by'],'additionalProperties':False}},
    {'type':'function','name':'analyze_land','description':'Calculate counts, official hectares, measured active hectares, existing class and year/community breakdowns. Returns an attached table, chart, CSV and map-selection card.',
     'strict':True,'parameters':{'type':'object','properties':QUERY_PROPERTIES,'required':list(QUERY_PROPERTIES),'additionalProperties':False}},
    {'type':'function','name':'get_parcel_profile','description':'Read prepared analytical profile for an exact cadastral code. This is not an operational identifier.',
     'strict':True,'parameters':{'type':'object','properties':{'code':{'type':'string'}},'required':['code'],'additionalProperties':False}},
]

class ServiceError(Exception):
    pass

def credentials():
    values={}
    path=ROOT/'.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k,v=line.split('=',1); values[k.strip()]=v.strip().strip('"').strip("'")
    return values.get('OPENAI_API_KEY'), values.get('OPENAI_COPILOT_MODEL','gpt-5.4-mini')

def connection_enabled():
    """Explicitly enabled only after owner approval of the external aggregate payload."""
    path=ROOT/'.env'
    return path.exists() and any(line.strip()=='OPENAI_AGENT_ENABLED=true' for line in path.read_text(encoding='utf-8-sig').splitlines())

def openai_response(messages):
    if not connection_enabled():
        raise ServiceError('The external AI connection is awaiting owner activation.')
    key,model=credentials()
    if not key:
        raise ServiceError('The local AI connection is not configured.')
    payload={'model':model,'store':False,'instructions':instructions(), 'input':messages,
             'tools':TOOLS,'parallel_tool_calls':False,'max_output_tokens':2400,'reasoning':{'effort':'low'},'include':['reasoning.encrypted_content']}
    request=urllib.request.Request('https://api.openai.com/v1/responses',
        data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
        headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(request,timeout=55) as response:
            value=json.loads(response.read(2_000_000))
    except urllib.error.HTTPError as exc:
        # Never expose or log the remote response body, headers, request or key.
        text={401:'The configured AI key was rejected.',403:'The AI account cannot access this model.',
              429:'The AI connection is at its usage limit; try again later.'}.get(exc.code,'The AI service could not complete this request.')
        raise ServiceError(text) from None
    except (urllib.error.URLError,TimeoutError,OSError,ValueError):
        raise ServiceError('The AI connection timed out or is temporarily unavailable.') from None
    if value.get('status')!='completed':
        raise ServiceError('The AI answer did not finish. Try a shorter question.')
    return value

def clean_context(value):
    if not isinstance(value,dict) or set(value)-{'scope','mode','include_expansion','selected_code','language'}:
        raise QueryError('Invalid map context')
    scope=value.get('scope','lower_hrazdan')
    if scope not in SCOPES:
        raise QueryError('This area has no prepared analysis.')
    mode=value.get('mode','activity_2026')
    if mode not in ['activity_2026','history_2021_2025','land_use_type','potential','consolidation']:
        raise QueryError('Invalid analysis tab')
    expansion=value.get('include_expansion',False)
    if type(expansion) is not bool:
        raise QueryError('Invalid expansion setting')
    code=value.get('selected_code')
    if code is not None and (not isinstance(code,str) or not re.fullmatch(r'\d{2}-\d{3}-\d{4}-\d{4}',code)):
        raise QueryError('Invalid parcel context')
    language=value.get('language','hy')
    if language not in ['hy','en','ru']:raise QueryError('Invalid language')
    return {'scope':scope,'mode':mode,'include_expansion':expansion,'selected_code':code,'language':language}

class AgentService:
    def __init__(self, responder=openai_response):
        self.sessions={}; self.jobs={}; self.lock=threading.RLock()
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='land-agent')
        self.responder=responder; self.calls=[]

    def session(self):
        with self.lock:
            now=time.monotonic()
            for token,s in list(self.sessions.items()):
                if now-s['touched']>TTL and not s['busy']:
                    del self.sessions[token]
            for jid,j in list(self.jobs.items()):
                if j['owner'] not in self.sessions: del self.jobs[jid]
            if len(self.sessions)>=50:raise ServiceError('Too many open sessions. Please try again later.')
            token=secrets.token_urlsafe(32)
            self.sessions[token]={'touched':now,'turns':[],'results':set(),'busy':None}
            return {'token':token,'configured':bool(credentials()[0]) and connection_enabled()}

    def authorize(self,token):
        s=self.sessions.get(token)
        if not s or time.monotonic()-s['touched']>TTL:
            raise PermissionError('Session expired; reopen the assistant.')
        s['touched']=time.monotonic()
        return s

    def submit(self,token,text,context):
        if not isinstance(text,str) or not text.strip() or len(text)>4000:
            raise QueryError('Enter a question of up to 4000 characters.')
        context=clean_context(context)
        with self.lock:
            s=self.authorize(token)
            if s['busy']:raise ServiceError('This session already has a question running.')
            active=sum(j['state']=='running' for j in self.jobs.values())
            if active>=2:raise ServiceError('The assistant is busy. Please try again shortly.')
            jid=secrets.token_hex(16)
            job={'owner':token,'state':'running','phase':'understanding','cancel':threading.Event(),'results':[]}
            self.jobs[jid]=job; s['busy']=jid
            self.pool.submit(self._run,token,jid,text.strip(),context)
            return {'job_id':jid}

    def status(self,token,jid):
        with self.lock:
            self.authorize(token)
            j=self.jobs.get(jid)
            if not j or j['owner']!=token:raise PermissionError('Result is not available in this session.')
            return {k:v for k,v in j.items() if k not in ['owner','cancel']}

    def cancel(self,token,jid):
        with self.lock:
            self.status(token,jid)
            j=self.jobs[jid]
            if j['state']=='running':
                j['cancel'].set();j['state']='cancelled';j['phase']='cancelled'
            return {'state':j['state']}

    def result_path(self,token,rid,kind):
        with self.lock:
            s=self.authorize(token)
            if not re.fullmatch('[a-f0-9]{32}',rid) or rid not in s['results']:
                raise PermissionError('Result is not available in this session.')
        name={'map':'map.json','csv':'parcels.csv','summary':'result.json'}.get(kind)
        if not name:raise QueryError('Invalid result format')
        return ROOT/'server_data/agent_v1/results'/rid/name

    def _run(self,token,jid,text,context):
        job=self.jobs[jid];s=self.sessions[token]
        try:
            messages=[]
            for turn in s['turns'][-5:]:messages.extend(turn)
            current=[{'role':'developer','content':reader_context(context)}, {'role':'user','content':text}]
            messages+=current
            presentation_retries=0
            for round_number in range(8):
                if job['cancel'].is_set():return
                with self.lock:
                    now=time.monotonic();self.calls=[t for t in self.calls if now-t<86400]
                    if len(self.calls)>=250:raise ServiceError('The local daily AI request limit has been reached.')
                    self.calls.append(now)
                    job['phase']='understanding' if round_number==0 else 'explaining'
                response=self.responder(messages)
                if job['cancel'].is_set():return
                outputs=response.get('output',[])
                current+=outputs;messages+=outputs
                calls=[o for o in outputs if o.get('type')=='function_call']
                if not calls:
                    answer='\n'.join(c.get('text','') for o in outputs if o.get('type')=='message' for c in o.get('content',[]) if c.get('type')=='output_text')
                    if not answer.strip():raise ServiceError('No answer was returned. Please try again.')
                    if presentation_issues(answer,context['language']):
                        if presentation_retries>=2:
                            raise ServiceError({'hy':'Պատասխանը չհաջողվեց հստակ ձևակերպել։ Խնդրում ենք կրկնել հարցը։',
                                                'en':'The answer could not be phrased clearly. Please try again.',
                                                'ru':'Не удалось ясно сформулировать ответ. Повторите вопрос.'}[context['language']])
                        presentation_retries+=1
                        correction={'role':'developer','content':rewrite_request(context['language'])}
                        messages.append(correction);current.append(correction)
                        continue
                    with self.lock:
                        if job['cancel'].is_set():return
                        job.update(state='complete',phase='complete',answer=answer)
                        s['turns'].append(current);s['turns']=s['turns'][-5:]
                    return
                if len(calls)>5:raise ServiceError('Please narrow this comparison to fewer results.')
                for call in calls:
                    if job['cancel'].is_set():return
                    job['phase']='calculating'
                    try:
                        args=json.loads(call['arguments'])
                        if call['name']=='analyze_land':
                            raw=build_result(args,context,context['language'])
                        elif call['name']=='measure_active_area':
                            if set(args)-{'communities','scope','households','min_area_ha','max_area_ha','group_by'}:
                                raise QueryError('Observed active area uses the whole requested population, not a selected activity class.')
                            raw=build_result({**args,'topic':'activity','classes':[],'include_expansion':False,'years':[2026]},context,context['language'])
                            raw['measurement']='observed_active_area_across_all_activity_classes'
                        elif call['name']=='get_parcel_profile' and set(args)=={'code'}:
                            raw=parcel_result(args['code'],context['language'])
                        else:raise QueryError('Unsupported operation')
                        result=persist_result(raw)
                        with self.lock:
                            s['results'].add(result['result_id']);job['results'].append(result)
                        tool_result={k:v for k,v in result.items() if k not in ['result_id','map_available']}
                    except (ValueError,TypeError,KeyError) as exc:
                        tool_result={'error':str(exc) if isinstance(exc,QueryError) else 'Invalid tool arguments. Correct the structured query.'}
                    reply={'type':'function_call_output','call_id':call['call_id'],'output':json.dumps(tool_result,ensure_ascii=False,allow_nan=False)}
                    current.append(reply);messages.append(reply)
            raise ServiceError('This question needs too many steps. Please split it into smaller comparisons.')
        except ServiceError as exc:
            if not job['cancel'].is_set():job.update(state='error',phase='error',error=str(exc))
        except Exception:
            if not job['cancel'].is_set():job.update(state='error',phase='error',error='The prepared data could not be read. No analytical rules or data were changed.')
        finally:
            with self.lock:s['busy']=None

def install_handler(base,service):
    from urllib.parse import urlsplit
    class Handler(base):
        def _local(self,post=False):
            port=self.server.server_port
            allowed={f'127.0.0.1:{port}',f'localhost:{port}'}
            if self.client_address[0] not in ['127.0.0.1','::1'] or self.headers.get('Host') not in allowed:
                raise PermissionError('Local access only.')
            origin=self.headers.get('Origin')
            if (post and not origin) or (origin and origin not in {'http://'+h for h in allowed}):
                raise PermissionError('Same-origin access required.')
            if self.headers.get('Sec-Fetch-Site') in ['cross-site','same-site']:
                raise PermissionError('Same-origin access required.')

        def _token(self):
            return self.headers.get('X-Agent-Session','')

        def _error(self,exc):
            status=403 if isinstance(exc,PermissionError) else 400 if isinstance(exc,QueryError) else 503
            message=str(exc) if isinstance(exc,(PermissionError,QueryError,ServiceError)) else 'The assistant could not complete this request.'
            self._send_json({'error':message},status=status)

        def do_POST(self):
            path=urlsplit(self.path).path
            if not path.startswith('/api/agent/'):
                self.send_error(405);return
            try:
                self._local(post=True)
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=24000 or self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    raise QueryError('A bounded JSON request is required.')
                body=json.loads(self.rfile.read(size))
                if not isinstance(body,dict):raise QueryError('Invalid request')
                if path=='/api/agent/session' and not body:
                    out=service.session()
                elif path=='/api/agent/message' and set(body)=={'text','context'}:
                    out=service.submit(self._token(),body['text'],body['context'])
                elif path=='/api/agent/cancel' and set(body)=={'job_id'} and isinstance(body['job_id'],str):
                    out=service.cancel(self._token(),body['job_id'])
                else:raise QueryError('Unsupported request')
                self._send_json(out)
            except Exception as exc:self._error(exc)

        def do_GET(self):
            path=urlsplit(self.path).path
            if not path.startswith('/api/agent/'):
                return super().do_GET()
            try:
                self._local()
                parts=path.strip('/').split('/')
                if len(parts)==4 and parts[2]=='job':
                    self._send_json(service.status(self._token(),parts[3]))
                elif len(parts)==5 and parts[2]=='result':
                    file=service.result_path(self._token(),parts[3],parts[4])
                    data=file.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type','text/csv; charset=utf-8' if parts[4]=='csv' else 'application/json; charset=utf-8')
                    if parts[4]=='csv':self.send_header('Content-Disposition','attachment; filename="land-parcels.csv"')
                    self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
                else:raise QueryError('Unknown assistant endpoint')
            except Exception as exc:self._error(exc)
    return Handler
