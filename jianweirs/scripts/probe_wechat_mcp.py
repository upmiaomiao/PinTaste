"""Read-only MCP discovery, latency measurement, and bounded article probes."""
import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]


def unwrap(result):
    value = result.structuredContent
    if isinstance(value, dict):
        return value
    for item in result.content:
        if getattr(item, "type", "") == "text":
            try:
                return json.loads(item.text)
            except ValueError:
                continue
    return {}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://152.136.236.199:8077/mcp")
    parser.add_argument("--search", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs" / "wechat-integration-20261022" / "mcp-probe.json")
    args = parser.parse_args()
    report = {"tested_at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "endpoint": args.url, "health": [], "steps": []}
    async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
        async def health(url):
            tick = time.perf_counter()
            item = {"url": url}
            try:
                reply = await http.get(url)
                item.update(status=reply.status_code, data=reply.json())
            except Exception as error:
                item["error_type"] = type(error).__name__
            item["seconds"] = round(time.perf_counter()-tick, 3)
            return item
        report["health"] = await asyncio.gather(*(health(url) for url in ["http://127.0.0.1:8077/healthz", "http://collectron-wechat-search:8077/healthz", args.url.rsplit("/", 1)[0] + "/healthz"]))
    try:
        async with httpx.AsyncClient(timeout=180, trust_env=False) as http:
            async with streamable_http_client(args.url, http_client=http) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=180)) as session:
                    async def record(name, operation):
                        tick = time.perf_counter()
                        value = await operation
                        report["steps"].append({"step": name, "seconds": round(time.perf_counter()-tick, 3), "data": value.model_dump(mode="json", exclude_none=True)})
                        print(json.dumps({"step": name, "seconds": report["steps"][-1]["seconds"]}, ensure_ascii=False), flush=True)
                        args.out.parent.mkdir(parents=True, exist_ok=True)
                        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                        return value
                    await record("initialize", session.initialize())
                    listed = await record("tools/list", session.list_tools())
                    names = [tool.name for tool in listed.tools]
                    print(json.dumps({"tools": names}, ensure_ascii=False), flush=True)
                    if "wechat_search_status" in names:
                        await record("status", session.call_tool("wechat_search_status", {}))
                    if args.search:
                        result = await record("search", session.call_tool("wechat_search", {"query": "固废处理", "max_results": 5, "max_pages": 1, "time_range": "two_years", "sort": "recent"}))
                        body = unwrap(result)
                        rows = body.get("items") or body.get("articles") or body.get("results") or []
                        if isinstance(rows, list):
                            for index, row in enumerate(rows[:2]):
                                if isinstance(row, dict) and row.get("reference_id"):
                                    await record(f"article_{index+1}", session.call_tool("wechat_get_article", {"reference_id": row["reference_id"], "offset": 0, "max_chars": 12000}))
    except Exception as error:
        report["error_type"] = type(error).__name__
        print(json.dumps({"error_type": type(error).__name__}, ensure_ascii=False), flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
