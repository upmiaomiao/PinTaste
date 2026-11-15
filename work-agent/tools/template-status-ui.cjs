const fs=require('fs'),path=require('path'),crypto=require('crypto');
let ts;for(const name of [process.env.WUYU_TYPESCRIPT_MODULE,'typescript','/opt/dsh-harness/node_modules/typescript'].filter(Boolean)){try{ts=require(name);break}catch{}}if(!ts)throw Error('TypeScript parser required');
const [input,output]=process.argv.slice(2);if(!input||!output)throw Error('Usage: node template-status-ui.cjs baseline.html output.html');
const html=fs.readFileSync(input,'utf8'),sha=x=>crypto.createHash('sha256').update(x).digest('hex');if(sha(html)!=='3ef566402b0bde4ae9345c01c1af29906c5be984bb835b6d7f2acfe89caf9105')throw Error('Actual panel baseline changed');
const match=/<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html),code=match[1],offset=match.index+match[0].indexOf(code),ast=ts.createSourceFile('panel.js',code,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS),nodes={};
function visit(n){if(ts.isFunctionDeclaration(n)&&['Ci','Oe','U$','$e','P$','ke'].includes(n.name?.text))nodes[n.name.text]=n;ts.forEachChild(n,visit)}visit(ast);if(Object.keys(nodes).length!==6)throw Error('Expected UI functions missing');
const replace=(text,from,to)=>{if(text.split(from).length!==2)throw Error('UI fragment changed: '+from.slice(0,60));return text.replace(from,to)};
const edits=[];for(const[name,node]of Object.entries(nodes)){
 const old=node.getText(ast);let next=old;
 if(name==='Oe')next='function Oe(){return k.materialId?k.snapshot.materials?.find(item=>item.id===k.materialId):k.tab==="upload"?null:k.snapshot.detail}';
 if(name==='Ci')next='function Ci(){const pages=pr(),required=__wuyuTemplateRequired();return (__wuyuTemplatePrivate()||Oe()?.reference_sufficient===true)&&pages.length>=required&&pages.slice(0,required).every(page=>k.loaded.has(page.image_url))}';
 if(name==='U$'){
  next=replace(next,'P$();','P$();__wuyuTemplateEnsure();');
  next=replace(next,'${a.ready?(__wuyu_panel18_text("查看版式")):a.status==="failed"?(__wuyu_panel18_text("解析未完成")):(__wuyu_panel18_text("解析中"))}','${$(__wuyuTemplateMaterialState(a))}');
  next=replace(next,'${t?(__wuyu_panel18_text("已确认这套模板")):r.length<5?(__wuyu_panel18_text("此模板不足5页，展示实际可用页面")):(__wuyu_panel18_text("查看五页版式后，确认使用"))}','${$(__wuyuTemplateConfirmationHint())}');
  next=replace(next,'${k.error?','${__wuyuTemplateWatchNotice()}${k.error?');
 }
 if(name==='$e')next=replace(next,'k.loading=!1,ze()','k.loading=!1,__wuyuTemplateSchedule(),ze()');
 if(name==='P$')next=replace(next,'k.handle=m.handle,','__wuyuTemplateWatchReset(),k.handle=m.handle,');
 if(name==='ke')next=replace(next,'${m.previewError?','${__wuyuReadingResetNotice()}${m.previewError?');
 edits.push({start:node.getStart(ast),end:node.getEnd(),text:next,name});
}
let result=code;for(const e of edits.sort((a,b)=>b.start-a.start))result=result.slice(0,e.start)+e.text+result.slice(e.end);result+='\n'+fs.readFileSync(path.join(__dirname,'template-status-panel.js'),'utf8');
const out=html.slice(0,offset)+result+html.slice(offset+code.length);fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,out);console.log(JSON.stringify({before:sha(html),after:sha(out),functions:edits.map(e=>e.name)}));
