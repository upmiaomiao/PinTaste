"""Persisted complaint scans, ordinary pipeline jobs, server-derived evidence."""

from __future__ import annotations

import asyncio
import json
import uuid

from .complaints import _valid_bbox, plan_scan, plan_window_tasks, scan_status
from .presentation import same_point_eligible, spatial_state
from .schemas import ComplaintScan, TaskRequest
from .store import now


def submit_scan(service, base_task: TaskRequest | dict, spec: ComplaintScan | dict) -> dict:
    """Submit only a bounded, explicitly confirmed area; never a whole city."""
    base_task = base_task if isinstance(base_task, TaskRequest) else TaskRequest.model_validate(base_task)
    plan = plan_scan(spec)
    if plan["status"] != "ready":
        return {"status": plan["status"], "plan": plan, "submitted": False}
    tasks = plan_window_tasks(base_task, plan)
    scan_id = uuid.uuid4().hex
    record = {"id": scan_id, "plan": plan, "base_task": base_task.model_dump(mode="json"),
              "created_at": now(), "updated_at": now(), "status": "running", "window_runs": {}}
    service.store.put("complaint_scan", record)
    dispatchers = getattr(service, "_complaint_dispatchers", None)
    if dispatchers is None:
        dispatchers = service._complaint_dispatchers = {}
    dispatchers[scan_id] = asyncio.create_task(_dispatch_scan(service, scan_id, tasks))
    dispatchers[scan_id].add_done_callback(lambda future: dispatchers.pop(scan_id, None))
    return {"scan_id": scan_id, "status": "queued", "submitted": True,
            "window_count": len(tasks), "plan": plan, "window_runs": record["window_runs"],
            "next_tool": "wuyu_scan_status"}


async def _dispatch_scan(service, scan_id, tasks):
    """Admit at most two windows per scan; respect the pipeline's global queue."""
    record = service.store.get(scan_id, "complaint_scan")
    active = set()
    try:
        for frame, task in zip(record["plan"]["windows"], tasks):
            while True:
                active = {run_id for run_id in active if run_id in service.pipeline.pending}
                if len(active) < 2 and len(service.pipeline.pending) < 12:
                    break
                pending = [service.pipeline.pending[run_id] for run_id in active if run_id in service.pipeline.pending]
                if not pending:
                    pending = list(service.pipeline.pending.values())[:12]
                if pending:
                    await asyncio.wait(pending, timeout=1, return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(.05)
            run = service.pipeline.submit(task)
            active.add(run["id"])
            record["window_runs"][frame["window_id"]] = run["id"]
            record["updated_at"] = now()
            service.store.put("complaint_scan", record)
        futures = [service.pipeline.pending[run_id] for run_id in active if run_id in service.pipeline.pending]
        if futures:
            await asyncio.gather(*futures, return_exceptions=True)
        record.update(status="finished", updated_at=now())
    except asyncio.CancelledError:
        record.update(status="interrupted", updated_at=now())
        raise
    except Exception as error:
        record.update(status="partially_submitted", error=type(error).__name__, updated_at=now())
    finally:
        service.store.put("complaint_scan", record)


async def close_scan_dispatchers(service):
    tasks = list(getattr(service, "_complaint_dispatchers", {}).values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def cancel_scan(service, scan_id: str) -> dict:
    """Stop admitting new windows before cancelling the already admitted ones."""
    record = service.store.get(scan_id, "complaint_scan")
    dispatcher = getattr(service, "_complaint_dispatchers", {}).get(scan_id)
    if dispatcher is not None and not dispatcher.done():
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)
    record = service.store.get(scan_id, "complaint_scan")
    run_ids = list(record.get("window_runs", {}).values())
    futures = [service.pipeline.pending[run_id] for run_id in run_ids if run_id in service.pipeline.pending]
    for future in futures:
        future.cancel()
    if futures:
        await asyncio.gather(*futures, return_exceptions=True)
    for run_id in run_ids:
        run = service.store.get(run_id, "run")
        if run.get("status") in {"queued", "running"}:
            run.update(status="interrupted", error="用户停止区域扫描；未继续提交剩余窗口", updated_at=now())
            service.store.put("run", run)
    record.update(status="cancelled", cancel_requested=True, updated_at=now())
    service.store.put("complaint_scan", record)
    return {"scan_id": scan_id, "status": "cancelled", "submitted_windows": len(run_ids),
            "planned_windows": len(record["plan"]["windows"]), "new_windows_will_start": False}


def ledger_from_runs(service, plan: dict, window_runs: dict[str, str]) -> list[dict]:
    """Do not trust client-provided completion, geometry, dates or predictions."""
    if not isinstance(window_runs, dict) or len(window_runs) > 256:
        raise ValueError("扫描任务映射无效或超过窗口预算")
    frames = {frame["window_id"]: frame for frame in plan.get("windows", [])}
    ledger = []
    for window_id, run_id in window_runs.items():
        if window_id not in frames:
            raise ValueError("任务映射引用不属于本次计划的窗口")
        frame = frames[window_id]
        record = service.store.get(run_id, "run")
        task = record.get("task") or record.get("request") or {}
        run_bbox = _valid_bbox(task.get("bbox"))
        if run_bbox is None or any(abs(a - b) > 1e-7 for a, b in zip(frame["bbox"], run_bbox)):
            ledger.append({"window_id": window_id, "run_id": run_id, "source": "unknown",
                           "status": "task_region_mismatch", "requested_bbox": frame["bbox"]})
            continue
        path = service.store.run_dir(run_id) / "finding-package.json"
        if record.get("status") != "succeeded" or not path.is_file():
            ledger.append({"window_id": window_id, "run_id": run_id,
                           "source": task.get("optical_source") or "unresolved_source",
                           "status": record.get("status", "unknown"), "requested_bbox": frame["bbox"]})
            continue
        if path.stat().st_size > 32_000_000:
            raise ValueError("单窗证据包超过读取上限")
        package = json.loads(path.read_text(encoding="utf-8"))
        if package.get("run_id") != run_id:
            raise ValueError("证据包与任务编号不一致")
        assessments = {row.get("asset_id"): row for row in package.get("image_assessments", [])}
        assets = package.get("assets", [])
        before = len(ledger)
        for listed in assets:
            if not listed.get("id"):
                continue
            asset = service.store.get(listed["id"], "image")
            meta, api = asset.get("metadata") or {}, asset.get("data_api_metadata") or {}
            if meta.get("modality", "optical") != "optical" or asset.get("derived_from_asset_id") or asset.get("science_preview"):
                continue
            assessed = assessments.get(asset["id"]) or {}
            reg = spatial_state(asset)
            actual = _valid_bbox(api.get("actual_bbox_wgs84"))
            if actual is None and reg.get("strict_same_point_eligible") is True:
                actual = _valid_bbox((reg.get("reference_extent") or {}).get("actual_bbox_wgs84"))
            # A returned actual extent without validated spatial support still
            # cannot establish same-point evidence. Relative registration with
            # clipping/transparent margins cannot credit the entire rectangle.
            valid_mask = api.get("full_requested_bbox_covered", reg.get("full_requested_bbox_covered"))
            footprint_verified = bool(actual and same_point_eligible(asset) and valid_mask is not False)
            analyzed = ("jianwei_raw_decision" in assessed and not assessed.get("error")
                        and assessed.get("reason") not in {"recognizer_not_configured", "native_pixels_insufficient_for_target"})
            review = assessed.get("optical_false_positive_review") or {}
            quality = (assessed.get("quality_passed") is True and not assessed.get("quality_issues")
                       and review.get("image_quality_sufficient") is not False)
            scope = api.get("actual_date_scope", api.get("metadata_scope", meta.get("metadata_scope")))
            dates = api.get("actual_dates") or ([api["actual_date"]] if api.get("actual_date") else [])
            if not dates and meta.get("acquisition_time"):
                dates = [meta["acquisition_time"]]
            capture_verified = (scope in {"whole_scene", "full_scene"}
                                and api.get("actual_date_method") not in {"visible_date_ocr", "ocr", "edition_year"})
            ledger.append({"window_id": window_id, "run_id": run_id, "asset_id": asset["id"],
                           "source": meta.get("source") or "unknown", "status": "succeeded",
                           "requested_bbox": frame["bbox"], "footprint_bbox": actual,
                           "downloaded": service.store.asset_path(asset).is_file(),
                           "spatial_verified": footprint_verified, "quality_passed": quality, "analyzed": analyzed,
                           "negative_review_complete": analyzed and assessed.get("decision") == "negative" and assessed.get("target_relevant") is True,
                           "reviewable_finding": (assessed.get("decision") == "positive" or assessed.get("image_only_decision") == "positive") and assessed.get("candidate_count", 0) > 0,
                           "actual_dates": dates, "actual_date_scope": "full_footprint" if capture_verified else "unknown",
                           "evidence_sha256": asset.get("sha256"), "model_decision": assessed.get("decision", "unknown")})
        if len(ledger) == before:
            ledger.append({"window_id": window_id, "run_id": run_id, "source": "unresolved_source",
                           "status": "no_optical_evidence", "requested_bbox": frame["bbox"]})
    return ledger


def get_scan_status(service, scan_id: str) -> dict:
    record = service.store.get(scan_id, "complaint_scan")
    base = record.get("base_task") or {}
    ledger = ledger_from_runs(service, record["plan"], record.get("window_runs", {}))
    summary = scan_status(record["plan"], ledger, base.get("date_start"), base.get("date_end"), trusted=True)
    states = []
    for run_id in record.get("window_runs", {}).values():
        states.append(service.store.get(run_id, "run").get("status"))
    dispatcher = getattr(service, "_complaint_dispatchers", {}).get(scan_id)
    running = (dispatcher is not None and not dispatcher.done()) or any(state in {"queued", "running"} for state in states)
    if record.get("cancel_requested"):
        record_state = "cancelled"
    elif running:
        record_state = "running"
    elif len(record.get("window_runs", {})) < len(record["plan"]["windows"]):
        record_state = "interrupted"
    else:
        record_state = "finished"
    record.update(summary=summary, updated_at=now(), status=record_state)
    service.store.put("complaint_scan", record)
    return {"scan_id": scan_id, "status": record["status"], "summary": summary,
            "window_runs": record.get("window_runs", {}), "next_tool": "wuyu_scan_status" if running else None}
