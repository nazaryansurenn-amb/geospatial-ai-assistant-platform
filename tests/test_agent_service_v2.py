import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from wp_core import agent_service_v2 as service

class IsolatedAgent(unittest.TestCase):
    def test_request_ignores_previous_agent_configuration(self):
        captured={};reads=[];original=Path.read_text
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return b'{"status":"completed","output":[]}'
        def capture(request,**kwargs):
            captured.update(json.loads(request.data));return Response()
        def read(path,*args,**kwargs):
            reads.append(path.resolve());return original(path,*args,**kwargs)
        with patch.dict(os.environ,{'OPENAI_SYSTEM_PROMPT':'OLD_AGENT_SENTINEL','OPENAI_ASSISTANT_ID':'OLD_AGENT_SENTINEL','OPENAI_BASE_URL':'https://old.invalid'}),patch.object(Path,'read_text',read),patch.object(service.urllib.request,'urlopen',capture):
            service.openai_response([{'role':'user','content':'Explain the five land tabs.'}])
        self.assertEqual(set(reads),{service.ROOT/'.env'})
        self.assertNotIn('OLD_AGENT_SENTINEL',json.dumps(captured))
        self.assertTrue({'assistant_id','previous_response_id','conversation','prompt'}.isdisjoint(captured))
        self.assertEqual(captured['model'],'gpt-5.4-mini')
        self.assertFalse(captured['store'])
        self.assertEqual(service.instructions.__module__,'wp_core.agent_knowledge_v2')
        self.assertEqual({t['name'] for t in captured['tools']},{'measure_active_area','analyze_land','get_parcel_profile'})

    def test_measurement_tool_preserves_all_activity_classes(self):
        def responder(messages):
            if messages[-1].get('type')=='function_call_output':
                return {'output':[{'type':'message','content':[{'type':'output_text','text':'Calculation complete.'}]}]}
            return {'output':[{'type':'function_call','name':'measure_active_area','call_id':'local-test',
                              'arguments':json.dumps({'communities':['Aknalich'],'scope':'lower_hrazdan','households':'exclude','min_area_ha':None,'max_area_ha':None,'group_by':'class'})}]}
        s=service.AgentService(responder);token=s.session()['token']
        self.assertEqual(s.sessions[token]['turns'],[])
        job=s.submit(token,'How many hectares are actively used?',{'scope':'lower_hrazdan','mode':'activity_2026','language':'en'})['job_id']
        try:
            deadline=time.monotonic()+10
            while s.status(token,job)['state']=='running' and time.monotonic()<deadline:time.sleep(.05)
            result=s.status(token,job);self.assertEqual(result['state'],'complete')
            card=result['results'][0]
            self.assertEqual(card['query']['classes'],[])
            self.assertEqual(card['summary']['parcel_count'],1406)
            self.assertAlmostEqual(card['summary']['observed_active_area_ha'],354.1724,5)
        finally:s.pool.shutdown()

if __name__=='__main__':unittest.main()
