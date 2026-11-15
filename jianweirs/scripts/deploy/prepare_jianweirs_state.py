"""Stage only encrypted product configuration, never legacy users or task data."""
import argparse
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv, dotenv_values
from wuyu_harness.auth_portal import PortalDB
from wuyu_harness.config import Settings
from wuyu_harness.portal_config import PortalConfig


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args()
    destination=args.destination.resolve()
    if destination.exists():raise SystemExit('Destination already exists; configuration preserved.')
    load_dotenv(ROOT/'.env.wuyu',override=False)
    current=PortalConfig(PortalDB(ROOT/'outputs/portal'),Settings.from_env()).read()
    private=dotenv_values(ROOT/'.local/unified-access.env')
    if not private.get('WUYU_UNIFIED_API_KEY'):raise SystemExit('New imagery key not configured locally.')
    current['unified_data']={'enabled':True,'base_url':private['WUYU_UNIFIED_BASE_URL'],
        'api_key':private['WUYU_UNIFIED_API_KEY'],'max_concurrency':2,'timeout_seconds':120}
    science_key=secrets.token_urlsafe(36)
    current['data']={'base_url':'http://127.0.0.1:8171','api_key':science_key,'mode':'worker'}
    current['task_policy'].update(search_policy=False,concurrency=2,max_assets=8)
    current['sources']['source_order']=list(dict.fromkeys(['gaofen2026','arcgis','tianditu',*current['sources'].get('source_order',[])]))
    destination.mkdir(parents=True,mode=0o700)
    portal=PortalConfig(PortalDB(destination/'portal'),Settings.from_env())
    portal.save(current)
    env=destination/'server.env'
    env.write_text('WUYU_AUTH_MODE=wuyu\nWUYU_IDENTITY_URL=http://127.0.0.1:8920\n'
        'WUYU_PORTAL_DIR=/opt/wuyu-jianweirs/state/portal\nWUYU_PUBLIC_ORIGIN=https://ai.wuyullm.com\n'
        'OMAP_WORKER_API_KEY='+science_key+'\nOMAP_OUTPUT_DIR=/opt/wuyu-jianweirs/state/science\n'
        'OMAP_ALLOW_REMOTE=0\nDSH_TELEMETRY_DISABLED=1\n',encoding='utf-8')
    env.chmod(0o600)
    # public summary only
    print('Staged encrypted configuration; no user accounts or task history copied.')
    print('Model roles configured: '+','.join(role for role,value in current['models'].items() if value.get('api_key')))
    print('New optical PNG + independent science worker enabled; per-task web search defaults off.')


if __name__=='__main__':main()
