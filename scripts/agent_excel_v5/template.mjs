import fs from 'node:fs/promises';
import {SpreadsheetFile,Workbook} from '@oai/artifact-tool';
const out=new URL('../../server_data/agent_v5/',import.meta.url);
await fs.mkdir(out,{recursive:true});
const wb=Workbook.create();
const names=['Ամփոփում','Աղյուսակ','Հողամասեր'];
const headings=[['Ցուցանիշ','Արժեք'],['Խումբ / տարի','Հողամաս','Կադաստրային հա','Դիտվող ակտիվ հա'],['Կադաստրային կոդ','Համայնք','Կադաստրային հա','Հերթ','Տարածք','Դաս','Տարի','Դիտվող ակտիվ հա']];
for(let i=0;i<3;i++){
 const s=wb.worksheets.add(names[i]);s.showGridLines=false;s.freezePanes.freezeRows(1);
 s.getRangeByIndexes(0,0,1,headings[i].length).values=[headings[i]];
 const prototype=i===0?['Հողամաս',1]:i===1?['Ակնալիճ',1,1.23,null]:['04-001-0001-0001','Ակնալիճ',1.23,'I հերթ','Ստորին Հրազդան I','Թեկնածու',2025,null];
 s.getRangeByIndexes(1,0,1,prototype.length).values=[prototype];
 s.getRangeByIndexes(0,0,2,prototype.length).format.font={name:'Arial',size:11,color:'#233F35'};
 s.getRangeByIndexes(0,0,1,prototype.length).format={fill:'#185348',font:{name:'Arial',size:11,bold:true,color:'#FFFFFF'},rowHeight:40,wrapText:true};
 s.getRangeByIndexes(1,0,1,prototype.length).format.rowHeight=23;
 s.getRange('A:A').format.columnWidth=i===0?40:30;
 s.getRange('B:B').format.columnWidth=i===0?110:i===1?18:28;
 if(i>0){s.getRange('C:D').format.columnWidth=25;s.getRange('C2:D2').setNumberFormat('#,##0.00');}
 if(i===1)s.getRange('B2').setNumberFormat('#,##0');
 if(i===2){s.getRange('A2:B2').setNumberFormat('@');s.getRange('D2:F2').setNumberFormat('@');s.getRange('E:F').format.columnWidth=38;s.getRange('G:G').format.columnWidth=12;s.getRange('H:H').format.columnWidth=25;s.getRange('H2').setNumberFormat('#,##0.00');s.getRange('G2').setNumberFormat('0');}
}
const data=wb.worksheets.getItem('Աղյուսակ');
for(const [idx,col,title] of [[0,'B','Հողամասերի քանակ'],[1,'C','Կադաստրային մակերես (հա)'],[2,'D','Դիտվող ակտիվ մակերես (հա)']]){
 const c=data.charts.add('bar',[data.getRange('A1:A2'),data.getRange(`${col}1:${col}2`)]);
 c.title=title;c.hasLegend=false;c.titleTextStyle.typeface='Arial';c.titleTextStyle.fontSize=14;
 c.xAxis={axisType:'textAxis',textStyle:{typeface:'Arial',fontSize:11}};
 c.yAxis={numberFormatCode:idx===0?'#,##0':'#,##0.00',numberFormatSourceLinked:false,textStyle:{typeface:'Arial',fontSize:11}};
 c.series.items[0].fill='#25846C';c.setPosition(`F${1+idx*24}`,`Q${22+idx*24}`);
}
await(await SpreadsheetFile.exportXlsx(wb)).save(new URL('export_template.xlsx',out).pathname.replace(/^\/([A-Z]:)/,'$1'));
console.log('Excel template prepared');
