"""Bounded real DeepSeek + AMap address matching smoke test, no secrets logged."""
from datetime import datetime, timezone
from pathlib import Path
import asyncio
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import httpx
from wuyu_harness.auth_portal import PortalDB
from wuyu_harness.config import Settings
from wuyu_harness.portal_config import PortalConfig
from wuyu_harness.geocoding import AmapGeocoder
from wuyu_harness.location_resolution import extract_place,rank_places
from wuyu_harness.models import ModelClient


async def main():
    config=PortalConfig(PortalDB(ROOT/'outputs/portal'),Settings.from_env())
    settings=config.settings();geo=config.read()['geography']
    report={'tested_at':datetime.now(timezone.utc).isoformat(),'passed':False,'credentials_logged':False}
    try:
        async with httpx.AsyncClient() as client:
            model=ModelClient(client)
            objective='帮我找到安徽宿州皖创环保园区是否存在固废倾倒和相关情况'
            intent=await extract_place(model,settings.models['main'],objective)
            report['grounded_query']=intent.query if intent else None
            if intent:
                async with AmapGeocoder(geo['api_key'],timeout_seconds=geo['timeout_seconds']) as geocoder:
                    result=await geocoder.search(intent.query,city=intent.city,limit=5)
                chosen=await rank_places(model,settings.models['main'],objective,result)
                report.update(candidate_count=len(result.get('candidates',[])),
                    chosen_candidate_id=chosen['id'] if chosen else None,
                    chosen_name=chosen['name'] if chosen else None,
                    resolution='auto_matched' if chosen else 'user_confirmation_required',
                    coordinates_from_provider_only=True)
            vague=await extract_place(model,settings.models['main'],'看看这个工业园有没有固废')
            report['vague_request_needs_location']=vague is None
            report['passed']=bool(intent and report.get('candidate_count',0)>0 and report['vague_request_needs_location'])
    except Exception as exc:
        report['error_kind']=type(exc).__name__
    path=ROOT/'outputs/runtime-v4-location-live-20261024.json'
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({key:report.get(key) for key in ('passed','candidate_count','resolution','vague_request_needs_location','error_kind')},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(asyncio.run(main()))
