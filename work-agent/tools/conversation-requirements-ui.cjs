const fs=require('fs'),path=require('path'),crypto=require('crypto');
let ts;for(const name of [process.env.WUYU_TYPESCRIPT_MODULE,'typescript','/opt/dsh-harness/node_modules/typescript'].filter(Boolean)){try{ts=require(name);break}catch{}}if(!ts)throw Error('TypeScript parser required');
const[input,output]=process.argv.slice(2),html=fs.readFileSync(input,'utf8'),sha=x=>crypto.createHash('sha256').update(x).digest('hex');
if(sha(html)!=='4a414ea0d554a9b18828337f556ffe3da3fbfc9bc73de98b7b2b01f83b1a3dde')throw Error('Actual panel baseline changed');
const match=/<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html),code=match[1],offset=match.index+match[0].indexOf(code),ast=ts.createSourceFile('panel.js',code,99,true,1),edits=[];
function replace(s,old,next){if(s.split(old).length!==2)throw Error('UI fragment changed: '+old);return s.replace(old,next)}
function visit(n){if(ts.isFunctionDeclaration(n)&&['V$','ke','J$'].includes(n.name?.text)){
 let next=n.getText(ast);
 if(n.name.text==='V$')next='function V$(){return __wuyuRequirementsView()}';
 if(n.name.text==='ke')next=replace(next,'${pt().filter(nr).length}/${pt().length||s.page_count||0} 页','${$(__wuyuRequirementsProgress())}');
 if(n.name.text==='J$')next=replace(next,'(__wuyu_panel18_text("处理未完成，请在对话中处理"))','(__wuyuRequirementsCopy("stepFailed"))');
 edits.push({start:n.getStart(ast),end:n.getEnd(),text:next,name:n.name.text});
 }ts.forEachChild(n,visit)}visit(ast);if(edits.length!==3)throw Error('Expected requirements functions missing');
let result=code;for(const edit of edits.sort((a,b)=>b.start-a.start))result=result.slice(0,edit.start)+edit.text+result.slice(edit.end);
result+='\n'+fs.readFileSync(path.join(__dirname,'conversation-requirements-panel.js'),'utf8');
let out=html.slice(0,offset)+result+html.slice(offset+code.length);
const style='<style id="conversation-requirements-79">.current-requirements h2{font-size:17px;margin:0 0 8px}.current-requirements .brief-details dd{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:4px 12px;white-space:pre-wrap;overflow-wrap:anywhere}.requirement-source{font-size:11px;opacity:.7;font-weight:400}</style>';
if(out.includes('conversation-requirements-79'))throw Error('Requirements styles already present');out=out.replace('</head>',style+'</head>');if(!out.includes(style))throw Error('Panel head missing');
fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,out);console.log(JSON.stringify({before:sha(html),after:sha(out),functions:edits.map(edit=>edit.name)}));
