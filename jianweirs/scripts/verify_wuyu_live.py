"""Verify configured endpoints with small requests; prints no credentials or raw replies."""

from __future__ import annotations

import asyncio
import argparse
import base64
import io
import json
import sys
from pathlib import Path
from typing import Literal

import httpx
from dotenv import load_dotenv
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wuyu_harness.config import Settings
from wuyu_harness.data import DataClient, DataClientError
from wuyu_harness.models import ModelClient, ProviderError
from wuyu_harness.policy import PolicyClient
from wuyu_harness.schemas import Record, TaskRequest
from wuyu_harness.store import now


class ConnectivityReply(Record):
    status: Literal["ok"]


async def verify(only: set[str] | None = None) -> dict:
    load_dotenv(ROOT / ".env.wuyu", override=False)
    settings = Settings.from_env()
    result = {"tested_at": now(), "configured": settings.public_status(), "models": {},
              "scope": "真实小请求仅检查协议和连接，不评价遥感识别准确率"}
    previous_path = ROOT / "outputs" / "wuyu-verification.json"
    if only and previous_path.is_file():
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        result.update(models=previous.get("models", {}), data=previous.get("data", {}), metaso=previous.get("metaso", {}))
        result["checked_components"] = sorted(only)
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
        client = ModelClient(http)

        async def probe_model(role: str) -> tuple[str, dict]:
            if only and role not in only:
                return role, result["models"].get(role, {"status": "not_checked"})
            config = settings.models[role]
            if not config.ready:
                return role, {"status": "not_configured", "model": config.model}
            images = None
            if role == "recognizer":
                stream = io.BytesIO()
                Image.new("RGB", (64, 64), (100, 100, 100)).save(stream, "PNG")
                images = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()}}]
            try:
                if role == "recognizer" and settings.rs_output_format == "xml":
                    from wuyu_harness.rs_xml import NATIVE_XML_PROMPT, parse_rs_xml
                    raw, trace = await client.complete_native(config, NATIVE_XML_PROMPT, {"purpose": "接口测试：人工纯色图，不是遥感影像"}, images)
                    (ROOT / "outputs" / "wuyu-rs-latest.xml").write_text(raw.replace(config.api_key, "[REDACTED]"), encoding="utf-8")
                    parsed = parse_rs_xml(raw, asset_id="connectivity-test", width=64, height=64)
                    return role, {"status": "passed", "model": config.model, "output_format": "native_xml", "decision": parsed.native["decision"], "usage": trace.get("usage", {})}
                output, trace = await client.complete(config, "仅做服务连通性测试，附图如有则是人工生成的纯色测试图，无任何监管内容。请返回 JSON 对象 {\"status\":\"ok\"}，不要分析图像。", {}, ConnectivityReply, images)
                return role, {"status": "passed", "model": config.model, "json_output": output.status, "usage": trace.get("usage", {})}
            except (ProviderError, ValueError) as error:
                return role, {"status": "failed", "model": config.model, "error": str(error)}

        async def probe_data() -> dict:
            if only and "data" not in only:
                return result.get("data", {"status": "not_checked"})
            try:
                async with DataClient() as data_client:
                    catalog = await data_client.catalog()
                return {"status": "partial" if catalog["errors"] else "passed", "mode": catalog["mode"],
                        "source_counts": {group: len(value.get("sources", [])) for group, value in catalog["groups"].items()}, "errors": catalog["errors"]}
            except DataClientError as error:
                return {"status": "failed", "code": error.code}

        async def probe_search() -> dict:
            if only and "metaso" not in only:
                return result.get("metaso", {"status": "not_checked"})
            task = TaskRequest(objective="接口联调：固废遥感核查政策检索", task_types=["environmental_context"])
            search = await PolicyClient(http, settings.metaso_key, settings.metaso_base_url, max_queries=1).search(task)
            return {"status": "not_configured" if not settings.metaso_key else "failed" if search["errors"] else "passed",
                    "query_count": 0 if not settings.metaso_key else 1, "source_count": len(search["sources"]), "errors": search["errors"],
                    "note": "仅检查一次真实检索，不是完整政策研究"}

        replies = await asyncio.gather(*(probe_model(role) for role in ("main", "recognizer", "reasoner")), probe_data(), probe_search())
        for role, reply in replies[:3]:
            result["models"][role] = reply
        result["data"] = replies[3]
        result["metaso"] = replies[4]
    path = ROOT / "outputs" / "wuyu-verification.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="Comma-separated main,recognizer,reasoner,data,metaso")
    options = parser.parse_args()
    selected = set(options.only.split(",")) if options.only else None
    print(json.dumps(asyncio.run(verify(selected)), ensure_ascii=False, indent=2))
