// op204: in the conversation template panel, offer "use the PPT you uploaded as the template / use our templates",
// and let the owner drop or restore the automatically chosen representative pages of that template.
// Runs on the op200 panel output. usage: node own-template-ui.cjs <input index.html> <output index.html>
const fs = require('fs'), crypto = require('crypto');
const [input, output] = process.argv.slice(2);
const raw = fs.readFileSync(input), sha = x => crypto.createHash('sha256').update(x).digest('hex');
if (sha(raw) !== '9a29d6d4a8b78652b3a7102dec569bdebdf117c9d8bbb7fbe7b2cb5244a0672d') throw Error('Actual panel baseline changed');
let html = raw.toString('utf8');

const CODE = String.raw`function __op204Text(zh,en){return document.documentElement.lang==="en"?en:zh}
function __op204Material(){return k.materialId?(k.snapshot.materials||[]).find(a=>a.id===k.materialId)||null:null}
function __op204Card(){
 const sources=(k.snapshot&&k.snapshot.templateSources)||[];
 if(!sources.length)return "";
 const s=sources[sources.length-1],name=$(s.filename),off=k.loading?"disabled":"";
 if(s.templateMaterialId){
  if(k.tab==="upload"&&k.materialId===s.templateMaterialId)return "";
  const made=(k.snapshot.materials||[]).find(a=>a.id===s.templateMaterialId);
  const ready=made&&made.ready;
  return '<div class="op204-card" data-preview-key="op204-card"><p>'+__op204Text("已用你上传的《","Using your uploaded “")+name+__op204Text("》做模板。","” as the template.")+(ready?"":__op204Text("正在挑选代表页，请稍候…"," Picking representative pages…"))+'</p><div class="op204-actions"><button type="button" class="template-primary" data-op204-open="'+$(s.templateMaterialId)+'" '+(ready?off:"disabled")+'>'+__op204Text("查看页面并确认","Review pages and confirm")+'</button></div></div>';
 }
 if(k.op204Dismissed&&k.op204Dismissed[s.id])return "";
 return '<div class="op204-card" data-preview-key="op204-card"><p><strong>'+__op204Text("要用你上传的 PPT 做模板吗？","Use your own presentation as the template?")+'</strong><br>'+__op204Text("你上传了《","You uploaded “")+name+__op204Text("》。可以按它的风格来做：系统会自动挑出封面、结尾和几种版式的代表页，你可以去掉不想要的页。","”. We can follow its style: the cover, closing and one page of each layout are picked automatically, and you can drop any you do not want.")+'</p><div class="op204-actions"><button type="button" class="template-primary" data-op204-use="'+$(s.id)+'" '+off+'>'+__op204Text("用这份 PPT 做模板","Use this presentation")+'</button><button type="button" data-op204-dismiss="'+$(s.id)+'" '+off+'>'+__op204Text("用我们的模板","Use our templates")+'</button></div></div>';
}
function __op204PageTools(){
 const mat=__op204Material();
 if(!mat||!mat.ready)return "";
 const kept=mat.pages||[],removed=mat.removedPages||[];
 if(kept.length+removed.length<2)return "";
 const canRemove=kept.length>2,off=k.loading?"disabled":"";
 const thumb=(p,attr,label,mark)=>'<span class="op204-page"><img src="'+$(Y(p.image_url))+'" alt="" loading="lazy">'+(attr?'<button type="button" '+attr+'="'+p.number+'" '+off+' aria-label="'+$(label)+'" title="'+$(label)+'">'+mark+'</button>':'')+'<small>'+__op204Text("第"+p.number+"页","p. "+p.number)+'</small></span>';
 const head=(mat.sourceMaterialId?__op204Text("从你的 PPT 里自动挑出的代表页","Pages picked from your presentation"):__op204Text("模板参考页","Template reference pages"))+"（"+kept.length+__op204Text(" 页）。"," pages). ")+(canRemove?__op204Text("点 × 去掉不想要的页，至少保留 2 页；生成时只参考保留的页。","Click × to drop a page; at least 2 stay, and only kept pages are used."):__op204Text("至少保留 2 页。","At least 2 pages stay."));
 return '<div class="op204-pages" data-preview-key="op204-pages"><p class="preview-note">'+head+'</p><div class="op204-row">'+kept.map(p=>canRemove?thumb(p,"data-op204-remove",__op204Text("去掉第"+p.number+"页","Remove page "+p.number),"×"):thumb(p,"","","")).join("")+'</div>'+(removed.length?'<p class="preview-note">'+__op204Text("已去掉（点 ↺ 恢复）：","Removed (click ↺ to restore):")+'</p><div class="op204-row op204-removed">'+removed.map(p=>thumb(p,"data-op204-restore",__op204Text("恢复第"+p.number+"页","Restore page "+p.number),"↺")).join("")+'</div>':'')+'</div>';
}
function __op204Bind(){
 if(window.__op204Bound)return;window.__op204Bound=true;
 const style=document.createElement("style");
 style.textContent=".op204-card{border:1px solid #c9dcff;background:#f3f7ff;border-radius:12px;padding:12px 14px;margin:10px 0;font-size:13px;line-height:1.6}.op204-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}.op204-pages{margin:10px 0 4px}.op204-row{display:flex;flex-wrap:wrap;gap:10px;margin:6px 0}.op204-page{position:relative;display:inline-flex;flex-direction:column;align-items:center;gap:2px;width:104px}.op204-page img{width:104px;aspect-ratio:16/9;object-fit:cover;border:1px solid #dbe3ef;border-radius:6px;background:#fff}.op204-page button{position:absolute;top:3px;right:3px;width:22px;height:22px;padding:0;border:0;border-radius:50%;background:rgba(15,23,42,.72);color:#fff;font-size:14px;line-height:22px;cursor:pointer}.op204-page button:disabled{opacity:.4;cursor:default}.op204-removed img{opacity:.45}.op204-page small{font-size:11px;color:#64748b}";
 document.head.appendChild(style);
 document.addEventListener("click",e=>{
  const b=e.target.closest("button");if(!b||b.disabled)return;const d=b.dataset;
  if(d.op204Use){k.tab="upload";k.materialId="";k.large=-1;ze();$e({operation:"use_material_as_template",material_id:d.op204Use})}
  else if(d.op204Dismiss){k.op204Dismissed=Object.assign({},k.op204Dismissed,{[d.op204Dismiss]:true});ze()}
  else if(d.op204Open){k.tab="upload";k.materialId=d.op204Open;k.large=-1;k.loaded.clear();ze();$e({operation:"materials"})}
  else if(d.op204Remove&&k.materialId){$e({operation:"template_pages",material_id:k.materialId,remove:[Number(d.op204Remove)]})}
  else if(d.op204Restore&&k.materialId){$e({operation:"template_pages",material_id:k.materialId,restore:[Number(d.op204Restore)]})}
 });
}
`;

const edits = [
  ['</nav>\n ${__wuyuTemplateWatchNotice()}', '</nav>\n ${__op204Card()}${__wuyuTemplateWatchNotice()}'],
  ['<div class="template-confirm"><span>${$(__wuyuTemplateConfirmationHint())}</span>',
   '${__op204PageTools()}<div class="template-confirm"><span>${$(__wuyuTemplateConfirmationHint())}</span>'],
  ['function O$(){', CODE + 'function O$(){__op204Bind();'],
];
for (const [from, to] of edits) {
  if (html.split(from).length !== 2) throw Error('Anchor not unique: ' + from.slice(0, 60));
  html = html.replace(from, () => to);
}
if (CODE.includes('</script')) throw Error('Unsafe panel text');
fs.writeFileSync(output, html);
console.log('own template', sha(fs.readFileSync(output)));
