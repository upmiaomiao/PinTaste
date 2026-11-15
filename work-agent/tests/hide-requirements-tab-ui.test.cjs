// Behavioural check of the two patched snippets, evaluated in isolation with the real source text.
const fs=require('node:fs'),assert=require('node:assert'),vm=require('node:vm');
const html=fs.readFileSync(process.argv[2],'utf8');
const start=html.indexOf('<script type="module" crossorigin>'),script=html.slice(start,html.indexOf('</script>',start));
const i=script.indexOf('function H$(){'),fn=script.slice(i,script.indexOf('function V$',i));
const ctx={Rs:{get brief(){return'需求'},get materials(){return'材料'},get templates(){return'模板'},get outline(){return'大纲'},get slides(){return'页面'},history:'',get trace(){return'可编辑预览'}},
  __wuyuHistoryText:()=> '历史',m:{snapshot:{}},__wuyu_panel18_text:s=>s};
vm.createContext(ctx);vm.runInContext(fn+';this.H=H$;',ctx);
const tabs=Object.keys(ctx.H());
assert.ok(!tabs.includes('brief'),'brief tab must be gone: '+tabs);
assert.deepStrictEqual(tabs,['materials','templates','outline','slides','history','trace'],tabs.join());
ctx.m.snapshot={reading:{protocol_version:1}};assert.ok(ctx.H().outline.length>0&&!('brief' in ctx.H()));
assert.ok(script.includes('const n=Object.keys(Rs).filter(k=>k!=="brief"),r=n.indexOf(m.previewTab)'),'keyboard nav filter missing');
assert.ok(script.includes('Object.hasOwn(r,m.previewTab)||(m.previewTab=m.previewTab==="references"?"templates":"outline")'),'fallback to outline missing');
for(const k of['__wuyuLockToolbar','__wuyuLockChip','__wuyuLockButton'])assert.ok(script.includes(k),k);
console.log('ok: tabs =',tabs.join(','));
