// 154 upload-limit and safe confirmation feedback against the current runtime.
const fs=require('fs'),path=require('path'),crypto=require('crypto');
let ts;for(const name of [process.env.WUYU_TYPESCRIPT_MODULE,'typescript','/opt/dsh-harness/node_modules/typescript'].filter(Boolean)){try{ts=require(name);break}catch{}}if(!ts)throw Error('TypeScript parser required');
const[input,output]=process.argv.slice(2),raw=fs.readFileSync(input),html=raw.toString('utf8'),sha=x=>crypto.createHash('sha256').update(x).digest('hex');
if(sha(raw)!=='c97c4d8e5d3180ff00b0e0046173a5e3ab90b544127a1032930d09dd2b7ecfc1')throw Error('Actual panel baseline changed');
const match=/<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html);if(!match)throw Error('Panel module missing');
const code=match[1],offset=match.index+match[0].indexOf(code),marker='// Read-only template paging. Confirmation, previews and workflow actions stay in their original handlers.';
if(code.split(marker).length!==2)throw Error('Current helper marker changed');const helperStart=code.indexOf(marker);
if(sha(code.slice(helperStart))!=='234fd0eb6f5dee279a229691808ccb84a131eaff604e7f173ce9a1c730587012')throw Error('Current helper bytes changed');
const ast=ts.createSourceFile('panel.js',code,99,true,1),edits=[];
function replace(s,a,b){if(s.split(a).length!==2)throw Error('Current UI fragment changed: '+a);return s.replace(a,b)}
for(const n of ast.statements)if(ts.isFunctionDeclaration(n)&&['U$','O$','P$','$e'].includes(n.name?.text)){
 let next=n.getText(ast);
 if(n.name.text==='P$')next=replace(next,'k.error=!1,k.snapshot={}','k.error=!1,k.actionFailure=null,k.snapshot={}');
 if(n.name.text==='$e'){
  next=replace(next,'k.loading=!0,k.error=!1,ze()','k.loading=!0,k.error=!1,k.actionFailure=null,ze()');
  next=replace(next,'if(n!==m.handle||r!==k.sequence)return;if(!o.data','if(n!==m.handle||r!==k.sequence)return;__wuyuTemplate154Receive(o.data,e.operation);if(!o.data');
 }
 if(n.name.text==='O$')next=replace(next,'r.templateMaterials!==void 0?','r.templateLimitsRetry!==void 0?$e({operation:"materials"}):r.templateMaterials!==void 0?');
 if(n.name.text==='U$'){
  next=replace(next,'${k.error?(__wuyu_panel18_html(["<p class=\\"preview-error\\" role=\\"alert\\">模板暂未加载成功。<button type=\\"button\\" data-template-retry>重新加载</button></p>"])):""}','${k.error?__wuyuTemplate154Error():""}');
  next=replace(next,'data-template-upload ${!k.file||k.loading?','data-template-upload ${!__wuyuTemplate154UploadAllowed()||k.loading?');
  next=replace(next,'<button type="button" data-template-materials>','${__wuyuTemplate154UploadLimits()}<button type="button" data-template-materials>');
 }
 edits.push({start:n.getStart(ast),end:n.getEnd(),text:next,name:n.name.text});
}
if(edits.length!==4)throw Error('Expected UI functions missing');let result=code.slice(0,helperStart);
for(const e of edits.sort((a,b)=>b.start-a.start))result=result.slice(0,e.start)+e.text+result.slice(e.end);
result+=fs.readFileSync(path.join(__dirname,'template-browser-panel.js'),'utf8');
if(ts.createSourceFile('result.js',result,99,true,1).parseDiagnostics.length)throw Error('Candidate syntax invalid');
const out=html.slice(0,offset)+result+html.slice(offset+code.length);fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,out);
console.log(JSON.stringify({before:sha(raw),after:sha(out),functions:edits.map(e=>e.name)}));
