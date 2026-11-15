"""Linux service entry; all persistent settings stay outside the source release."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from dotenv import load_dotenv


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--host",default="172.17.0.1")
    parser.add_argument("--port",type=int,default=8170)
    parser.add_argument("--root-path",default="/jianweirs")
    parser.add_argument("--origin",default="https://ai.wuyullm.com")
    args=parser.parse_args()
    load_dotenv(args.env_file,override=False)
    os.umask(0o077)
    args.output_dir.mkdir(parents=True,exist_ok=True)
    os.environ["WUYU_OUTPUT_DIR"]=str(args.output_dir)
    from wuyu_harness.config import Settings
    from wuyu_harness.harness_planner import plan_task
    from wuyu_harness.portal import create_portal_app
    import uvicorn
    app=create_portal_app(output_dir=args.output_dir,base_settings=Settings.from_env(),planner_adapter=plan_task,origin=args.origin)
    uvicorn.run(app,host=args.host,port=args.port,workers=1,root_path=args.root_path,
                proxy_headers=True,forwarded_allow_ips=os.getenv("WUYU_PROXY_IPS","172.20.0.2,172.29.0.3,127.0.0.1"),
                access_log=False,log_level="warning")


if __name__=="__main__":main()
