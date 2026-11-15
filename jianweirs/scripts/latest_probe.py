"""Bounded real captures; does not change the desktop map or print credentials."""
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from omap_api.protocol import OmapClient

SOURCES = [(35, 'tianditu'), (48, 'siwei'), (23, 'siwei_daily'),
           (221, 'amap'), (220, 'tencent'), (317, 'google'),
           (329, 'mapbox'), (330, 'mapquest'), (500, 'arcgis'),
           (246, 'history246'), (291, 'ovital_history')]

async def main():
    load_dotenv(ROOT / '.env')
    client = OmapClient(os.environ['OMAP_WS_URL'], os.environ['OMAP_PSK'], timeout=20)
    folder = ROOT / 'outputs' / 'expanded'
    folder.mkdir(parents=True, exist_ok=True)
    reports = []
    for map_id, name in SOURCES:
        check = {'source': name, 'map_type': map_id}
        started = asyncio.get_running_loop().time()
        try:
            result = await client.capture({'bbox': [116.446,39.849,116.456,39.859],
                'map_type': map_id, 'coord_type': 0, 'zoom': 16,
                'width': 512, 'height': 512, 'timeout_seconds': 15})
            (folder / f'{name}.png').write_bytes(result['image_bytes'])
            check.update(status='captured', metadata=result['metadata'])
        except Exception as exc:
            check.update(status='unavailable', error_type=type(exc).__name__,
                         error_code=getattr(exc, 'code', None), message=str(exc))
        check['elapsed_seconds'] = round(asyncio.get_running_loop().time()-started, 3)
        reports.append(check)
        print(json.dumps(check, ensure_ascii=False), flush=True)
        (folder/'latest-probe.json').write_text(json.dumps({
            'tested_at':datetime.now(timezone.utc).isoformat(), 'checks':reports
        }, ensure_ascii=False, indent=2), encoding='utf-8')

if __name__ == '__main__':
    asyncio.run(main())
