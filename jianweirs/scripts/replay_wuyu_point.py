"""Replay the user's point with current routing; save evidence without credentials."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import time

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from wuyu_harness.config import Settings
from wuyu_harness.service import Service
from wuyu_harness.schemas import TaskRequest


async def main():
    load_dotenv(PROJECT / ".env.wuyu", override=False)
    settings = replace(Settings.from_env(), output_dir=PROJECT / "outputs/chengdu-point-replay")
    service = await Service(settings).start()
    started = time.monotonic()
    try:
        task = TaskRequest(objective="复核104.504329、30.507354附近当前可见影像是否存在疑似堆存，给出原图、像素证据与竞争解释。在线底图拍摄日期未证实时不可称为今天现场状态。",
            task_types=["waste_type_screening", "environmental_context"], center=(104.504329, 30.507354),
            coordinate_system="unknown", acquire_optical=True, search_policy=False, consult_shensi=True,
            jurisdiction="四川省成都市")
        record = service.pipeline.submit(task)
        print(json.dumps({"run_id": record["id"], "status": "started"}), flush=True)
        await service.pipeline.pending[record["id"]]
        result = service.store.get(record["id"], "run")
        summary = {"run_id": record["id"], "status": result["status"], "elapsed_seconds": round(time.monotonic() - started, 2),
                   "event_count": len(result.get("events", [])), "error": result.get("error")}
        folder = service.store.run_dir(record["id"])
        if result["status"] == "succeeded":
            package = json.loads((folder / "finding-package.json").read_text(encoding="utf-8"))
            summary.update(selection_method=package["selection"]["method"], image_count=len(package["assets"]),
                sources=[a["metadata"]["source"] for a in package["assets"]], roles=[a["metadata"]["role"] for a in package["assets"]],
                model_calls=[{"role": c["role"], "image_input_count": c.get("image_input_count"), "usage": c.get("usage")} for c in package["model_calls"]],
                recognition_status=package["recognition_status"], narrative_status=package["narrative_status"],
                specialist_status=package["specialist_review"]["status"], warnings=package["warnings"],
                report=str(folder / "report.html"))
        target = PROJECT / "outputs/wuyu-point-replay-check.json"
        target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    finally:
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
