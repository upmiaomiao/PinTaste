// Adds the read-only before/after page comparison to the slides tab of the deployed panel.
// Last step of the panel chain. Hash-locked to the dist/ui/index.html this checkout carried before the step
// (image history and page lock applied; the requirements tab still present).
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const BASELINE_SHA=process.env.WUYU_PANEL_BASELINE_SHA256||'b8d3963d0bee15382eadb8b482b9c18746738c3f25d4afda0d69fbb2b71033ad';
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const panel='\n'+fs.readFileSync(path.join(__dirname,'page-compare-panel.js'),'utf8');
const style='<style data-wuyu-page-compare>\n'+fs.readFileSync(path.join(__dirname,'page-compare.css'),'utf8')+'\n</style>';
// Both fragments live in the slides renderer B$: the focused page title row and its single picture.
const replacements=[
 ['<h2>${n.number}. ${$(n.title)}</h2>${__wuyuLockButton(n)}','<h2>${n.number}. ${$(n.title)}</h2>${__wuyuLockButton(n)}${__wuyuCompareButton(n)}'],
 ['${Li(n.image_url,n.title,void 0,`page-${n.number}-${n.image_version}`)}<pre>','${__wuyuCompareView(n)||Li(n.image_url,n.title,void 0,`page-${n.number}-${n.image_version}`)}<pre>'],
];
function swap(text,old,next,label){if(text.split(old).length!==2)throw Error(label+': '+old);return text.split(old).join(next)}
function moduleCode(html){
 const match=/<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html);if(!match)throw Error('Runtime module missing');
 return {code:match[1],offset:match.index+match[0].indexOf(match[1])};
}
function apply(html){
 let {code,offset}=moduleCode(html);const end=offset+code.length;
 for(const [old,next] of replacements)code=swap(code,old,next,'Slides renderer changed');
 return swap(html.slice(0,offset)+code+panel+html.slice(end),'</head>',style+'</head>','Document head changed');
}
// Recovers the pinned input from this step's own output; any other document is refused by the hash check.
function baselineOf(html){
 if(hash(html)===BASELINE_SHA)return html;
 if(html.split(style+'</head>').length!==2)throw Error('Actual panel baseline changed: '+hash(html));
 const plain=html.split(style+'</head>').join('</head>');let {code,offset}=moduleCode(plain);const end=offset+code.length;
 if(!code.endsWith(panel))throw Error('Actual panel baseline changed: '+hash(html));
 code=code.slice(0,-panel.length);
 for(const [old,next] of replacements)code=swap(code,next,old,'Actual panel baseline changed');
 const baseline=plain.slice(0,offset)+code+plain.slice(end);
 if(hash(baseline)!==BASELINE_SHA)throw Error('Actual panel baseline changed: '+hash(html));
 return baseline;
}
function transform(html){
 const baseline=baselineOf(html),result=apply(baseline),idempotent=baseline!==html;
 if(idempotent&&result!==html)throw Error('Actual panel baseline changed: '+hash(html));
 if(baselineOf(result)!==baseline)throw Error('Unexpected panel change');
 return {html:result,metadata:{before:hash(html),baseline:BASELINE_SHA,after:hash(result),modified:['B$'],idempotent}};
}
module.exports={transform,baselineOf,BASELINE_SHA};
if(require.main===module){
 const [input,output]=process.argv.slice(2);if(!input||!output){console.error('Usage: node page-compare-ui.cjs INPUT_HTML OUTPUT_HTML');process.exit(2);}
 const result=transform(fs.readFileSync(input,'utf8'));fs.mkdirSync(path.dirname(path.resolve(output)),{recursive:true});fs.writeFileSync(output,result.html);console.log(JSON.stringify(result.metadata));
}
