// op200: in the conversation template panel, a person who has never confirmed a template does not see
// the 推荐 tab; the first recommend answer (first_template from the work service, kept by the host
// template library as snapshot.firstTemplate) switches the panel to 全部模板 (PPT 模板 group) instead.
// Runs on the op198 panel output. usage: node template-first-ui.cjs <input index.html> <output index.html>
const fs = require('fs'), crypto = require('crypto');
const [input, output] = process.argv.slice(2);
const raw = fs.readFileSync(input), sha = x => crypto.createHash('sha256').update(x).digest('hex');
if (sha(raw) !== 'b1463794324f5c9587ab31fd1668b090a21615fd4f3312e99bbb8341384d71cd') throw Error('Actual panel baseline changed');
let html = raw.toString('utf8');
const edits = [
  ['[["recommended",(__wuyu_panel18_text("推荐"))],["all",(__wuyu_panel18_text("全部模板"))],["upload",(__wuyu_panel18_text("上传自己的模板"))]].map(',
   '[["recommended",(__wuyu_panel18_text("推荐"))],["all",(__wuyu_panel18_text("全部模板"))],["upload",(__wuyu_panel18_text("上传自己的模板"))]].filter(([a])=>a!=="recommended"||k.snapshot.firstTemplate!==true).map('],
  ['e.operation==="recommend"&&(__wuyuNext=__wuyuAutoConfirmedDetail(o.data))',
   'e.operation==="recommend"&&(__wuyuNext=o.data.firstTemplate===true&&k.tab==="recommended"?(k.tab="all",k.materialId="",k.large=-1,__wuyuTemplateListRequest()):__wuyuAutoConfirmedDetail(o.data))'],
];
for (const [from, to] of edits) {
  if (html.split(from).length !== 2) throw Error('Anchor not unique: ' + from.slice(0, 60));
  html = html.replace(from, () => to);
}
fs.writeFileSync(output, html);
console.log('template first', sha(fs.readFileSync(output)));
