"""Retry specialist review on saved evidence without repeating acquisition or search."""

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from wuyu_harness.config import Settings
from wuyu_harness.evidence import reject_unsupported_claims
from wuyu_harness.models import AUDIT_DIR
from wuyu_harness.pipeline import SpecialistReview
from wuyu_harness.report import render_report
from wuyu_harness.schemas import ReportNarrative
from wuyu_harness.service import Service
from wuyu_harness.store import now


async def refine(folder: Path, run_id: str, summary_prefix: str = "wuyu-e2e") -> dict:
    if not summary_prefix.replace("-", "").replace("_", "").isalnum():
        raise ValueError("summary_prefix只能包含字母、数字、下划线或连字符")
    load_dotenv(ROOT / ".env.wuyu", override=False)
    from dataclasses import replace
    service = await Service(replace(Settings.from_env(), output_dir=folder.resolve())).start()
    token = None
    try:
        record = service.store.get(run_id, "run")
        if record["status"] != "succeeded":
            raise ValueError("仅可复核已保存完整证据包的任务")
        run_folder = service.store.run_dir(run_id)
        original = (run_folder / "finding-package.json").read_bytes()
        package = json.loads(original)
        token = AUDIT_DIR.set(run_folder / "model-calls")
        evidence = {"run_id": run_id, "task": package["task"], "recognition": package["recognition"],
                    "native_model_reports": package.get("native_rs_results", []), "quality": package["quality"], "policy": package["policy"]}
        review, review_trace = await service.models.complete(service.settings.models["reasoner"],
            "简要复核已有证据：assessment最多500个中文字；conflicts最多4条，verification最多5条，每条最多80字。只指出实质冲突、具体数据缺口及核查事项，已提供的观测不要逐条复述，不讨论模型分工。不得新增直接观测、定量变化或法定结论。", evidence, SpecialistReview)
        reject_unsupported_claims(review.model_dump_json())
        specialist = {"status": "completed", **review.model_dump()}
        narrative, narrative_trace = await service.models.complete(service.settings.models["main"],
            "根据用户目标、见微证据及深思复核撰写中文摘要和建议。摘要不超过1200字。不得新增影像事实或测量；只引用提供的政策ID，并说明法规现行性与适用性待核验。", {**evidence, "specialist_review": specialist}, ReportNarrative)
        reject_unsupported_claims(narrative.model_dump_json())
        if not set(narrative.cited_policy_ids) <= {item["id"] for item in package["policy"]["sources"]}:
            raise ValueError("模型引用不存在的政策来源")
        revision_id = uuid.uuid4().hex
        revisions = run_folder / "revisions"
        revisions.mkdir(exist_ok=True)
        (revisions / f"{revision_id}-finding-package.json").write_bytes(original)
        package.update(specialist_review=specialist, narrative=narrative.model_dump(), narrative_status="completed",
            refined_at=now(), previous_package_sha256=hashlib.sha256(original).hexdigest())
        package["model_calls"].extend([review_trace, narrative_trace])
        package["warnings"] = [value for value in package["warnings"] if value != "深思复核失败，报告保留未复核标志"]
        render_report(package, service.store, run_id)
        service.pipeline.event(run_id, "report_refinement", "completed", {"revision_id": revision_id})
        result = {"run_id": run_id, "status": "passed", "result_status": package["status"],
            "roles_executed": sorted({item["role"] for item in package["model_calls"]}),
            "policy_sources": len(package["policy"]["sources"]), "report_path": str(run_folder / "report.html"),
            "note": "复用已保存影像、选星、识别和政策证据，仅重试专业复核与最终综合；旧证据包保存在revisions"}
        (ROOT / "outputs" / f"{summary_prefix}-refined.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        summary_path = ROOT / "outputs" / f"{summary_prefix}-verification.json"
        if summary_path.is_file():
            (revisions / f"{revision_id}-execution-summary.json").write_bytes(summary_path.read_bytes())
        summary = {**result, "status": "succeeded", "assets": len(package["assets"]),
            "recognition_status": package["recognition_status"], "narrative_status": package["narrative_status"],
            "specialist_review_status": "completed", "policy_status": package["policy"]["status"],
            "review_refined": True, "warnings": package["warnings"], "scope": "真实接口联调样例，复用保存证据；不评价遥感识别准确率"}
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    finally:
        if token is not None:
            AUDIT_DIR.reset(token)
        await service.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "wuyu-e2e")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--summary-prefix", default="wuyu-e2e")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(refine(args.output_dir, args.run_id, args.summary_prefix)), ensure_ascii=False, indent=2))
