import {escapeHTML as esc,TASK_TYPES} from './lib.mjs';
export function workbookBatchModal(workbook={},draft={},icon=()=> '') {
 const records=workbook.records||[];
 return `<header class="modal-header"><h2>批量点位核查 <span class="pill">${records.length} 个点位</span></h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon('close')}</button></header><form id="workbook-batch-form" data-id="${esc(workbook.id)}"><div class="modal-body"><p>先选择点位和核查目标，再创建逐点任务。每个点位单独保留影像、结论和报告。</p>${(workbook.proposals||[]).length?`<section class="workbook-proposals"><h3>按这份清单建议的任务</h3><p>我先读表内的变化标记与期间，提出这些核查方向。选择一项可填入要求和点位；确认提交前不会取图。</p><div>${workbook.proposals.map(proposal=>`<button class="secondary" type="button" data-action="batch-proposal" data-id="${esc(proposal.id)}">${esc(proposal.label)} · ${proposal.point_count} 个点位</button>`).join('')}</div></section>`:''}<label class="field">本次核查要求<textarea name="objective" required rows="3" maxlength="12000" placeholder="例如核查表内新增及面积增加点位的堆存变化，结合已知日期影像逐点说明。">${esc(draft.objective||'请核查所选疑似点位的当前可见堆存线索；如需比较变化，先核对历史影像实际日期与空间对应，再形成逐点结论。')}</textarea></label><div class="field-grid"><label class="field">主要核查目标<select name="task_type"><option value="">按我的要求自动确定</option>${TASK_TYPES.map(([value,label,stage])=>`<option value="${value}">${stage} · ${label}</option>`).join('')}</select></label><label class="field">表格坐标依据<select name="coordinate_system" required><option value="" selected disabled>请先确认坐标来源</option><option value="wgs84">WGS84 / GPS 经纬度</option><option value="gcj02">高德 / GCJ-02 经纬度</option></select><small>坐标依据不明时，先核实原表；不能把未知坐标直接用于同点位变化判断。</small></label><label class="field">比较起始日期（可选）<input name="date_start" type="date" value="${esc(draft.date_start||'')}"></label><label class="field">比较结束日期（可选）<input name="date_end" type="date" value="${esc(draft.date_end||'')}"></label></div><label class="source-choice"><input name="search_policy" type="checkbox" ${draft.search_policy?'checked':''}><span>本批任务启用联网检索<small>根据任务补充国家、省、市公开政策，并单独列明来源。</small></span></label><div class="batch-selection-heading"><strong>选择要核查的点位</strong><div><button class="text-button" type="button" data-action="batch-select-all">全选</button><button class="text-button" type="button" data-action="batch-clear">清空</button></div></div><div class="batch-records">${records.map(record=>`<label class="source-choice batch-record"><input type="checkbox" name="record_id" value="${esc(record.record_id)}"><span><b>${esc(record.site_id||record.record_id)}</b><small>${esc([record.administrative_area,record.address].filter(Boolean).join(' · ')||record.source?.sheet||'工作簿点位')}</small></span></label>`).join('')}</div></div><footer class="modal-footer"><span class="batch-selection-count">已选 0 个点位</span><button class="secondary" type="button" data-action="close-modal">取消</button><button class="primary" type="submit">创建所选点位任务</button></footer></form>`;
}
export function workbookBatchPayload(fields,selectedSources=[],proposalTaskTypes=[]) {
 const record_ids=[...new Set(fields.getAll('record_id').filter(Boolean))];
 if(!record_ids.length)throw new Error('请至少选择一个要核查的点位。');
 const coordinate_system=String(fields.get('coordinate_system')||'');
 if(!['wgs84','gcj02'].includes(coordinate_system))throw new Error('请先确认表格的坐标依据。');
 const objective=String(fields.get('objective')||'').trim();if(!objective)throw new Error('请说明本批点位的核查要求。');
 const task_type=String(fields.get('task_type')||''),body={record_ids,objective,coordinate_system,task_types:task_type?[task_type]:[...proposalTaskTypes],search_policy:fields.get('search_policy')==='on'};
 for(const key of ['date_start','date_end'])if(fields.get(key))body[key]=String(fields.get(key));
 if(body.date_start&&body.date_end&&body.date_start>body.date_end)throw new Error('比较起始日期不能晚于结束日期。');
 if(selectedSources.length)body.selected_sources=[...selectedSources];
 return body;
}

export function applyWorkbookProposal(form,proposal) {
 if(!form||!proposal)return;
 form.elements.objective.value=proposal.objective||'';
 form.elements.task_type.value='';
 form.dataset.proposalTaskTypes=JSON.stringify(proposal.task_types||[]);
 for(const key of ['date_start','date_end'])form.elements[key].value=proposal[key]||'';
 const ids=new Set(proposal.record_ids||[]);
 for(const field of form.querySelectorAll('[name="record_id"]'))field.checked=ids.has(field.value);
 for(const option of form.querySelectorAll('[data-action="batch-proposal"]'))option.classList.toggle('active',option.dataset.id===proposal.id);
}
