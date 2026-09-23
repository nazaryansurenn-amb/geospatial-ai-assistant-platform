import json
import time
import unittest
from wp_core.agent_presentation import presentation_issues,reader_context
from wp_core.agent_service_v3 import AgentService

CONTEXT={'scope':'lower_hrazdan','mode':'potential','include_expansion':True,'selected_code':None,'language':'hy'}
BAD='Ապացուցված 2026 թ. EO-ով դիտված օգտագործման վիճակն է։ scope = lower\\_hrazdan; mode = potential; gravity_candidate; annual; partial; Show on map'
GOOD='Այժմ բաց է «Ներուժ» բաժինը՝ Ստորին Հրազդան I + II տարածքի և հարակից 1 կմ գոտու համար։ Արբանյակային դիտարկումները չեն հաստատում փաստացի ջրամատակարարումը։'

def response(text):return {'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]}

class Presentation(unittest.TestCase):
    def test_human_context(self):
        text=reader_context(CONTEXT)
        self.assertIn('Ստորին Հրազդան I + II',text)
        self.assertIn('Ներուժ',text)
        self.assertIn('ներառված է',text)
        for key in ['lower_hrazdan','include_expansion','current_map_context','mode','true']:
            self.assertNotIn(key,text)

    def test_reported_regression(self):
        issues=presentation_issues(BAD,'hy')
        self.assertIn('internal identifier',issues)
        self.assertIn('configuration dump',issues)
        self.assertIn('untranslated interface or implementation term',issues)
        self.assertIn('unsupported claim of proven use',issues)
        self.assertFalse(presentation_issues(GOOD,'hy'))
        self.assertFalse(presentation_issues('Irrigation is not proven. The active class is a satellite-based estimate.','en'))

    def test_retry_never_delivers_bad_draft(self):
        requests=[]
        def responder(messages):
            requests.append(list(messages));return response(BAD if len(requests)==1 else GOOD)
        s=AgentService(responder);token=s.session()['token']
        try:
            job=s.submit(token,'Բացատրի՛ր այս հարթակի կառուցվածքն ու բաժինները։',CONTEXT)['job_id']
            deadline=time.monotonic()+5
            while s.status(token,job)['state']=='running' and time.monotonic()<deadline:time.sleep(.02)
            result=s.status(token,job)
            self.assertEqual(result['state'],'complete')
            self.assertEqual(result['answer'],GOOD)
            self.assertEqual(len(requests),2)
            self.assertEqual(requests[0][-1]['content'],'Բացատրի՛ր այս հարթակի կառուցվածքն ու բաժինները։')
            self.assertFalse(result['results'])
        finally:s.pool.shutdown()

    def test_persistent_jargon_fails_without_exposing_it(self):
        s=AgentService(lambda messages:response(BAD));token=s.session()['token']
        try:
            job=s.submit(token,'Բացատրի՛ր բաժինները։',CONTEXT)['job_id']
            deadline=time.monotonic()+5
            while s.status(token,job)['state']=='running' and time.monotonic()<deadline:time.sleep(.02)
            result=s.status(token,job)
            self.assertEqual(result['state'],'error')
            self.assertNotIn('answer',result)
            self.assertFalse(presentation_issues(result['error'],'hy'))
            self.assertEqual(s.sessions[token]['turns'],[])
        finally:s.pool.shutdown()

if __name__=='__main__':unittest.main()
