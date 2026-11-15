"""One labelled acceptance task against the running, locally owned portal.

Uses a short-lived local test session. No passwords or API keys are changed.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from wuyu_harness.auth_portal import COOKIE, PortalDB
from wuyu_harness.store import Store


def main():
    db = PortalDB(ROOT / "outputs/portal")
    admins = [u for u in db.users() if u["role"] == "admin" and u["active"]]
    if len(admins) != 1:
        print("ONE_EXISTING_ADMIN_REQUIRED")
        return 2
    token, csrf = db.issue_session(admins[0]["id"])
    out = ROOT / "outputs/demand-post-upgrade-20261022.json"
    question = (
        "验收测试（图源升级后）：WGS84坐标104.461117,30.858473附近有没有疑似小堆体？"
        "请使用Bing高分底图查看，允许ArcGIS只作为位置配准参考。"
        "不调用其他高分图源、多光谱或SAR；只描述影像可见证据和不确定性，不能确定物料种类。"
    )
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "question": question,
              "system_url": "http://127.0.0.1:3080/", "passed": False}
    try:
        with httpx.Client(base_url=report["system_url"], trust_env=False, follow_redirects=False,
                          timeout=30, cookies={COOKIE: token},
                          headers={"Origin": report["system_url"].rstrip("/"), "X-CSRF-Token": csrf}) as client:
            response = client.post("/portal/api/tasks", json={
                "objective": question, "center": [104.461117, 30.858473],
                "coordinate_system": "wgs84", "task_types": ["suspected_site_discovery"],
                "search_policy": False, "consult_shensi": False,
            })
            if response.status_code != 202:
                raise RuntimeError("task_submission_failed")
            task_id = response.json()["id"]
            report["task_id"] = task_id
            print("LIVE_UPGRADE_TASK " + task_id, flush=True)
            deadline = time.monotonic() + 720
            last = None
            while time.monotonic() < deadline:
                response = client.get("/portal/api/tasks/" + task_id)
                if response.status_code != 200:
                    raise RuntimeError("task_poll_failed")
                view = response.json()
                state = view["status"]
                if state != last:
                    print("LIVE_UPGRADE_STATE " + state, flush=True)
                    last = state
                if state not in {"planning", "queued", "running"}:
                    break
                time.sleep(3)
            else:
                raise RuntimeError("task_deadline_reached")
            images = view.get("images", [])
            store = Store(ROOT / "outputs/portal/engine")
            alignment = []
            for im in images:
                raw = store.get(im["id"], "image")
                alignment.append({"asset_id": im["id"], "source": im.get("metadata", {}).get("source"),
                                  "name": im.get("name"),
                                  "spatial_registration": raw.get("data_api_metadata", {}).get("spatial_registration", {}),
                                  "analysis_scope": im.get("analysis_scope")})
            selected = [s["source"] for s in (view.get("selection") or {}).get("ranking", [])]
            report.update(status=state, summary=view.get("summary"), error=view.get("error"),
                          selected_sources=selected, scientific_evidence=view.get("scientific_evidence"),
                          images=alignment, fusion=view.get("fusion"),
                          image_assessments=view.get("image_assessments"))
            eligible = {im['asset_id'] for im in alignment
                        if im['spatial_registration'].get('strict_same_point_eligible') is True}
            fusion = view.get('fusion') or {}
            point_evidence = set(fusion.get('positive_asset_ids', []) + fusion.get('negative_asset_ids', []))
            report['spatial_gate_respected'] = point_evidence <= eligible
            report["passed"] = (state == "succeeded" and bool(view.get("summary")) and
                                bool(images) and "bing" in selected and
                                set(selected) <= {"arcgis", "bing"} and
                                {im["source"] for im in alignment} <= {"arcgis", "bing"} and
                                report['spatial_gate_respected'] and
                                not view.get("scientific_evidence"))
    except Exception:
        report["verification_error"] = "local_task_acceptance_incomplete"
    finally:
        db.logout(token)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("LIVE_UPGRADE_ACCEPTANCE " + ("PASS" if report["passed"] else "FAIL"), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
