const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
let ts;for(const name of [process.env.WUYU_TYPESCRIPT_MODULE,'typescript','/opt/dsh-harness/node_modules/typescript'].filter(Boolean)){try{ts=require(name);break}catch{}}if(!ts)throw Error('TypeScript parser required');
const [input,output]=process.argv.slice(2),html=fs.readFileSync(input,'utf8'),hash=x=>crypto.createHash('sha256').update(x).digest('hex');
if(hash(html)!=='fedb22537974199a2065563194ce58d18e5d1d30aefc84bb65e25427b6c5880e')throw Error('Actual panel baseline changed');
const match=/<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html),code=match[1],offset=match.index+match[0].indexOf(code),ast=ts.createSourceFile('panel.js',code,99,true,1),edits=[];
function visit(node){
 if(ts.isVariableDeclaration(node)&&node.name.getText(ast)==='Rs'&&node.initializer&&ts.isObjectLiteralExpression(node.initializer)){
  const property=node.initializer.properties.find(p=>p.name?.getText(ast)==='slides');if(!property)throw Error('Slides tab missing');edits.push({start:property.end,end:property.end,text:',history:""',name:'Rs'});
 }
 if(ts.isFunctionDeclaration(node)&&['H$','ke'].includes(node.name?.text)){
  const old=node.getText(ast);let next=old;
  if(node.name.text==='H$'){const needle='{...Rs}';if(next.split(needle).length!==2)throw Error('Tab labels changed');next=next.replace(needle,'{...Rs,history:__wuyuHistoryText("title")}');}
  else{const needle='c==="trace"?X$():Y$()';if(next.split(needle).length!==2)throw Error('Preview renderer changed');next=next.replace(needle,'c==="trace"?X$():c==="history"?__wuyuImageHistory(m.snapshot):Y$()');}
  edits.push({start:node.getStart(ast),end:node.end,text:next,name:node.name.text});return;
 }
 ts.forEachChild(node,visit);
}visit(ast);if(edits.length!==3)throw Error('Expected three panel edits');
let result=code;for(const edit of edits.sort((a,b)=>b.start-a.start))result=result.slice(0,edit.start)+edit.text+result.slice(edit.end);
result+='\n'+fs.readFileSync(path.join(__dirname,'image-history-panel.js'),'utf8');let out=html.slice(0,offset)+result+html.slice(offset+code.length);out=out.replace('</head>','<style data-wuyu-image-history>\n'+fs.readFileSync(path.join(__dirname,'image-history.css'),'utf8')+'\n</style></head>');fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,out);console.log(JSON.stringify({before:hash(html),after:hash(out),modified:edits.map(e=>e.name)}));
