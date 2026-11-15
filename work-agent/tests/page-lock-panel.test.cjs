// Runs the injected panel functions against stubbed panel globals (no network).
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert');
const listeners={};const calls=[];const messages=[];const links=[];
const ctx={document:{documentElement:{lang:'zh-CN'},addEventListener:(t,f)=>listeners[t]=f},
 m:{handle:'h'.repeat(24),snapshot:{workflow:{phase:'exported',pages:[{number:1,generation_status:'ready',locked:true},{number:2,generation_status:'ready'},{number:3,generation_status:'running'}]},export_capabilities:{editable_pptx:true}}},
 $:s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])),ke:()=>{},
 gp:r=>{ctx.m.snapshot=r},
 Ti:async(name,args)=>{calls.push([name,args]);return name==='open_presentation_editor'?{editor_url:'https://x/agent-api/office/editor/p'}:{workflow:{phase:'exported',pages:[{number:1,generation_status:'ready'}]}}},
 Rt:{sendMessage:async m=>{messages.push(m)},openLink:async l=>{links.push(l.url)}}};
ctx.pt=()=>ctx.m.snapshot.workflow.pages;
vm.createContext(ctx);vm.runInContext(fs.readFileSync(path.join(__dirname,'../tools/page-lock-panel.js'),'utf8'),ctx);
const run=(code)=>vm.runInContext(code,ctx);
(async()=>{
 const bar=run('__wuyuLockToolbar()');
 assert.match(bar,/已锁定 1 页/);assert.match(bar,/data-lock-regenerate/);assert.doesNotMatch(bar,/data-office-edit disabled/);
 assert.match(run('__wuyuLockButton(pt()[0])'),/已锁定 · 点击解锁/);
 assert.match(run('__wuyuLockButton(pt()[1])'),/锁定此页/);
 assert.equal(run('__wuyuLockButton(pt()[2])'),'');
 assert.match(run('__wuyuLockChip(pt()[0])'),/wuyu-lock-chip/);
 const btn=(sel,data)=>({target:{closest:s=>s===sel?{disabled:false,dataset:data||{}}:null}});
 await listeners.click(btn('button[data-page-lock]',{pageLock:'2'}));
 assert.deepEqual(calls[0],['set_presentation_page_lock',{project_handle:ctx.m.handle,numbers:[2],locked:true,source:'user_ui'}]);
 ctx.m.snapshot={workflow:{phase:'exported',pages:[{number:1,generation_status:'ready',locked:true},{number:2,generation_status:'ready'}]},export_capabilities:{editable_pptx:true}};
 await listeners.click(btn('button[data-lock-regenerate]'));
 assert.match(messages[0].content[0].text,/锁定页：1/);
 await listeners.click(btn('button[data-office-edit]'));
 assert.equal(links[0],'https://x/agent-api/office/editor/p');
 ctx.m.snapshot.workflow.phase='final_image_review';
 assert.match(run('__wuyuLockToolbar()'),/data-office-edit disabled/);
 console.log('page-lock panel: 11 assertions passed');
})().catch(e=>{console.error(e);process.exit(1)});
