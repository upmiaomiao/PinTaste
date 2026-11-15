const fs=require('fs'),path=require('path'),crypto=require('crypto');
let ts;for(const name of [process.env.WUYU_TYPESCRIPT_MODULE,'typescript','/opt/dsh-harness/node_modules/typescript'].filter(Boolean)){try{ts=require(name);break}catch{}}if(!ts)throw Error('TypeScript parser required');
const[input,output]=process.argv.slice(2),html=fs.readFileSync(input,'utf8'),sha=x=>crypto.createHash('sha256').update(x).digest('hex');
if(sha(html)!=='e6d4f7d1929eefa6878fc63cd21848b1b5dbeae7d65cfad0256e786f7f52a66b')throw Error('Actual panel baseline changed');
const match=/<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html),code=match[1],offset=match.index+match[0].indexOf(code),ast=ts.createSourceFile('panel.js',code,99,true,1),edits=[];
function visit(n){if(ts.isFunctionDeclaration(n)){
 const old=n.getText(ast);let next=old;
 if(n.name?.text==='Y$'){
 const needle='${Ge((__wuyu_panel18_text("导出文件")),(__wuyu_panel18_text("在对话中提出导出需求")))}';if(next.split(needle).length!==2)throw Error('Delivery fragment changed');next=next.replace(needle,'${__wuyuDeliveryChoices(s)}');
 }
 const needle='(__wuyu_panel18_text("全部图像满足审核条件后，系统才会描摹和检查可编辑文件。"))';
 if(next.includes(needle))next=next.replace(needle,'__wuyuDeliveryText("traceLater")');
 if(next!==old)edits.push({start:n.getStart(ast),end:n.getEnd(),text:next,name:n.name.text});
}ts.forEachChild(n,visit)}visit(ast);if(edits.length!==2)throw Error('Expected delivery functions missing');
let result=code;for(const e of edits.sort((a,b)=>b.start-a.start))result=result.slice(0,e.start)+e.text+result.slice(e.end);
result+='\n'+fs.readFileSync(path.join(__dirname,'delivery-panel.js'),'utf8');const out=html.slice(0,offset)+result+html.slice(offset+code.length);fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,out);console.log(JSON.stringify({before:sha(html),after:sha(out),functions:edits.map(e=>e.name)}));
