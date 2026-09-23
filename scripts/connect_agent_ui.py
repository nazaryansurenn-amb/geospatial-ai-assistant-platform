"""Minimal additive hooks in the copied review, preserving the existing sidebar."""
from pathlib import Path
root=Path(__file__).resolve().parents[1]
src=root/'server_data/review/agent_review_20260906_v1/frontend_source/src'
app=src/'App.jsx'
text=app.read_text(encoding='utf-8')
assert 'import AgentPanel' not in text
text='import AgentPanel from "./AgentPanel";\n'+text
text=text.replace('  const [layerPanelOpen, setLayerPanelOpen] = useState(true);',
    '  const [layerPanelOpen, setLayerPanelOpen] = useState(true);\n  const [agentExpansion, setAgentExpansion] = useState(true);')
text=text.replace('<PotentialView map={mapRef.current}', '<PotentialView onExpansionChange={setAgentExpansion} map={mapRef.current}')
text=text.replace('      <header className="identity-bar">',
    '      <AgentPanel map={mapRef.current} ready={mapReady} scope={landScope} mode={landMode} includeExpansion={agentExpansion} selectedCode={selectedCode} />\n\n      <header className="identity-bar">')
# Existing selection uses selectedParcel, inspected before connecting below.
text=text.replace('selectedCode={selectedCode}', 'selectedCode={null}')
app.write_text(text,encoding='utf-8')
p=src/'PotentialView.jsx';text=p.read_text(encoding='utf-8')
text=text.replace('({map,ready,payload,scope,active})','({map,ready,payload,scope,active,onExpansionChange})')
text=text.replace('  const expansion=payload?.expansion;', '  useEffect(()=>{onExpansionChange?.(includeExpansion);},[includeExpansion,onExpansionChange]);\n  const expansion=payload?.expansion;')
p.write_text(text,encoding='utf-8')
print('Agent added independently; existing layers panel and five tabs retained.')
