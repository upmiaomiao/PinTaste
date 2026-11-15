"""Independent loopback science/Esri worker; desktop OMAP is not configured."""
import argparse
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file",type=Path,required=True)
    parser.add_argument("--output-dir",type=Path,required=True)
    parser.add_argument("--port",type=int,default=8171)
    args=parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(args.env_file,override=False)
    if not os.getenv("OMAP_WORKER_API_KEY"):
        raise SystemExit("Configure a private worker credential before starting the science worker")
    os.umask(0o077)
    os.environ["OMAP_OUTPUT_DIR"]=str(args.output_dir)
    os.environ["OMAP_SPECTRAL_SUBSET_DIR"]=str(args.output_dir/"spectral/subsets")
    os.environ["OMAP_SAR_OUTPUT_DIR"]=str(args.output_dir/"sar")
    from omap_api.app import create_app
    import uvicorn
    uvicorn.run(create_app(output_dir=args.output_dir),host="127.0.0.1",port=args.port,
                access_log=False,log_level="warning",workers=1)


if __name__=="__main__":main()
