"""Start the local Wuyu HTTP service without changing the existing imagery API."""

import argparse
import os

from dotenv import load_dotenv


def main() -> None:
    load_dotenv(".env.wuyu", override=False)
    parser = argparse.ArgumentParser(description="无隅见微多星遥感 Harness API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost") and not os.getenv("WUYU_SERVICE_API_KEY"):
        parser.error("绑定非本机地址需要设置 WUYU_SERVICE_API_KEY")
    import uvicorn
    uvicorn.run("wuyu_harness.app:app", host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
