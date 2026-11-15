"""Configure authorized search routing and run isolated real portal acceptance."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys
import time

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wuyu_harness.auth_portal import COOKIE, PortalDB
from wuyu_harness.config import Settings
from wuyu_harness.portal_config import PortalConfig

OUT = ROOT / "outputs" / "wechat-integration-20261022"
TEST_FOLDER = OUT / "portal-test"
MCP_URL = "http://152.136.236.199:8077/mcp"


def configured(folder):
    load_dotenv(ROOT / ".env.wuyu", override=False)
    settings = replace(Settings.from_env(), output_dir=folder / "engine")
    db = PortalDB(folder)
    config = PortalConfig(db, settings)
    config.update({"web_search": {"base_url": settings.web_search_base_url, "api_key": settings.web_search_key},
                   "wechat": {"enabled": True, "mcp_url": MCP_URL, "max_results": 5, "max_pages": 3, "read_articles": 2, "timeout": 180}})
    return config


def run(base, only=None):
    password_path = TEST_FOLDER / ".qa-login.json"
    if password_path.exists():
        credentials = json.loads(password_path.read_text(encoding="utf-8"))
    else:
        credentials = {"username": "search_acceptance", "password": secrets.token_urlsafe(28)}
        password_path.write_text(json.dumps(credentials), encoding="utf-8")
    headers = {"Origin": base}
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as client:
        bootstrap = client.get("/portal/api/session").json()["bootstrap_required"]
        response = client.post("/portal/api/bootstrap" if bootstrap else "/portal/api/login", headers=headers, json=credentials)
        response.raise_for_status()
        csrf = response.json()["csrf_token"]
        cookie = client.cookies.get(COOKIE)
        storage = {"cookies": [{"name": COOKIE, "value": cookie, "domain": "127.0.0.1", "path": "/", "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Strict"}], "origins": []}
        (TEST_FOLDER / ".browser-state.json").write_text(json.dumps(storage), encoding="utf-8")
        report = {"tested_at": datetime.now(timezone.utc).isoformat(), "base_url": base, "tests": [],
                  "configuration": client.get("/portal/api/config").json(), "mcp_health": client.get("/portal/api/admin/search-health").json()}
        tests = [
            ("policy_wechat", "请查阅危险废物贮存相关标准和微信公众号资料，说明企业贮存设施与贮存点的区别，并列出出处。"),
            ("scholar_wechat", "请结合污水处理领域的学术论文和微信公众号文章，介绍膜生物反应器MBR的主要优势与限制，附上来源。"),
        ]
        for name, objective in tests:
            if only and name != only:
                continue
            started = time.perf_counter()
            response = client.post("/portal/api/tasks", headers={**headers, "X-CSRF-Token": csrf},
                                   json={"objective": objective, "search_policy": True, "consult_shensi": False})
            response.raise_for_status()
            task_id = response.json()["id"]
            deadline = time.monotonic() + 260
            while time.monotonic() < deadline:
                value = client.get("/portal/api/tasks/" + task_id).json()
                if value["status"] not in {"planning", "queued", "running"}:
                    break
                time.sleep(1)
            policy = value.get("policy") or {}
            snapshots = []
            for source in policy.get("sources", []):
                if source.get("snapshot_url"):
                    page = client.get(source["snapshot_url"])
                    snapshots.append({"source_id": source["id"], "status": page.status_code, "response_chars": len(page.text)})
            sources_by_id = {s["id"]: s for s in policy.get("sources", [])}
            item = {"name": name, "task_id": task_id, "status": value["status"], "result_status": value.get("result_status"),
                    "seconds": round(time.perf_counter()-started, 3), "task_api_url": base + "/portal/api/tasks/" + task_id,
                    "channels": policy.get("channels"), "source_counts": {kind: sum(s.get("source_type") == kind for s in policy.get("sources", [])) for kind in ("webpage", "academic", "wechat")},
                    "cited_source_types": sorted({sources_by_id[i].get("source_type") for i in value.get("narrative", {}).get("cited_policy_ids", []) if i in sources_by_id}),
                    "snapshots": snapshots, "summary": value.get("summary"), "errors": value.get("error"),
                    "source_metadata": [{k: s.get(k) for k in ("id", "source_type", "title", "account", "published_at", "indexed_published_at", "content_status", "content_chars", "metadata_conflicts", "snapshot_url")} for s in policy.get("sources", [])]}
            report["tests"].append(item)
            OUT.mkdir(parents=True, exist_ok=True)
            (OUT / "portal-acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: item[k] for k in ("name", "task_id", "status", "result_status", "seconds", "source_counts", "cited_source_types", "snapshots", "errors")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare-test", "apply-current", "run"])
    parser.add_argument("--base-url", default="http://127.0.0.1:3083")
    parser.add_argument("--only", choices=["policy_wechat", "scholar_wechat"])
    args = parser.parse_args()
    if args.action == "run":
        run(args.base_url, args.only)
    else:
        folder = TEST_FOLDER if args.action == "prepare-test" else ROOT / "outputs" / "portal"
        config = configured(folder)
        public = config.public()
        print(json.dumps({"folder": str(folder), "web_provider": public["web_search"]["provider"],
            "web_configured": public["web_search"]["api_key_configured"], "academic_configured": public["metaso"]["api_key_configured"],
            "wechat_enabled": public["wechat"]["enabled"], "wechat_mcp_url": public["wechat"]["mcp_url"]}, ensure_ascii=False))
