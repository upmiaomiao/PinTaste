"""Save an operator-supplied Web Service key in the existing encrypted portal config."""
from pathlib import Path
import getpass
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from wuyu_harness.auth_portal import PortalDB
from wuyu_harness.config import Settings
from wuyu_harness.portal_config import PortalConfig

load_dotenv(ROOT / '.env.wuyu', override=False)
key = getpass.getpass('AMap Web Service key (hidden): ')
if not key or not key.isalnum() or not 20 <= len(key) <= 64:
    print('INVALID_WEB_SERVICE_KEY_FORMAT')
    raise SystemExit(2)
config = PortalConfig(PortalDB(ROOT / 'outputs/portal'), Settings.from_env())
config.update({'geography': {'enabled': True, 'api_key': key, 'timeout_seconds': 8}})
print('AMAP_KEY_SAVED_ENCRYPTED; no credential was printed')
