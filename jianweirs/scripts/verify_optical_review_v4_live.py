"""Replay original detector outputs through live same-image Gemini review.

This does not change the user's historical run, annotations or report. Original
registered images and original Jianwei hypotheses are reused as regression data.
Credentials are read only from the local encrypted administrator configuration.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from PIL import Image, ImageDraw

from wuyu_harness.adaptive import model_asset_context
from wuyu_harness.auth_portal import PortalDB
from wuyu_harness.config import Settings
from wuyu_harness.evidence import validate_recognition
from wuyu_harness.models import AUDIT_DIR, ModelClient
from wuyu_harness.optical_review import review_optical_candidates
from wuyu_harness.portal_config import PortalConfig
from wuyu_harness.schemas import Recognition, TaskRequest
from wuyu_harness.store import Store


def image_recognition(package, identity):
    result = package['recognition']; remap = {}; observations = []
    for index, obs in enumerate(result['observations']):
        if obs['asset_id'] == identity:
            remap[index] = len(observations); observations.append(obs)
    candidates = []
    for candidate in result['candidates']:
        if candidate['evidence_indices'] and all(i in remap for i in candidate['evidence_indices']):
            candidates.append({**candidate, 'evidence_indices': [remap[i] for i in candidate['evidence_indices']]})
    return Recognition.model_validate({'observations': observations, 'candidates': candidates})


async def main():
    folder = ROOT/'outputs/optical-review-v4-live-20261024'; folder.mkdir(exist_ok=True)
    audit = folder/'model-audit'; audit.mkdir(exist_ok=True)
    settings = PortalConfig(PortalDB(ROOT/'outputs/portal'), Settings.from_env()).settings()
    store = Store(ROOT/'outputs/portal/engine')
    run = 'a77b059569ba4498b9125c3270baf90c'
    package = json.loads((store.run_dir(run)/'finding-package.json').read_text(encoding='utf-8'))
    task = TaskRequest(objective='复核原候选是否把正常建筑误识别为建筑垃圾；只依据该图可见证据，不联网或调取新图。',
                       task_types=['waste_type_screening'], search_policy=False, acquire_optical=False,
                       online_supplement='uploaded_only')
    report = {'tested_at': datetime.now(timezone.utc).isoformat(), 'regression_run_id': run,
              'test_kind': 'live_Gemini_same_image_replay_of_saved_Jianwei_hypotheses', 'cases': [], 'passed': False}
    events = []
    token = AUDIT_DIR.set(audit)
    try:
        async with httpx.AsyncClient(timeout=150, trust_env=False) as client:
            pipeline = SimpleNamespace(settings=settings, store=store, models=ModelClient(client),
                event=lambda *row: events.append({'step': row[1], 'status': row[2], **row[3]}))
            for asset in package['assets']:
                if asset['metadata']['source'] not in {'arcgis', 'tencent'}: continue
                raw = image_recognition(package, asset['id'])
                if not raw.candidates: continue
                print('OPTICAL_REVIEW_LIVE '+asset['metadata']['source']+' START', flush=True)
                original = store.get(asset['id'], 'image')
                content = store.asset_path(original).read_bytes()
                effective, result, trace = await review_optical_candidates(pipeline, run, task, asset, raw, model_asset_context(asset), content)
                validate_recognition(effective, [asset])
                marked = folder/(asset['metadata']['source']+'-effective.png')
                with Image.open(store.asset_path(original)) as source:
                    image = source.convert('RGB'); drawing = ImageDraw.Draw(image)
                    for observation in effective.observations:
                        if observation.pixel_window: drawing.rectangle(observation.pixel_window, outline='yellow', width=3)
                    image.save(marked)
                row = {'source': asset['metadata']['source'], 'asset_id': asset['id'], 'raw_candidate_count': len(raw.candidates),
                       'effective_candidate_count': len(effective.candidates), 'raw_preserved': raw.model_dump(mode='json') == image_recognition(package, asset['id']).model_dump(mode='json'),
                       'review': result, 'model_trace': trace, 'effective_annotations': marked.name,
                       'effective_recognition': effective.model_dump(mode='json')}
                row['passed'] = result.get('status') == 'completed' and result.get('excluded_candidate_count') == len(raw.candidates) and not effective.candidates and row['raw_preserved']
                report['cases'].append(row)
                print('OPTICAL_REVIEW_LIVE '+asset['metadata']['source']+' '+result['status']+' '+str(result.get('effective_decision')), flush=True)
        report['passed'] = len(report['cases']) == 2 and all(row['passed'] for row in report['cases'])
    except Exception as error:
        report['error_type'] = type(error).__name__
    finally:
        AUDIT_DIR.reset(token)
        report['events'] = events
        (folder/'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('OPTICAL_REVIEW_V4 '+('PASS' if report['passed'] else 'FAIL'), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__': raise SystemExit(asyncio.run(main()))
