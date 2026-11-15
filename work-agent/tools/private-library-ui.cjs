const fs=require('fs'),path=require('path'),crypto=require('crypto');
let ts;for(const name of [process.env.WUYU_TYPESCRIPT_MODULE,'typescript','/opt/dsh-harness/node_modules/typescript'].filter(Boolean)){try{ts=require(name);break}catch{}}if(!ts)throw Error('TypeScript parser required');
const[input,output]=process.argv.slice(2),html=fs.readFileSync(input,'utf8'),sha=x=>crypto.createHash('sha256').update(x).digest('hex');
if(sha(html)!=='504a58c3403582300840865ceba8c2a2e694c6ce9eab45a211b0457230087b61')throw Error('Actual panel baseline changed');
const match=/<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html),code=match[1],offset=match.index+match[0].indexOf(code),ast=ts.createSourceFile('panel.js',code,99,true,1),edits=[];
function replace(s,old,next){if(s.split(old).length!==2)throw Error('UI fragment changed: '+old);return s.replace(old,next)}
function visit(n){if(ts.isFunctionDeclaration(n)&&['U$','O$','__wuyuMaterialText'].includes(n.name?.text)){
let next=n.getText(ast);
if(n.name.text==='U$'){
next=replace(next,'${$(a.recommendation_reason||a.tags?.purpose?.join(" · ")||(__wuyu_panel18_text("设计与排版参考")))}','${$(__wuyuPrivateLibraryLabel(a))}');
next=replace(next,'<input id="template-search-query"','${__wuyuPrivateLibraryScope()}<input id="template-search-query"');
next=replace(next,'$(n.preview_disclaimer||(__wuyu_panel18_text("仅参考设计和排版，内容不作为本项目事实。")))','$(n.preview_kind==="private_uploaded_template"?__wuyuPrivateLibraryCopy("reuse"):n.preview_disclaimer||(__wuyu_panel18_text("仅参考设计和排版，内容不作为本项目事实。")))');
}
if(n.name.text==='O$'){
next=replace(next,'n.id==="template-private-file"', 'n.id==="template-library-scope"&&(k.scope=n.value,$e({operation:"list",scope:k.scope,search:k.query,offset:0})),n.id==="template-private-file"');
next=next.replaceAll('operation:"list",search:k.query','operation:"list",scope:k.scope||"all",search:k.query');
next=replace(next,'search:k.query,offset:0}):r.templateMore','scope:k.scope||"all",search:k.query,offset:0}):r.templateMore');
}
if(n.name.text==='__wuyuMaterialText'){
next=replace(next,'此操作无法恢复。','此操作无法恢复；其他项目中由您明确复用创建的独立副本会保留。');
next=replace(next,'This cannot be undone.','This cannot be undone. Independent copies you explicitly reused in other projects remain.');
}
edits.push({start:n.getStart(ast),end:n.getEnd(),text:next,name:n.name.text});}
ts.forEachChild(n,visit)}visit(ast);if(edits.length!==3)throw Error('Expected functions missing');
let result=code;for(const e of edits.sort((a,b)=>b.start-a.start))result=result.slice(0,e.start)+e.text+result.slice(e.end);
result+='\n'+fs.readFileSync(path.join(__dirname,'private-library-panel.js'),'utf8');
const out=html.slice(0,offset)+result+html.slice(offset+code.length);fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,out);console.log(JSON.stringify({before:sha(html),after:sha(out),functions:edits.map(e=>e.name)}));
