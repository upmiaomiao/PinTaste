// Adds page lock controls and the online-editor entry to the deployed panel.
// Hash-locked to the panel produced by ppt-latest-flow-163 (LF, trailing whitespace trimmed).
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
let ts;for(const name of [process.env.WUYU_TYPESCRIPT_MODULE,'typescript','/opt/dsh-harness/node_modules/typescript'].filter(Boolean)){try{ts=require(name);break}catch{}}if(!ts)throw Error('TypeScript parser required');
const [input,output]=process.argv.slice(2),raw=fs.readFileSync(input,'utf8'),html=raw.replace(/\r\n/g,'\n'),hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const expected=process.env.WUYU_PANEL_BASELINE_SHA256||'662a74b89c5501625f4e8621be589fceccf11af86277d549eab8878c5b00da10';
if(hash(html.trimEnd())!==expected)throw Error('Actual panel baseline changed: '+hash(html.trimEnd()));
const match=/<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html),code=match[1],offset=match.index+match[0].indexOf(code),ast=ts.createSourceFile('panel.js',code,99,true,1),edits=[];
const replacements=[
 ['<div class="page-strip"','${__wuyuLockToolbar()}<div class="page-strip"'],
 ['<span>${o.number}. ${$(o.title)}</span>','<span>${o.number}. ${$(o.title)}${__wuyuLockChip(o)}</span>'],
 ['<div class="content-title"><h2>${n.number}. ${$(n.title)}</h2>','<div class="content-title"><h2>${n.number}. ${$(n.title)}</h2>${__wuyuLockButton(n)}'],
];
function visit(node){
 if(ts.isFunctionDeclaration(node)&&node.name?.text==='B$'){
  let next=node.getText(ast);
  for(const [needle,value] of replacements){if(next.split(needle).length!==2)throw Error('Slides renderer changed: '+needle);next=next.replace(needle,value);}
  edits.push({start:node.getStart(ast),end:node.end,text:next});return;
 }
 ts.forEachChild(node,visit);
}visit(ast);if(edits.length!==1)throw Error('Expected one slides renderer');
let result=code;for(const edit of edits)result=result.slice(0,edit.start)+edit.text+result.slice(edit.end);
result+='\n'+fs.readFileSync(path.join(__dirname,'page-lock-panel.js'),'utf8');
let out=html.slice(0,offset)+result+html.slice(offset+code.length);
out=out.replace('</head>','<style data-wuyu-page-lock>\n'+fs.readFileSync(path.join(__dirname,'page-lock.css'),'utf8')+'\n</style></head>');
fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,out);console.log(JSON.stringify({before:hash(html.trimEnd()),after:hash(out),modified:['B$']}));
