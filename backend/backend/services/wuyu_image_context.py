"""Fit image-only transport without changing confirmed copy or source evidence."""
from copy import deepcopy
import hashlib,json
from .wuyu_errors import PipelineError

def fit_image_prompt(prompt,limit=32000):
    if len(prompt)<=limit:return prompt
    value=json.loads(prompt);spec=value.get('slide_spec',{})
    # Original evidence remains complete in the server spec, MD and notes. The
    # image model prints confirmed short copy and receives actual original
    # figure bytes/table rows; repeated full paragraphs need not be reprinted.
    evidence=spec.get('evidence',[])
    if isinstance(evidence,list):
        index=[]
        for record in evidence:
            if not isinstance(record,dict):continue
            text=str(record.get('text',''))
            index.append({k:deepcopy(record[k])for k in ('ref','source')if k in record}|
                         {'original_text_chars':len(text),'original_text_sha256':hashlib.sha256(text.encode()).hexdigest()})
        spec.pop('evidence',None);spec['source_evidence_index']=index
        value['context_projection']={'mode':'confirmed_copy_and_original_visual_references',
            'full_evidence_preserved_in':'server_slide_spec_markdown_and_speaker_notes',
            'rule':'Only confirmed visible fields and verified table rows or reference images can support the rendered page; do not infer new facts from source locators.'}
    result=json.dumps(value,ensure_ascii=False)
    if len(result)>limit:
        raise PipelineError('image_prompt_limit','本页已确认文案或原始数据表超过生图输入上限；完整原文已保留，请先在大纲中精简本页展示内容或拆页，未调用生图。')
    return result
