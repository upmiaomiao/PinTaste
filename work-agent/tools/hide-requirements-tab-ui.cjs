// Removes the "需求" (brief) tab from the PPT detail panel. Requirements are described in the
// conversation; the tab only duplicated them. Hash-locked to the panel produced by page-lock-ui.cjs.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const [input,output]=process.argv.slice(2),html=fs.readFileSync(input,'utf8'),hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const expected=process.env.WUYU_PANEL_BASELINE_SHA256||'c728c2f62623be5ee779bf2d8be5869ea946c28455b073c7a56db825f3768b92';
if(hash(html)!==expected)throw Error('Actual panel baseline changed: '+hash(html));
function replace(s,old,next){if(s.split(old).length!==2)throw Error('UI fragment changed: '+old);return s.replace(old,next)}
let out=html;
// 1. The tab list: H$() builds it from Rs; drop brief. render() already falls back to the outline tab
//    when previewTab is not in this list, so a stale or tool-requested "brief" view lands on the outline.
out=replace(out,'function H$(){const e={...Rs,history:__wuyuHistoryText("title")};return','function H$(){const e={...Rs,history:__wuyuHistoryText("title")};delete e.brief;return');
// 2. Arrow/Home/End keyboard navigation must not step onto the removed tab.
out=replace(out,'const n=Object.keys(Rs),r=n.indexOf(m.previewTab)','const n=Object.keys(Rs).filter(k=>k!=="brief"),r=n.indexOf(m.previewTab)');
fs.mkdirSync(path.dirname(output),{recursive:true});fs.writeFileSync(output,out);
console.log(JSON.stringify({before:hash(html),after:hash(out),removed:'brief tab'}));
