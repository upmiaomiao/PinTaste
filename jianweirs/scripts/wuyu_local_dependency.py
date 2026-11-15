"""Print only the managed local worker port, never connection credentials."""
import argparse
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portal-dir", type=Path, required=True)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env.wuyu", override=False)
    endpoint = os.getenv("WUYU_DATA_BASE_URL", "http://127.0.0.1:8765")
    mode = os.getenv("WUYU_DATA_MODE", "auto")
    if (args.portal_dir / "portal.sqlite3").is_file():
        from wuyu_harness.auth_portal import PortalDB
        from wuyu_harness.config import Settings
        from wuyu_harness.portal_config import PortalConfig
        data = PortalConfig(PortalDB(args.portal_dir), Settings.from_env()).read()["data"]
        endpoint, mode = data["base_url"], data["mode"]
    parsed = urlsplit(endpoint)
    if (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
            and parsed.port == 8765 and mode in ("auto", "worker")):
        print("8765")


if __name__ == "__main__":
    main()
