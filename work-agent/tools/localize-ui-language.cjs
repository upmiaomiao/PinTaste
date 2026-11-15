/** Reproducible localization of the exact running UI; no old business bundle is rebuilt. */
const fs=require('fs'),path=require('path'),crypto=require('crypto'),assert=require('assert/strict');
if(!process.argv[2]||!process.argv[3]){console.error('Usage: node localize-ui-language.cjs BASELINE_HTML OUTPUT_HTML [TYPESCRIPT_MODULE_PATH]');process.exit(2);}
const ts=require(process.argv[4]?path.resolve(process.argv[4]):'typescript');
const root=__dirname,input=path.resolve(process.argv[2]),outputPath=path.resolve(process.argv[3]),reportRoot=path.dirname(outputPath),before=fs.readFileSync(input,'utf8');
assert.notEqual(input,outputPath,'Use a separate output file; do not overwrite the baseline');
const EXPECTED='5f0eefdd03047b339e3c08c35733f6883863be1fcb5f1d2cee087ef7f98562db';assert.equal(crypto.createHash('sha256').update(before).digest('hex'),EXPECTED);
const match=before.match(/<script type="module" crossorigin>([\s\S]*?)<\/script>/);assert.ok(match);const code=match[1],source=ts.createSourceFile('runtime.js',code,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS);assert.equal(source.parseDiagnostics.length,0);
const structure=JSON.parse(fs.readFileSync(path.join(root,'ui-language-structure.json'),'utf8'));assert.equal(structure.app,'Rt');assert.equal(structure.state,'m');assert.equal(structure.sha256,EXPECTED);
const names={text:'__wuyu_panel18_text',html:'__wuyu_panel18_html',bind:'__wuyu_panel18_bind'};for(const name of Object.values(names))assert.ok(!code.includes(name));
const messages={};for(const line of fs.readFileSync(path.join(root,'ui-language-messages.txt'),'utf8').split(/\r?\n/)){if(!line)continue;const i=line.indexOf('|||');assert.ok(i>=0);messages[line.slice(0,i)]=line.slice(i+3);}
const chunks={},missing=new Set(),changed=[],protectedTemplates=[];let connect=0,getterMaps=0;
let partialRange=null;function locatePartial(n){if(ts.isFunctionDeclaration(n)&&n.getText(source).includes('请协助我补充或替换本次尚未读完的材料'))partialRange=[n.getStart(source),n.end];ts.forEachChild(n,locatePartial);}locatePartial(source);
const insideFunction=n=>{for(let p=n.parent;p;p=p.parent)if(ts.isFunctionLike(p))return true;return false;};
const isConsent=n=>/^(?:请协助我补充或替换|我已查看第|我已核对第|请保留已读取材料)/.test(n);
function translateChunk(text){let translated=text;const phrases=[...new Set([...text.matchAll(/[^\r\n<>"'`{}]*[\u3400-\u9fff][^\r\n<>"'`{}]*/g)].map(m=>m[0].trim()))].sort((a,b)=>b.length-a.length);for(const phrase of phrases){if(Object.hasOwn(messages,phrase))translated=translated.split(phrase).join(messages[phrase]);else missing.add(phrase);}chunks[text]=translated;}
function comparisonLiteral(n){const p=n.parent;if(ts.isBinaryExpression(p)&&[ts.SyntaxKind.EqualsEqualsToken,ts.SyntaxKind.EqualsEqualsEqualsToken,ts.SyntaxKind.ExclamationEqualsToken,ts.SyntaxKind.ExclamationEqualsEqualsToken].includes(p.operatorToken.kind))return true;if(ts.isCallExpression(p)&&ts.isPropertyAccessExpression(p.expression)&&['includes','indexOf','startsWith','endsWith','replace','split'].includes(p.expression.name.text))return true;return false;}
function rewrite(n){const start=n.getStart(source),original=code.slice(start,n.end);if(n.end<=structure.appStart)return original;
 if(ts.isCallExpression(n)&&ts.isPropertyAccessExpression(n.expression)&&n.expression.name.text==='sendMessage')return original;
 const partial=partialRange&&start>=partialRange[0]&&n.end<=partialRange[1];
 if(partial&&(ts.isStringLiteral(n)||ts.isNoSubstitutionTemplateLiteral(n))&&['原图','材料 '].includes(n.text))return original;
 if(partial&&ts.isTemplateExpression(n)&&(n.head.text==='材料 '||n.templateSpans.some(s=>s.literal.text.includes('（原图'))))return original;
 if((ts.isStringLiteral(n)||ts.isNoSubstitutionTemplateLiteral(n))&&isConsent(n.text)){protectedTemplates.push(n.text);return original;}
 if(ts.isTemplateExpression(n)&&isConsent(n.head.text)){protectedTemplates.push(original);return original;}
 if(ts.isAwaitExpression(n)&&ts.isCallExpression(n.expression)&&ts.isPropertyAccessExpression(n.expression.expression)&&n.expression.expression.expression.getText(source)===structure.app&&n.expression.expression.name.text==='connect'){
  assert.ok(ts.isExpressionStatement(n.parent)||ts.isBinaryExpression(n.parent)&&n.parent.operatorToken.kind===ts.SyntaxKind.CommaToken,'connect result must be discarded');connect++;return `(${original},${names.bind}(${structure.app}))`;
 }
 if(ts.isObjectLiteralExpression(n)&&!insideFunction(n)&&ts.isVariableDeclaration(n.parent)){
  const props=n.properties;if(props.length>=3&&props.every(p=>ts.isPropertyAssignment(p)&&ts.isStringLiteral(p.initializer))&&props.some(p=>/[\u3400-\u9fff]/.test(p.initializer.text))){
   const expected=structure.objects.find(o=>o.pos===n.parent.getStart(source));assert.ok(expected,'Unknown module dictionary');getterMaps++;
   return '{'+props.map(p=>`get ${p.name.getText(source)}(){return ${names.text}(${JSON.stringify(p.initializer.text)})}`).join(',')+'}';
  }
 }
 if((ts.isStringLiteral(n)||ts.isNoSubstitutionTemplateLiteral(n))&&/[\u3400-\u9fff]/.test(n.text)&&insideFunction(n)&&!comparisonLiteral(n)){
  if(/[<>]/.test(n.text)){translateChunk(n.text);changed.push({kind:'static_html',before:n.text});return `(${names.html}([${JSON.stringify(n.text)}]))`;}
  if(!Object.hasOwn(messages,n.text)&&!Object.hasOwn(messages,n.text.trim()))missing.add(n.text);changed.push({kind:'ui_label',before:n.text});return `(${names.text}(${JSON.stringify(n.text)}))`;
 }
 const patches=[];ts.forEachChild(n,child=>{const value=rewrite(child);if(value!==code.slice(child.getStart(source),child.end))patches.push({start:child.getStart(source)-start,end:child.end-start,value});});let result=original;for(const patch of patches.sort((a,b)=>b.start-a.start))result=result.slice(0,patch.start)+patch.value+result.slice(patch.end);
 if(ts.isTemplateExpression(n)&&insideFunction(n)&&/[\u3400-\u9fff]/.test(n.head.text+n.templateSpans.map(s=>s.literal.text).join(''))){translateChunk(n.head.text);for(const s of n.templateSpans)translateChunk(s.literal.text);changed.push({kind:'static_template',before:n.head.text+n.templateSpans.map(s=>s.literal.text).join('__EXPRESSION__')});return '('+names.html+result+')';}
 return result;
}
let rewritten=rewrite(source);assert.equal(connect,1);assert.ok(getterMaps>=8);
// Known static chrome only: no arbitrary text-node scan, no MutationObserver.
const chrome=[['#feedback-title','textContent','提出修改意见'],['#feedback-dialog [data-close]','aria-label','关闭'],['#feedback-form > p','textContent','意见会放入左侧输入框，由你确认后发送；这里不会直接生成、修改或批准页面。'],['label[for="feedback-text"]','textContent','具体意见'],['#feedback-text','placeholder','说明希望保留或调整的内容'],['#feedback-submit','textContent','放入对话输入框'],['#library-title','textContent','图文参考库'],['#library-dialog [data-close]','aria-label','关闭参考库'],['#library-search-label','textContent','关键词'],['#library-search','placeholder','技术路线、研究基础、市场分析…'],['#library-source-label','textContent','来源'],['#library-source option[value=""]','textContent','全部来源'],['#library-source option[value="local"]','textContent','本地资料'],['#library-source option[value="user"]','textContent','已审核共享模板'],['#library-form button','textContent','检索'],['#library-dialog > .preview-note','textContent','只读浏览。选用参考、审核样张和修改页面请通过左侧对话确认。'],['#library-more','textContent','加载更多来源'],['#brand-asset','alt','无隅 AI 原品牌标识']];
const helper=`
const __wuyu_panel18_state={language:'zh-CN'};
const __wuyu_panel18_messages=${JSON.stringify(messages)};
function ${names.text}(value){const text=String(value??'');if(__wuyu_panel18_state.language!=='en')return text;if(Object.hasOwn(__wuyu_panel18_messages,text))return __wuyu_panel18_messages[text];const trimmed=text.trim();return Object.hasOwn(__wuyu_panel18_messages,trimmed)?text.replace(trimmed,__wuyu_panel18_messages[trimmed]):text;}
function __wuyu_panel18_static(chunk){return chunk.replace(/[^\\r\\n<>"'\x60{}]*[\\u3400-\\u9fff][^\\r\\n<>"'\x60{}]*/g,part=>{const key=part.trim();return Object.hasOwn(__wuyu_panel18_messages,key)?part.replace(key,__wuyu_panel18_messages[key]):part;});}
function ${names.html}(strings,...values){let result='';for(let i=0;i<strings.length;i++){const chunk=strings[i];result+=__wuyu_panel18_state.language==='en'?__wuyu_panel18_static(chunk):chunk;if(i<values.length)result+=String(values[i]);}return result;}
function __wuyu_panel18_chrome(){document.title=${names.text}('无隅 AI · PPT详情');document.documentElement.lang=__wuyu_panel18_state.language;for(const [selector,attribute,key]of ${JSON.stringify(chrome)}){const element=document.querySelector(selector);if(!element)continue;if(attribute==='textContent')element.textContent=${names.text}(key);else element.setAttribute(attribute,${names.text}(key));}}
function __wuyu_panel18_set(value){const next=typeof value==='string'&&/^en(?:-|$)/i.test(value)?'en':'zh-CN',changed=next!==__wuyu_panel18_state.language;__wuyu_panel18_state.language=next;__wuyu_panel18_chrome();if(changed&&typeof ${structure.state}.onchange==='function')${structure.state}.onchange();}
function ${names.bind}(app){app.onhostcontextchanged=context=>__wuyu_panel18_set(context?.locale);__wuyu_panel18_set(app.getHostContext()?.locale);}
`;
const combined=helper+rewritten,parsed=ts.createSourceFile('localized.js',combined,ts.ScriptTarget.Latest,true,ts.ScriptKind.JS);assert.equal(parsed.parseDiagnostics.length,0,JSON.stringify(parsed.parseDiagnostics.map(d=>({start:d.start,message:ts.flattenDiagnosticMessageText(d.messageText,' ')}))));
let output=before.replace(match[0],()=>'<script type="module" crossorigin>'+combined+'</script>');output=output.replace('<label>关键词<input','<label><span id="library-search-label">关键词</span><input').replace('<label>来源<select','<label><span id="library-source-label">来源</span><select');
const target=outputPath;fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,output);
const report={input_sha256:EXPECTED,output_sha256:crypto.createHash('sha256').update(output).digest('hex'),input_bytes:Buffer.byteLength(before),output_bytes:Buffer.byteLength(output),ui_label_changes:changed.filter(x=>x.kind==='ui_label').length,static_template_changes:changed.filter(x=>x.kind==='static_template').length,static_html_changes:changed.filter(x=>x.kind==='static_html').length,module_ui_getter_maps:getterMaps,connect_bridges:connect,protected_consent_templates:protectedTemplates.length,missing:[...missing],vendor_prefix_unchanged:rewritten.slice(0,structure.appStart)===code.slice(0,structure.appStart),business_source_rebuilt:false,runtime_source_used:true,static_template_chunks_only:true,dynamic_values_not_translated:true,model_calls:0};
fs.writeFileSync(path.join(reportRoot,'panel-runtime-candidate-validation.json'),JSON.stringify(report,null,2)+'\n');fs.writeFileSync(path.join(reportRoot,'panel-runtime-helper.js'),helper);fs.writeFileSync(path.join(reportRoot,'panel-runtime-codemod-changes.json'),JSON.stringify(changed,null,2));console.log(JSON.stringify(report,null,2));
