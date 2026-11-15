"""对已保存的真实证据包只重算确定性融合与任务结论（不调用模型/不取图），输出 JSON 汇总。"""
import json, sys, glob, os
sys.path.insert(0, os.getcwd())
from wuyu_harness.schemas import TaskRequest, Recognition
from wuyu_harness.fusion import fuse_findings
from wuyu_harness.task_results import build_task_results

out = {}
paths = sorted(p for p in glob.glob('outputs/**/finding-package.json', recursive=True)) + \
        sorted(glob.glob('outputs/dataset-evaluation-20261018/**/LIVE-*.json', recursive=True))
for path in paths:
    try:
        p = json.load(open(path, encoding='utf-8'))
        if 'task' not in p or 'image_assessments' not in p: continue
        task = TaskRequest.model_validate(p['task'])
        visual = ((p.get('adaptive_review') or {}).get('main_visual_review'))
        f = fuse_findings(task, p['assets'], p['image_assessments'], p['strategy'], visual)
        rec = Recognition.model_validate(p.get('recognition') or {})
        tr = build_task_results(task, p['assets'], rec, p['image_assessments'], f, scientific_evidence=p.get('scientific_evidence'))
        old = p.get('fusion') or {}
        out[path] = {'kinds': [str(k) for k in task.task_types],
                     'old_level': old.get('level'), 'new_level': f['level'], 'judgment': f['judgment'],
                     'pending': f['candidate_presence']['has_pending_candidates'],
                     'results': [(r.get('task_type'), r.get('status'), (r.get('conclusion') or '')[:60]) for r in tr.get('results', [])]}
    except Exception as e:
        out[path] = {'error': f'{type(e).__name__}: {str(e)[:160]}'}
json.dump(out, open(sys.argv[1], 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print(len(out))
