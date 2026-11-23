// op198: replace the conversation panel's template-category block with the three-group version
// (PPT 模板 / 图片模板 / 我的上传) from template-category-panel.js. Runs on the op192 panel output;
// the old op77 append step is not re-run. Page size stays 7 (op152 pagination).
// usage: node template-groups-ui.cjs <input index.html> <output index.html>
const fs = require('fs'), path = require('path'), crypto = require('crypto');
const [input, output] = process.argv.slice(2);
const raw = fs.readFileSync(input), sha = x => crypto.createHash('sha256').update(x).digest('hex');
if (sha(raw) !== '5e12045d1ac31dde873fab3eaeb178f2df48d11acea2ee32871446e7fd53b8eb') throw Error('Actual panel baseline changed');
const html = raw.toString('utf8');
const head = 'function __wuyuTemplateCategoryText(', tail = 'function __wuyuTemplateListRequest(';
if (html.split(head).length !== 2 || html.split(tail).length !== 2) throw Error('Template category block not unique');
const start = html.indexOf(head), end = html.indexOf('\n}', html.indexOf(tail)) + 2;
const old = html.slice(start, end);
if (!old.includes('limit: 7 }') || old.length > 3000) throw Error('Unexpected template category block');
let next = fs.readFileSync(path.join(__dirname, 'template-category-panel.js'), 'utf8').trimEnd();
if (next.split('limit: 24 }').length !== 3) throw Error('template-category-panel.js changed');
next = next.split('limit: 24 }').join('limit: 7 }');
if (next.includes('</script')) throw Error('Unsafe panel text');
fs.writeFileSync(output, html.slice(0, start) + next + html.slice(end));
console.log('template groups', sha(fs.readFileSync(output)));
