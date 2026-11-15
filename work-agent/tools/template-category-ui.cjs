const fs=require('fs'),path=require('path'),crypto=require('crypto');
let ts;for(const name of [process.env.WUYU_TYPESCRIPT_MODULE,'typescript','/opt/dsh-harness/node_modules/typescript'].filter(Boolean)){try{ts=require(name);break}catch{}}if(!ts)throw Error('TypeScript parser required');
const[input,output]=process.argv.slice(2),html=fs.readFileSync(input,'utf8'),sha=x=>crypto.createHash('sha256').update(x).digest('hex');
if(sha(html)!=='3eeb8c52e46e9f4d375bbe6cca55fffc53e5ca76b768af749dbb2f94ef9f7258')throw Error('Actual panel baseline changed');
const match=/<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html),code=match[1],offset=match.index+match[0].indexOf(code),ast=ts.createSourceFile('panel.js',code,99,true,1),edits=[];
function replace(s,old,next){if(s.split(old).length!==2)throw Error('UI fragment changed: '+old);return s.replace(old,next)}
function visit(n){if(ts.isFunctionDeclaration(n)&&['U$','O$','__wuyuTemplateRequired','__wuyuTemplateConfirmationHint','__wuyuPrivateLibraryScope'].includes(n.name?.text)){
 let next=n.getText(ast);
 if(n.name.text==='U$'){
  next=replace(next,'k.tab==="all"?k.snapshot.items||[]:k.snapshot.recommended||[]','k.tab==="all"?(k.loading||k.error?[]:k.snapshot.items||[]):k.snapshot.recommended||[]');
  next=replace(next,'data-template-mode="${a}" aria-pressed="${k.tab===a}"','data-template-mode="${a}" aria-pressed="${k.tab===a}" ${k.loading?"disabled":""}');
  next=replace(next,'<form id="template-search-form"','${__wuyuTemplateCategories()}<form id="template-search-form"');
  next=replace(next,'value="${$(k.query)}"><button type="submit">','value="${$(k.query)}" ${k.loading?"disabled":""}><button type="submit" ${k.loading?"disabled":""}>');
  next=replace(next,'<div class="template-options">${i}</div>','<div class="template-options" aria-busy="${k.loading}">${i}</div>');
  const empty='(__wuyu_panel18_html(["<p class=\\"preview-note\\">暂无匹配的模板，可以浏览全部模板或上传自己的模板。</p>"]))';
  next=replace(next,empty,'(k.tab==="all"?`<p class="preview-note">${$(__wuyuTemplateCategoryText("empty"))}</p>`:'+empty+')');
  const more='${k.tab==="all"&&(k.snapshot.filters?.offset||0)+e.length<(k.snapshot.total||0)?(__wuyu_panel18_html(["<button type=\\"button\\" data-template-more>查看更多</button>"])):""}';
  const pager='${k.tab==="all"&&!k.loading&&!k.error&&(k.snapshot.filters?.offset||0)>0?`<button type="button" data-template-previous>${$(__wuyu_panel18_text("上一页"))}</button>`:""}${k.tab==="all"&&!k.loading&&!k.error&&(k.snapshot.filters?.offset||0)+e.length<(k.snapshot.total||0)?`<button type="button" data-template-more>${$(__wuyu_panel18_text("下一页"))}</button>`:""}';
  next=replace(next,more,pager);
 }
 if(n.name.text==='O$'){
  next=replace(next,'n.id==="template-library-scope"&&(k.scope=n.value,$e({operation:"list",scope:k.scope,search:k.query,offset:0}))','n.id==="template-library-scope"&&!k.loading&&(k.scope=n.value,$e(__wuyuTemplateListRequest()))');
  next=replace(next,'e.preventDefault(),$e({operation:"list",scope:k.scope||"all",search:k.query,offset:0})','e.preventDefault(),$e(__wuyuTemplateListRequest())');
  next=replace(next,'r.templateMode?','r.templateCategory!==void 0?(!k.loading&&(k.category=r.templateCategory,$e(__wuyuTemplateListRequest()))):r.templateMode?');
  next=replace(next,'k.tab==="all"?$e({operation:"list",scope:k.scope||"all",search:k.query,offset:0})','k.tab==="all"?$e(__wuyuTemplateListRequest())');
  next=replace(next,'$e({operation:k.tab==="upload"?"materials":k.tab==="all"?"list":"recommend",scope:k.scope||"all",search:k.query,offset:0})','$e(k.tab==="all"?__wuyuTemplateListRequest():{operation:k.tab==="upload"?"materials":"recommend"})');
  next=replace(next,'r.templateMore!==void 0?$e({operation:"list",scope:k.scope||"all",search:k.query,offset:(k.snapshot.filters?.offset||0)+50})','r.templatePrevious!==void 0?$e(__wuyuTemplateListRequest(Math.max(0,(k.snapshot.filters?.offset||0)-24))):r.templateMore!==void 0?$e(__wuyuTemplateListRequest((k.snapshot.filters?.offset||0)+(k.snapshot.items?.length||0)))');
 }
 if(n.name.text==='__wuyuTemplateRequired')next=replace(next,'__wuyuTemplatePrivate() ? 1 : 4','__wuyuTemplatePrivate() || Oe()?.reference_scope === \'single_image\' ? 1 : 4');
 if(n.name.text==='__wuyuTemplateConfirmationHint')next=replace(next,"return __wuyuTemplateText(__wuyuTemplatePrivate() ? 'privateReview' : 'libraryReview');","return Oe()?.reference_scope === 'single_image' ? __wuyuTemplateCategoryText('singleReview') : __wuyuTemplateText(__wuyuTemplatePrivate() ? 'privateReview' : 'libraryReview');");
 if(n.name.text==='__wuyuPrivateLibraryScope')next=replace(next,'<select id="template-library-scope"','<select ${k.loading ? \'disabled\' : \'\'} id="template-library-scope"');
 edits.push({start:n.getStart(ast),end:n.getEnd(),text:next,name:n.name.text});
 }ts.forEachChild(n,visit)}visit(ast);
if(edits.length!==5)throw Error('Expected template functions missing');
let result=code;for(const e of edits.sort((a,b)=>b.start-a.start))result=result.slice(0,e.start)+e.text+result.slice(e.end);
result+='\n'+fs.readFileSync(path.join(__dirname,'template-category-panel.js'),'utf8');
let out=html.slice(0,offset)+result+html.slice(offset+code.length);
const style='<style id="template-category-77">.template-category-title{margin-bottom:7px}.template-categories{flex-wrap:wrap;gap:8px;margin-bottom:16px}.template-categories button{flex:0 1 auto;white-space:normal;overflow-wrap:anywhere}.template-categories button:focus-visible{outline:2px solid currentColor;outline-offset:3px}.template-categories button:disabled{opacity:.55;cursor:default}</style>';
if(out.includes('template-category-77'))throw Error('Category styles already present');
out=out.replace('</head>',style+'</head>');
if(!out.includes(style))throw Error('Panel head not found');
fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,out);
console.log(JSON.stringify({before:sha(html),after:sha(out),functions:edits.map(e=>e.name),changed_function_count:edits.length}));
