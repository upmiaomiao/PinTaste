"""Reparse saved native answers and refresh dependent reviews, without reacquisition."""
import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from scripts.replay_portal_evidence import replay_native_evidence
from wuyu_harness.adaptive import image_parts
from wuyu_harness.business_narrative import REPORT_INSTRUCTION, narrative_material, final_summary
from wuyu_harness.config import Settings
from wuyu_harness.evidence import reject_unsupported_claims
from wuyu_harness.fusion import fuse_findings
from wuyu_harness.models import AUDIT_DIR
from wuyu_harness.pipeline import SpecialistReview, VisualCrossReview
from wuyu_harness.report import render_report
from wuyu_harness.schemas import TaskRequest, ReportNarrative
from wuyu_harness.service import Service
from wuyu_harness.store import now


async def refine(output_dir, run_id):
    load_dotenv(ROOT / ".env.wuyu", override=False)
    service = await Service(replace(Settings.from_env(), output_dir=output_dir)).start()
    token = None
    try:
        record = service.store.get(run_id, "run")
        if record["status"] != "succeeded":
            raise ValueError("Only completed runs can be refined")
        folder = service.store.run_dir(run_id)
        original = (folder / "finding-package.json").read_bytes()
        package = replay_native_evidence(json.loads(original), folder, service.store)
        task = TaskRequest.model_validate(package["task"])
        assets = [service.store.get(item["id"], "image") for item in package["assets"]]
        documents = [service.store.get(identity, "document") for identity in task.document_ids]
        token = AUDIT_DIR.set(folder / "model-calls")
        print("Saved native answers reparsed; refreshing visual cross-review.", flush=True)
        visual, trace = await service.models.complete(service.settings.models["main"],
            "依据当前图像和已校验的候选做视觉交叉复核。assessment不超过350中文字，直接说明同一目标的对齐依据及屋顶、裸土、正常施工等主要混淆；"
            "摘要用图1、图2等编号，不写长ID、字段名、流程和模型分工。limitations最多3条。"
            "evidence_asset_ids引用原ID。只有可见同名道路/建筑/地形证据支持同目标时same_target_supported=true，"
            "只有确实讨论主要混淆时alternative_explanations_reviewed=true。裁图不是独立来源；保留正向疑点。不得编造测量或现场成分。",
            {"task": package["task"], "recognition": package["recognition"],
             "assets": [{"图号": f"图{i + 1}", **service.store.public_asset(a)} for i, a in enumerate(assets)]},
            VisualCrossReview, image_parts(service.pipeline, assets))
        if not set(visual.evidence_asset_ids) <= {a["id"] for a in assets}:
            raise ValueError("Unknown visual reference")
        reject_unsupported_claims(visual.model_dump_json())
        package["model_calls"].append(trace)
        package["adaptive_review"]["main_visual_review"] = {"status": "completed", **visual.model_dump(), "can_override_positive_candidate": False}
        package["fusion"] = fuse_findings(task, assets, package["image_assessments"], package["strategy"], package["adaptive_review"]["main_visual_review"])
        material = narrative_material(task, package["fusion"], assets, package["recognition"], package["native_rs_results"],
            package["image_assessments"], package["adaptive_review"], {}, package["scientific_evidence"], package["policy"], documents)
        print("Visual review complete; refreshing specialist review.", flush=True)
        specialist, trace = await service.models.complete(service.settings.models["reasoner"],
            "复核所给证据。assessment不超过300中文字，直接说明支持程度、具体冲突与缺口，不复述流程或字段。"
            "conflicts最多3条，verification最多4条，每条60字内。图像用给定图号引用。不得新增观测、测量或法定结论。",
            material, SpecialistReview)
        reject_unsupported_claims(specialist.model_dump_json())
        package["specialist_review"] = {"status": "completed", **specialist.model_dump()}
        package["model_calls"].append(trace)
        print("Specialist review complete; writing concise report.", flush=True)
        narrative, trace = await service.models.complete(service.settings.models["main"], REPORT_INSTRUCTION,
            narrative_material(task, package["fusion"], assets, package["recognition"], package["native_rs_results"],
                package["image_assessments"], package["adaptive_review"], package["specialist_review"],
                package["scientific_evidence"], package["policy"], documents), ReportNarrative, image_parts(service.pipeline, assets))
        reject_unsupported_claims(narrative.model_dump_json())
        if not set(narrative.cited_policy_ids) <= {item["id"] for item in package["policy"]["sources"]}:
            raise ValueError("Unknown policy reference")
        package.update(narrative=narrative.model_dump(), narrative_status="completed", refined_at=now(),
                       previous_package_sha256=hashlib.sha256(original).hexdigest())
        package["narrative"]["summary"] = final_summary(package["narrative"]["summary"], package["fusion"]["judgment"])
        package["model_calls"].append(trace)
        revision_id = uuid.uuid4().hex
        (folder / "revisions").mkdir(exist_ok=True)
        (folder / "revisions" / f"{revision_id}-finding-package.json").write_bytes(original)
        for phase in package["workflow_stages"]:
            if phase["id"] == "synthesis":
                phase.update(status="completed", narrative_status="completed")
            elif phase["id"] == "recognition_review":
                phase.update(fusion=package["fusion"], review=package["adaptive_review"])
        render_report(package, service.store, run_id)
        service.pipeline.event(run_id, "report_refinement", "completed", {"revision_id": revision_id})
        result = {"run_id": run_id, "status": "passed", "refined_at": package["refined_at"],
            "image_count": len(assets), "native_answer_count": len(package["native_rs_results"]),
            "image_decisions": [a["decision"] for a in package["image_assessments"]],
            "fusion": package["fusion"], "narrative": package["narrative"], "report": str(folder / "report.html")}
        (ROOT / "outputs/wuyu-portal-refined-verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "passed", "native_answers": result["native_answer_count"], "judgment": package["fusion"]["judgment"]}, ensure_ascii=False), flush=True)
    finally:
        if token is not None:
            AUDIT_DIR.reset(token)
        await service.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    asyncio.run(refine(args.output_dir.resolve(), args.run_id))
