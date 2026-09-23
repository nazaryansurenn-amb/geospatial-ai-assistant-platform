"""Local UI test only: deterministic fixture responder, never an external API call."""
from pathlib import Path
import json
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import run_agent_review
from wp_core.agent_service import AgentService

def fixture(messages):
    if messages[-1].get('type')=='function_call_output':
        r=json.loads(messages[-1]['output'])
        if 'error' in r: raise AssertionError(r)
        s=r['summary']
        return {'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':f"Local UI test — deterministic Python result, not a live model answer. Aknalich: {s['parcel_count']} parcels; {s['official_area_ha']:.2f} official ha; {s['observed_active_area_ha']:.2f} observed active ha through 2026-08-23."}]}]}
    args={'topic':'activity','communities':['Aknalich'],'scope':'lower_hrazdan','include_expansion':False,'households':'exclude','classes':[],'min_area_ha':None,'max_area_ha':None,'group_by':'class','years':[2026]}
    return {'output':[{'type':'function_call','name':'analyze_land','call_id':'fixture-call','arguments':json.dumps(args)}]}

class FixtureService(AgentService):
    def __init__(self):super().__init__(responder=fixture)
    def session(self):
        value=super().session();value['configured']=True;return value

if __name__=='__main__':
    assert sys.argv[1:]==['--port','8527']
    run_agent_review.AgentService=FixtureService
    run_agent_review.main()
