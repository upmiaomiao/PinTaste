const fs=require('fs'),path=require('path'),crypto=require('crypto');
let ts;for(const name of [process.env.WUYU_TYPESCRIPT_MODULE,'typescript','/opt/dsh-harness/node_modules/typescript'].filter(Boolean)){try{ts=require(name);break}catch{}}if(!ts)throw Error('TypeScript parser required');
const [input,output]=process.argv.slice(2);if(!input||!output)throw Error('Usage: node material-management-ui.cjs baseline.html output.html');
const html=fs.readFileSync(input,'utf8'),sha=s=>crypto.createHash('sha256').update(s).digest('hex');if(sha(html)!=='4e0f74dd4258cbabee11f7bede1ccf151f56e00410018c2ad052f13a1b00ff07')throw Error('Actual panel baseline changed');
const match=/<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html),code=match[1],offset=match.index+match[0].indexOf(code),ast=ts.createSourceFile('panel.js',code,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS),nodes={};
function visit(n){if(ts.isFunctionDeclaration(n)&&['x$','K$','ke'].includes(n.name?.text))nodes[n.name.text]=n;ts.forEachChild(n,visit)}visit(ast);if(Object.keys(nodes).length!==3)throw Error('Material renderers missing');
const edits=[];for(const [name,node]of Object.entries(nodes)){
 const old=node.getText(ast);let next;
 if(name==='x$'){
  const needle='${u?(__wuyu_panel18_html`';if(old.split(needle).length!==2)throw Error('Material footer changed');
  next=old.replace(needle,'${__wuyuMaterialDeleteButton(e)}${u?(__wuyu_panel18_html`');
 }else if(name==='K$'){
  const needle='${k$()}';if(old.split(needle).length!==2)throw Error('Material section changed');next=old.replace(needle,'${__wuyuMaterialSummary()}${k$()}');
 }else{
  const needle='["reading_synthesis_invalid","reading_synthesis_source_mismatch","reading_synthesis_incomplete_statement"].includes(String(d.error_code))?';if(old.split(needle).length!==2)throw Error('Failure guidance changed');
  next=old.replace(needle,'["reading_material_capacity","reading_prepare_timeout","material_project_capacity"].includes(String(d.error_code))?(document.documentElement.lang==="en"?"Reduce the selected material scope or delete unneeded uploads, then reorganize the draft. No model reading was started for this failed preparation.":"请减少选用材料或删除不需要的资料，再重新整理草稿；本次准备失败尚未开始模型阅读。"):'+needle);
 }
 edits.push({start:node.getStart(ast),end:node.getEnd(),text:next,name});
}
const helper=fs.readFileSync(path.join(__dirname,'material-management-panel.js'),'utf8');
edits.push({start:code.length,end:code.length,text:helper,name:'material management controls'});
let updated=code;for(const edit of edits.sort((a,b)=>b.start-a.start))updated=updated.slice(0,edit.start)+edit.text+updated.slice(edit.end);
if(ts.createSourceFile('candidate.js',updated,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS).parseDiagnostics.length)throw Error('Candidate syntax failed');
const result=html.slice(0,offset)+updated+html.slice(offset+code.length);fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,result);console.log(JSON.stringify({before_sha256:sha(html),after_sha256:sha(result),changed_renderers:['x$','K$','ke'],all_other_code_preserved:true,sdk_prefix_unchanged:true,user_interface_channel_only:true}));
