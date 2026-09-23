import fs from 'node:fs/promises';
import {FileBlob,SpreadsheetFile} from '@oai/artifact-tool';
const out=new URL('../../output/agent_excel_v5/',import.meta.url);
const file=new URL('communities.xlsx',out).pathname.replace(/^\/([A-Z]:)/,'$1');
const wb=await SpreadsheetFile.importXlsx(await FileBlob.load(file));
console.log((await wb.inspect({kind:'table',range:'Աղյուսակ!A1:C5',include:'values',tableMaxRows:5,tableMaxCols:3,maxChars:1500})).ndjson);
console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!',options:{useRegex:true,maxResults:10},maxChars:1200})).ndjson);
for(const [name,sheetName,range] of [['summary','Ամփոփում','A1:B17'],['table','Աղյուսակ','A1:D24'],['chart','Աղյուսակ','F1:R31'],['parcels','Հողամասեր','A1:H10']]){
 const b=await wb.render({sheetName,range,scale:1.3,format:'png'});
 await fs.writeFile(new URL(name+'.png',out),new Uint8Array(await b.arrayBuffer()));
}
console.log('Workbook read and four views rendered');
