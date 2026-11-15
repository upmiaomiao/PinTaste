import test from 'node:test';
import assert from 'node:assert/strict';
import {renderTaskResults,temporalConditions,consumerConclusion} from './task-results.mjs';
test('one authored conclusion precedes collapsed temporal gates and task-specific evidence',()=>{
 const task={id:'change',images:[{id:'unknown'}],task_results:{consumer_conclusion:{text:'保留一处疑似堆存线索，现有时相不足以确认增减。'},results:[{task_type:'stockpile_change',heading:'逐期状态与变化依据',status:'needs_more_data',conclusion:'现有证据尚不能确认变化方向',temporal_evidence:{required:true,ready:false,required_distinct_dates:2,observed_distinct_dates:1,registration_verified:false,date_unknown_asset_ids:['unknown']},evidence:['图1：仅有一幅有效影像'],next_steps:['补齐带实际日期的前后影像']}],timeline:[{figure:'图1',source:'century',actual_capture_date:null,product_year:2024,decision:'unknown'}]}};
 const html=renderTaskResults(task),visible=html.split('<details')[0];assert.match(visible,/保留一处疑似堆存线索，现有时相不足以确认增减。/);assert.doesNotMatch(visible,/时序比较条件|逐期状态与变化依据|补齐带实际日期|2024 版/);assert.match(html,/<details[^>]*><summary>判读依据与核查记录/);assert.doesNotMatch(html,/<details[^>]*\sopen(?:\s|=|>)/);assert.ok(html.indexOf('时序比较条件')<html.indexOf('逐期状态与变化依据'));assert.match(html,/目前可用 1 个/);assert.match(html,/同点空间对应尚未核实/);assert.match(html,/图1的实际拍摄日期或日期适用范围尚未核实/);assert.match(html,/2024 版；不是拍摄日期/);assert.match(html,/尚需补证/);assert.match(html,/现有证据尚不能确认变化方向/);assert.match(html,/<details[^>]*>[\s\S]*图1：仅有一幅有效影像/);assert.doesNotMatch(html,/变化面积.*平方米|项核查目标/);
});
test('all task distinctions and next steps stay inside a single collapsed record without synthesizing classes',()=>{
 const html=renderTaskResults({id:'tasks',summary:'两处候选仍待现场核查，当前图像不能确认其物料性质。',task_results:{results:[{task_type:'waste_type_screening',heading:'候选物料与排除解释',status:'evidence_available',conclusion:'保留候选类别，成分待核验',alternatives:['商品原料'],evidence:['<script>untrusted</script>'],next_steps:['核对物料来源']},{task_type:'environmental_context',heading:'环境受体与核查路径',status:'evidence_available',conclusion:'尚不能据影像确认污染状态或法定风险等级'}]}});
 const visible=html.split('<details')[0];assert.match(visible,/两处候选仍待现场核查/);assert.doesNotMatch(visible,/保留候选类别|环境受体|核对物料来源|下一步/);assert.equal((html.match(/<details\b/g)||[]).length,1);assert.equal((html.match(/data-result-task=/g)||[]).length,2);assert.match(html,/候选物料与排除解释/);assert.match(html,/环境受体与核查路径/);assert.match(html,/商品原料/);assert.match(html,/&lt;script&gt;/);assert.doesNotMatch(html,/<script>|时序比较条件|项核查目标/);assert.equal(renderTaskResults({}), '');
});

test('dedicated conclusion locations have deterministic priority and render without task cards',()=>{
 const task={consumer_conclusion:{text:'任务层结论。'},package:{consumer_conclusion:{text:'包层结论。'},narrative:{summary:'旧摘要。'}},task_results:{consumer_conclusion:{text:'唯一主结论。'}}};
 assert.equal(consumerConclusion(task),'任务层结论。');assert.equal(consumerConclusion({...task,task_results:{}}),'任务层结论。');assert.equal(consumerConclusion({package:task.package}),'包层结论。');
 const html=renderTaskResults(task);assert.match(html,/class="consumer-conclusion">任务层结论。<\/p>/);assert.doesNotMatch(html,/唯一主结论|包层结论|旧摘要|<details/);
});

test('legacy fallback skips brand, model bylines and headings, then takes the first complete paragraph',()=>{
 const first='图内两处候选边界仍不清晰，保留疑似堆存线索；现有材料不足以确认物料成分。';
 const task={summary:'## 无隅见微\n\n无隅AI平台\n\n模型：Gemini 3.8 Flash\n\nGemini 3.8 Flash · 场景与误报复核\n\n**本次核查结论**\n'+first+'\n\n后续段落：补齐日期后再判断变化。',package:{narrative:{summary:'不应优先采用的第二摘要。'}}};
 assert.equal(consumerConclusion(task),first);const html=renderTaskResults(task);assert.match(html,/图内两处候选/);assert.doesNotMatch(html,/Gemini|模型：|无隅AI|后续段落|第二摘要/);
 assert.equal(consumerConclusion({summary:'无隅见微',package:{narrative:{summary:first}}}),first);
});

test('a long historical first paragraph is preserved whole, including its final limitation',()=>{
 const paragraph='可见不规则纹理和场地关系，仍保留疑似线索。'.repeat(25)+'最后一句：实际物料和现时状态均待现场核查。';
 assert.ok(paragraph.length>300);assert.equal(consumerConclusion({summary:paragraph+'\n\n另一个自然段。'}),paragraph);
 const html=renderTaskResults({summary:paragraph});assert.ok(html.includes(paragraph));assert.doesNotMatch(html,/另一个自然段|…|\.\.\./);
});

test('a missing authored summary never promotes raw task classes into an invented unified conclusion',()=>{
 const html=renderTaskResults({task_results:{results:[{task_type:'waste_type_screening',heading:'固废类型识别',conclusion:'仅保存原始行结论。'}]}});
 assert.doesNotMatch(html.split('<details')[0],/原始行结论|consumer-conclusion">/);assert.match(html,/<details[^>]*>[\s\S]*仅保存原始行结论/);
});

test('untrusted conclusion, task identifiers and timeline values remain escaped text',()=>{
 const html=renderTaskResults({id:'"><script>bad</script>',consumer_conclusion:{text:'候选 <img src=x onerror=alert(1)> 仍待核查。'},task_results:{results:[],timeline:[{source:'<svg onload=bad()>',figure:'<script>figure</script>',actual_capture_date:'<img onerror=bad()>',reported_actual_dates:['<script>date</script>'],date_scope:'"><script>scope</script>'}]}});
 assert.match(html,/&lt;img src=x onerror=alert\(1\)&gt;/);assert.match(html,/&lt;script&gt;date/);assert.doesNotMatch(html,/<script|<img|<svg|\son\w+=['"]|<details[^>]*\sopen(?:\s|=|>)/);
});
test('passing temporal conditions remain qualitative and annual editions are never converted to capture dates',()=>{
 const html=temporalConditions({required:true,ready:true,required_distinct_dates:3,observed_distinct_dates:3,registration_verified:true});assert.match(html,/具备定性比较条件/);assert.match(html,/仍需结合逐期可见证据复核/);assert.match(html,/均不代替实际拍摄日期/);assert.doesNotMatch(html,/暂不能确认变化/);
});

test('information and policy answers retain their full response instead of rendering a screening card',()=>{
 const summary='国家与地方政策应结合任务范围分别核对。\n\n第二段保留具体适用条件和来源，不应被结论卡截去。';
 for(const scope of [{task:{search_only:true}},{package:{task:{search_only:true}}}]){
  assert.equal(renderTaskResults({...scope,summary,task_results:{consumer_conclusion:{text:'不应展示的筛查结论。'}}}),'');
 }
 assert.equal(consumerConclusion({summary,task:{search_only:true}}),'国家与地方政策应结合任务范围分别核对。');
});

test('scientific date and catalog research never renders a compact screening conclusion',()=>{
 const summary='已按月份核验科学影像目录。\n\n后续各月结果、缺口和日期范围需要完整展示。';
 const variants=[{research_result:{mode:'availability'}},{package:{research_result:{mode:'availability'}}},
  {fusion:{screening_applicable:false}},{package:{fusion:{screening_applicable:false}}},
  {fusion:{level:'not_applicable'}},{package:{fusion:{level:'not_applicable'}}},
  {fusion:{},package:{fusion:{screening_applicable:false}}}];
 for(const variant of variants){
  assert.equal(renderTaskResults({...variant,summary,task_results:{results:[{task_type:'site_tracking',conclusion:'旧任务行记录。'}]}}),'');
 }
 assert.equal(consumerConclusion({summary,research_result:{mode:'availability'}}),'已按月份核验科学影像目录。');
});
