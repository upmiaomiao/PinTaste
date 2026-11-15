/** Exact current-runtime patch. Never rebuild drifted historical panel authoring. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
const root=path.resolve(process.argv[2]||'.');
const sha=s=>crypto.createHash('sha256').update(s).digest('hex');
const expected=/*BASELINE_HASHES*/{"harness/packages/client/ui-banana-panel/src/client/template-action-error.ts":"b461aaf872c41465a5800699f32cb46f9109a2c368bd93915514b6aad10d4149","harness/packages/client/ui-banana-panel/src/client/locales.ts":"bd9e0bc289ec78ee0ef5b24090e76d55354ba70efe84e46a751eced41fef74f6","work-agent/tools/template-browser-panel.js":"93e9cfeec2a96d1499e526b13e4626527bcb3022a5ac60d6adc7bcd1aca014ae","work-agent/dist/ui/index.html":"cea68c70154fff4e69264b57682be34121b2f9fd298fa94fe391622a2599040b","work-agent/dist/workflow-tools.js":"c63b2af61dc4baaa700a0919aaa3cc52c641c4f9cfe064ebed766fef1debeac8","work-agent/dist/workflow-action-contract.js":"838b6ace7996ed5d8769c15e6ba5af04f499f66405f6fa2d47acfbcd814ed6a7"};
if(!expected)throw Error('BASELINE_HASHES_REQUIRED');
const copy={
 'templateAction.readingInProgress':['资料阅读正在进行，请待本轮结束后再确认或更换模板；当前阅读会继续。','Material reading is in progress. Wait for this run to finish before confirming or changing the template. The current reading will continue.'],
 'templateAction.parsePending':['模板仍在本地解析，请等待解析完成后再开始资料阅读；尚未发起新的阅读请求。','The template is still being parsed locally. Wait for parsing to finish before starting material reading. No new reading request has started.'],
 'templateAction.parseIncomplete':['模板解析尚未完成，当前没有处理它的解析任务。请刷新模板状态核对；若仍未恢复，请检查并恢复当前模板解析。原文件保留，本次未启动阅读。','Template parsing is incomplete and no parser task is processing it. Refresh the template status; if it remains incomplete, inspect and recover the current parse. The original is retained and no reading request has started.']
};
function change(s,a,b){if(s.split(a).length!==2)throw Error('UNIQUE_PATCH_ANCHOR:'+a.slice(0,70));return s.replace(a,b)}
const outputs={};
function read(name){const raw=fs.readFileSync(path.join(root,name));if(sha(raw)!==expected[name])throw Error('BASELINE_SHA_DRIFT:'+name);return raw.toString('utf8')}
const base='harness/packages/client/ui-banana-panel/src/client/';
let s=read(base+'template-action-error.ts');
s=change(s,"  TEMPLATE_NOT_READY:[409],", "  TEMPLATE_READING_IN_PROGRESS:[409],TEMPLATE_PARSE_PENDING:[409],TEMPLATE_PARSE_INCOMPLETE:[409],\n  TEMPLATE_NOT_READY:[409],");
s=change(s,"const operationAllowed=operation==='upload'?", "const operationAllowed=['TEMPLATE_READING_IN_PROGRESS','TEMPLATE_PARSE_PENDING','TEMPLATE_PARSE_INCOMPLETE'].includes(known)||operation==='upload'?");
// Parenthesize the existing ternary so shared codes do not become upload-only.
s=change(s,"const operationAllowed=['TEMPLATE_READING_IN_PROGRESS','TEMPLATE_PARSE_PENDING','TEMPLATE_PARSE_INCOMPLETE'].includes(known)||operation==='upload'?", "const operationAllowed=['TEMPLATE_READING_IN_PROGRESS','TEMPLATE_PARSE_PENDING','TEMPLATE_PARSE_INCOMPLETE'].includes(known)||(operation==='upload'?");
s=change(s,"'TEMPLATE_UPLOAD_INVALID_FILE'].includes(known)\n", "'TEMPLATE_UPLOAD_INVALID_FILE'].includes(known))\n");
s=change(s,"const messageKeys={", "const messageKeys={\n  TEMPLATE_READING_IN_PROGRESS:'templateAction.readingInProgress',TEMPLATE_PARSE_PENDING:'templateAction.parsePending',TEMPLATE_PARSE_INCOMPLETE:'templateAction.parseIncomplete',");
outputs[base+'template-action-error.ts']=s;
s=read(base+'locales.ts');
for(const [lang,index]of [['zh',0],['en',1]]){
 const lines=Object.entries(copy).map(([key,value])=>'  '+JSON.stringify(key)+':'+JSON.stringify(value[index])+',').join('\n');
 s=change(s,'export const '+lang+' = {','export const '+lang+' = {\n'+lines);
}
outputs[base+'locales.ts']=s;
function templateHelpers(s){
 for(const [name,add]of [
  ['__wuyuTemplate154Copy',copy],
  ['__wuyuTemplate154Codes',{TEMPLATE_READING_IN_PROGRESS:409,TEMPLATE_PARSE_PENDING:409,TEMPLATE_PARSE_INCOMPLETE:409}],
  ['__wuyuTemplate154Keys',{TEMPLATE_READING_IN_PROGRESS:'templateAction.readingInProgress',TEMPLATE_PARSE_PENDING:'templateAction.parsePending',TEMPLATE_PARSE_INCOMPLETE:'templateAction.parseIncomplete'}]
 ]){
  const re=new RegExp('const '+name+'=(\\{.*?\\});');const m=s.match(re);if(!m)throw Error('MISSING_LITERAL:'+name);
  const object=JSON.parse(m[1]);for(const key of Object.keys(add))if(Object.hasOwn(object,key))throw Error('ALREADY_PATCHED:'+key);
  s=change(s,m[0],'const '+name+'='+JSON.stringify({...object,...add})+';');
 }
 const a="(!known||(operation==='upload'?!upload.includes(code):upload.includes(code)))";
 const b="(!known||(!['TEMPLATE_READING_IN_PROGRESS','TEMPLATE_PARSE_PENDING','TEMPLATE_PARSE_INCOMPLETE'].includes(code)&&(operation==='upload'?!upload.includes(code):upload.includes(code))))";
 return change(s,a,b);
}
outputs['work-agent/tools/template-browser-panel.js']=templateHelpers(read('work-agent/tools/template-browser-panel.js'));
const interruptedHelper=`function __wuyuReadingInterruption160(reading){const i=reading?.interruption;return reading?.status==='draft_incomplete'&&i&&typeof i==='object'&&!Array.isArray(i)&&['cancelled','failed','interrupted','completed'].includes(i.job_status)&&['reading_cancelled','reading_interrupted','reading_job_incomplete'].includes(i.error_code)?i.error_code:null}`;
const interruptedCopy="(document.documentElement.lang===\"en\"?\"This reading run has stopped. Originals and existing results are retained. Review the current requirements and template in the chat before deciding whether to read again; no model request starts automatically.\":\"本轮资料阅读已停止，原材料与已有结果保留。请先在左侧核对当前需求和模板，再决定是否重新阅读；不会自动发起新的模型请求。\")";
s=templateHelpers(read('work-agent/dist/ui/index.html'));
s=change(s,'function __wuyuReadingFailure114(job){',interruptedHelper+'function __wuyuReadingFailure114(job){');
const readingAnchor='${e.coverage_complete!==!0?`<p class="coverage-notice">';
const independentNotice='${__wuyuReadingInterruption160(e)?`<p class="coverage-notice" data-reading-interruption>${'+interruptedCopy+'}</p>`:""}${!__wuyuReadingInterruption160(e)&&e.coverage_complete!==!0?`<p class="coverage-notice">';
s=change(s,readingAnchor,independentNotice);
outputs['work-agent/dist/ui/index.html']=s;
s=read('work-agent/dist/workflow-action-contract.js');
const guardCopy=`    TEMPLATE_READING_IN_PROGRESS: {
        next_action: "wait_for_current_reading", refresh_required: true,
        message: "资料阅读正在进行，当前模板修改未接受。请等本轮结束后再确认或更换模板；现有阅读继续，不会因此重新派发。",
        instruction: "Read the existing reader job and use bounded waiting. The template mutation was rejected and did not cancel or restart reading. Do not retry it, cancel the reader, change style to force it through, or dispatch another paid read. After the run ends, re-read state before the user confirms or changes the template.",
    },
    TEMPLATE_PARSE_PENDING: {
        next_action: "wait_for_template_parsing", refresh_required: true,
        message: "模板仍在本地解析，本次资料阅读尚未启动。请等待同一解析任务完成；不要重复上传或重复启动阅读。",
        instruction: "Wait for the already accepted local template parse. No new paid reading was started by the rejected action. Preserve the upload and project; do not re-upload or enqueue another reading job. Re-read state after parsing, and complete the actual user template confirmation before reading when that choice is pending.",
    },
    TEMPLATE_PARSE_INCOMPLETE: {
        next_action: "inspect_template_parse", refresh_required: true,
        message: "模板解析尚未完成，当前没有覆盖它的排队或运行中解析任务。请刷新并检查当前模板解析；原文件保留，本次没有启动阅读。",
        instruction: "Read the current template and parser jobs. There is no active parser covering this pending template, so do not keep reporting that it is processing. Preserve the original, inspect local parse recovery in the same project, and do not re-upload, enqueue paid reading or retry automatically. A known failed or cancelled parser may use the existing explicit local retry only after its current state and the user request are checked.",
    },
`;
s=change(s,'const errorGuidance = {','const errorGuidance = {\n'+guardCopy);
outputs['work-agent/dist/workflow-action-contract.js']=s;
s=read('work-agent/dist/workflow-tools.js');
const instruction='用户已选定或正在上传模板时，先等待其本地解析并由用户完成真实模板确认，再启动资料阅读。阅读进行中不要确认、替换模板或用修改style绕过保护；TEMPLATE_READING_IN_PROGRESS只等待已接受阅读，TEMPLATE_PARSE_PENDING只等待同一解析任务。reading.interruption表示该轮已停止，不可继续称正在阅读或无限等待；先核对当前需求、模板及已有缓存，再按明确用户请求与既有费用授权决定是否重新阅读，不自动重试、不保证供应商免费。';
const pattern=/export const researchPreparationOrderInstruction = ("(?:[^"\\]|\\.)*");/;
const match=s.match(pattern);if(!match)throw Error('INSTRUCTION_LITERAL_NOT_FOUND');
s=change(s,match[0],'export const researchPreparationOrderInstruction = '+JSON.stringify(JSON.parse(match[1])+'\n'+instruction)+';');
const key='    const readingWork = pending.some(job => ["analyze_materials", "revise_draft"].includes(String(job.action)));';
s=change(s,key,key+'\n    const readingInterruption = object(reading.interruption);\n    const readingInterrupted = reading.status === "draft_incomplete" && ["cancelled", "failed", "interrupted", "completed"].includes(String(readingInterruption.job_status)) && ["reading_cancelled", "reading_interrupted", "reading_job_incomplete"].includes(String(readingInterruption.error_code));');
const branch=`    else if (readingInterrupted && !pending.length && !failure) {
        stage = "failed";
        summary = "本轮资料阅读已停止，原材料与已有结果保留；当前没有阅读任务在运行。";
        question = "请先核对当前需求和模板，再决定是否重新阅读。新的模型请求仅按明确用户请求与既有费用授权执行，不会自动重试。";
        choices = [];
        requiredAction = null;
        target = {draft_revision:reading.draft_revision};
        recovery = {kind:"reading",error_code:readingInterruption.error_code,keep_project:true,keep_reading:true,next_action:"review_reading_interruption",repeat_paid_action_automatically:false};
    }
`;
s=change(s,'    else if (failure && failure.error_code === "reading_cached_plan_invalid") {',branch+'    else if (failure && failure.error_code === "reading_cached_plan_invalid") {');
outputs['work-agent/dist/workflow-tools.js']=s;
const records={};
for(const [name,text]of Object.entries(outputs)){
 const p=path.join(root,name);const raw=Buffer.from(text,'utf8');fs.writeFileSync(p,raw);
 records[name]={before_SHA256:expected[name],after_SHA256:sha(raw),bytes:raw.length};
}
console.log(JSON.stringify({passed:true,files:records,paid_requests:0,live_changes:0}));
