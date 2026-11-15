"""Bounded live adapter check; read credentials privately and publish no keys."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
from wuyu_harness.unified_imagery import UnifiedImageryClient
from wuyu_harness.data import DataClientError
from omap_api.image_quality import validate_optical_preview


async def main():
    folder=Path("outputs/unified-image-integration")
    folder.mkdir(parents=True,exist_ok=True)
    private=dotenv_values(".local/unified-access.env")
    config={"enabled":True,"base_url":private.get("WUYU_UNIFIED_BASE_URL", ""),
            "api_key":private.get("WUYU_UNIFIED_API_KEY", ""),"timeout_seconds":120,"max_concurrency":2}
    client=UnifiedImageryClient(config)
    report={"checked_at":datetime.now(timezone.utc).isoformat(),"coordinate":[104.06,30.67],
            "scope":"representative point only; catalogue and date-list success do not prove image delivery",
            "checks":[],"secrets_included":False}
    try:
        catalog=await client.catalog()
        report["sources"]=catalog["sources"]
        dates=await client.times({"source":"history246","lon":104.06,"lat":30.67,"zoom":18})
        report["history_catalog"]=dates
        requests=[{"source":"gaofen2026"},{"source":"century","year":2024}]
        if dates["dates"]:
            requests.append({"source":"history246","date":dates["dates"][-1]})
        for request in requests:
            try:
                result=await client.image({**request,"lon":104.06,"lat":30.67,"zoom":18,"width":512,"height":512})
                content,media=client.download(result["image_url"])
                quality=validate_optical_preview(content)
                name=request["source"]+("-"+str(request["year"]) if request.get("year") else "")+".png"
                (folder/name).write_bytes(content)
                report["checks"].append({"request":request,"http_png_received":True,"bytes":len(content),"media_type":media,
                    "preview_quality":quality,"metadata":result["metadata"],"artifact":name})
            except DataClientError as error:
                report["checks"].append({"request":request,"http_png_received":False,"code":error.code,"status_code":error.status_code})
        report["status"]="passed" if all(c["http_png_received"] for c in report["checks"]) else "partial"
    finally:
        await client.aclose()
        target=folder/"verification-20261025.json"
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps({"report":str(target),"status":report.get("status","failed"),
            "checks":[{"request":check["request"],"received":check["http_png_received"],"code":check.get("code")} for check in report["checks"]]}))


if __name__=="__main__":
    asyncio.run(main())
