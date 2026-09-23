import React from 'react';

// Safe React text rendering: no HTML or executable markup from model responses.
function inline(text){return text.split(/(\*\*[^*]+\*\*)/g).map((part,i)=>part.startsWith('**')?<strong key={i}>{part.slice(2,-2)}</strong>:part);}
export default function AgentMessage({text}){
  const lines=String(text||'').split('\n'),blocks=[];
  for(let i=0;i<lines.length;i++){
    const line=lines[i].trim();if(!line)continue;
    if(line.startsWith('|')&&lines[i+1]?.trim().match(/^\|?[\s:|-]+\|$/)){
      const cells=s=>s.trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim());
      const headers=cells(line),rows=[];i+=2;
      while(i<lines.length&&lines[i].trim().startsWith('|')){rows.push(cells(lines[i]));i++;}i--;
      blocks.push(<div key={i} className="agent-table-wrap"><table><thead><tr>{headers.map((c,j)=><th key={j}>{inline(c)}</th>)}</tr></thead><tbody>{rows.map((r,j)=><tr key={j}>{r.map((c,k)=><td key={k}>{inline(c)}</td>)}</tr>)}</tbody></table></div>);
    }else if(/^[-*] |^\d+[.)] /.test(line)){
      const items=[line.replace(/^([-*]|\d+[.)])\s+/, '')];
      while(i+1<lines.length&&/^\s*([-*]|\d+[.)])\s+/.test(lines[i+1]))items.push(lines[++i].trim().replace(/^([-*]|\d+[.)])\s+/,''));
      blocks.push(<ul key={i}>{items.map((s,j)=><li key={j}>{inline(s)}</li>)}</ul>);
    }else if(/^#{1,6} /.test(line))blocks.push(<p className="agent-message-heading" key={i}><strong>{inline(line.replace(/^#{1,6}\s+/,''))}</strong></p>);
    else blocks.push(<p key={i}>{inline(line)}</p>);
  }
  return <div className="agent-message-text">{blocks}</div>;
}
