"""Run one labeled live integration example using public ArcGIS imagery and real roles."""

import asyncio
import argparse
import copy
import json
import sys
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wuyu_harness.config import Settings
from wuyu_harness.schemas import TaskRequest
from wuyu_harness.service import Service


async def main(image: Path | None = None, policy_package: Path | None = None) -> dict:
    load_dotenv(ROOT / ".env.wuyu", override=False)
    settings = replace(Settings.from_env(), output_dir=ROOT / "outputs" / "wuyu-e2e")
    service = await Service(settings).start()
    try:
        request = TaskRequest(objective="系统接口联调示例：检查北京公开影像，描述可见场景及可能混淆因素。本任务仅用于验证系统链路，不作为实际监管疑点或案件认定。",
            task_types=["waste_type_screening", "environmental_context"],
            bbox=[116.446, 39.849, 116.456, 39.859], jurisdiction="北京市", acquire_optical=True,
            optical_source="arcgis", search_policy=True, consult_shensi=True)
        if image:
            metadata_path = image.with_suffix(".json")
            source_metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
            asset = service.import_file(str(image.resolve()), {"source": "existing_local_api_capture", "role": "current"})
            raw_asset = service.store.get(asset["id"], "image")
            raw_asset["data_api_metadata"] = source_metadata
            service.store.put("image", raw_asset)
            request = TaskRequest.model_validate({**request.model_dump(mode="json"), "acquire_optical": False,
                                                  "asset_ids": [asset["id"]], "bbox": source_metadata.get("request_bounds")})
        if policy_package:
            saved = json.loads(policy_package.read_text(encoding="utf-8"))
            if saved["task"]["jurisdiction"] != request.jurisdiction or saved["task"]["objective"] != request.objective:
                raise ValueError("仅能复用同辖区、同专题的联调政策检索")
            async def cached_search(task):
                policy = copy.deepcopy(saved["policy"])
                policy["reused_from_run"] = saved["run_id"]
                policy["note"] = "复用同日同专题真实检索结果，未重复消费秘塔请求"
                return policy
            service.policy.search = cached_search
        record = service.pipeline.submit(request)
        print(json.dumps({"run_id": record["id"], "status": "started", "note": "真实服务联调示例"}, ensure_ascii=False), flush=True)
        pending = service.pipeline.pending[record["id"]]
        last_step = None
        while not pending.done():
            done, _ = await asyncio.wait({pending}, timeout=10)
            current = service.store.get(record["id"], "run")
            step = current["events"][-1] if current["events"] else None
            if step != last_step:
                print(json.dumps({"run_id": record["id"], "latest_event": step}, ensure_ascii=False), flush=True)
                last_step = step
        result = service.store.get(record["id"], "run")
        if result["status"] == "succeeded":
            folder = service.store.run_dir(record["id"])
            package = json.loads((folder / "finding-package.json").read_text(encoding="utf-8"))
            summary = {"run_id": record["id"], "status": result["status"], "result_status": result["result_status"],
                "assets": len(package["assets"]), "model_calls": [{"role": c["role"], "model": c["model"]} for c in package["model_calls"]],
                "policy_status": package["policy"]["status"], "policy_sources": len(package["policy"]["sources"]),
                "recognition_status": package["recognition_status"], "narrative_status": package["narrative_status"],
                "specialist_review_status": package["specialist_review"]["status"], "warnings": package["warnings"],
                "report_path": str(folder / "report.html"), "package_path": str(folder / "finding-package.json"),
                "scope": "单个真实链路示例，不是模型准确率或全图源能力评测"}
        else:
            summary = result
        (ROOT / "outputs" / "wuyu-e2e-verification.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary
    finally:
        await service.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", type=Path)
    parser.add_argument("--policy-package", type=Path)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(main(args.image, args.policy_package)), ensure_ascii=False, indent=2))
