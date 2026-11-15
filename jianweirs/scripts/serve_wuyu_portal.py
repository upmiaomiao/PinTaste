"""Start the Wuyu product portal; the official DSH remains its planning runtime."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
import uvicorn
from wuyu_harness.config import Settings
from wuyu_harness.harness_planner import plan_task
from wuyu_harness.portal import create_portal_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3080)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs/portal")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    load_dotenv(ROOT / ".env.wuyu", override=False)
    app = create_portal_app(output_dir=args.output_dir, base_settings=Settings.from_env(),
                            planner_adapter=plan_task, origin=f"http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
