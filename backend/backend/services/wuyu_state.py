"""Persistent workflow contracts; adapters produce artifacts, this module gates them.

No model SDK, network call or deployment is performed by this module.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


class WorkflowError(ValueError):
    """An event conflicts with the current reviewed workflow state."""


DEFAULT_REQUIREMENT_STEPS = ["scenario", "audience", "page_count", "style"]
REVIEW_POLICIES = {"guided", "template_only", "template_and_final"}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise WorkflowError(message)


def _nonempty(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def new_state(project_id: str, mode: str = "guided", review_policy: str | None = None,
              requirement_steps: list[str] | None = None, creation_kind: str = 'deck',
              output_preference: str = 'editable') -> dict:
    require(bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project_id)), "Invalid project ID")
    policy = review_policy or ("guided" if mode == "guided" else "template_only")
    require(mode in {"guided", "batch"}, "Mode must be guided or batch")
    require(policy in REVIEW_POLICIES, "Unknown review policy")
    require((mode == "guided") == (policy == "guided"), "Mode and review policy conflict")
    steps = requirement_steps or DEFAULT_REQUIREMENT_STEPS
    require(len(steps) == len(set(steps)) and "page_count" in steps, "Steps need unique keys and page_count")
    require(all(isinstance(x, str) and x for x in steps), "Invalid requirement steps")
    require(creation_kind in {'deck', 'pages', 'asset'}, 'Unknown creation kind')
    require(output_preference in {'image', 'editable'}, 'Unknown output preference')
    require(creation_kind == 'deck' or policy == 'template_only', 'Focused jobs use explicit generation authorization')
    return {
        "schema_version": 1, "project_id": project_id, "revision": 0,
        "mode": mode, "review_policy": policy,
        "creation_kind": creation_kind, "output_preference": output_preference,
        "requires_final_image_review": True,
        "focused_authorization": None, "focused_delivery": None,
        "requirements": {"steps": list(steps), "values": {}, "revision": 0},
        "template": {"id": None, "revision": 0, "confirmed_revision": None, "samples_revision": 0,
                     "samples": {}, "sample_attempts": {}, "sample_errors": {}},
        "pages": [], "images_confirmation": None, "trace_batch": None,
        "trace_error": None, "qa": None, "qa_run": None, "qa_error": None,
        "export": None, "export_run": None, "export_error": None,
        "applied_events": {}, "used_attempt_ids": [], "used_sample_attempt_ids": [],
        "used_trace_batch_ids": [], "used_qa_run_ids": [], "used_export_run_ids": [], "events": [],
    }


def _page(number: int) -> dict:
    return {"number": number, "input_revision": 0, "image_version": 0,
            "generation_status": "idle", "attempt": None, "error": None,
            "artifact": None, "confirmed_image_version": None,
            "trace_version": 0, "trace": None, "requested_change": None,
            "locked": False, "lock_source": None}


LOCK_SOURCES = {"user_ui", "user_chat", "manual_edit"}


def locked_numbers(s: dict) -> list[int]:
    """Pages whose current image/trace the user asked to keep unchanged."""
    return [p["number"] for p in s.get("pages", []) if p.get("locked") is True]


def _require_unlocked(s: dict, message: str = "Locked pages must be unlocked before every page is regenerated") -> None:
    require(not locked_numbers(s), message)


def _requirements_ready(s: dict) -> bool:
    return all(k in s["requirements"]["values"] for k in s["requirements"]["steps"])


def _template_ready(s: dict) -> bool:
    t = s["template"]
    if s.get('creation_kind', 'deck') != 'deck':
        authorization = s.get('focused_authorization') or {}
        return bool(t['id'] is not None and authorization.get('template_revision') == t['revision']
                    and authorization.get('requirements_revision') == s['requirements']['revision'])
    return t["id"] is not None and t["confirmed_revision"] == t["revision"]


def _current(s: dict, p: dict) -> bool:
    a = p["artifact"]
    return bool(a and p["generation_status"] == "ready"
                and a["input_revision"] == p["input_revision"]
                and a["template_revision"] == s["template"]["revision"]
                and a["requirements_revision"] == s["requirements"]["revision"])


def _image_snapshot(s: dict) -> dict:
    return {str(p["number"]): p["image_version"] for p in s["pages"]}


def _all_current(s: dict) -> bool:
    return _template_ready(s) and bool(s["pages"]) and all(_current(s, p) for p in s["pages"])


def _all_reviewed(s: dict) -> bool:
    c = s["images_confirmation"]
    return bool(_all_current(s) and c and c["image_versions"] == _image_snapshot(s)
                and c["template_revision"] == s["template"]["revision"]
                and (not s.get('requires_final_image_review') or c.get('basis') == 'single_final_user_review'))


def image_export_ready(state: dict) -> bool:
    """Image-only delivery needs current reviewed images, never native traces."""
    return _all_reviewed(state)


def _trace_current(p: dict) -> bool:
    return bool(p["trace"] and p["trace"]["source_image_version"] == p["image_version"])


def _trace_snapshot(s: dict) -> dict:
    return {str(p["number"]): p["trace_version"] for p in s["pages"]}


def _all_traced(s: dict) -> bool:
    return _all_reviewed(s) and not s["trace_batch"] and all(_trace_current(p) for p in s["pages"])


def _qa_current(s: dict) -> bool:
    q = s["qa"]
    return bool(_all_traced(s) and q and q["passed"]
                and q["image_versions"] == _image_snapshot(s)
                and q["trace_versions"] == _trace_snapshot(s))


def phase(s: dict) -> str:
    if not _requirements_ready(s):
        return "requirements"
    focused = s.get('creation_kind', 'deck') != 'deck'
    if focused and not _template_ready(s):
        return 'generation_request'
    chosen_layout = s['template'].get('confirmation_kind') in {'library', 'upload'} and _template_ready(s)
    if not focused and not chosen_layout and (not s["template"]["id"] or s["template"]["sample_attempts"] or set(s["template"]["samples"]) != {"cover", "first_body"}):
        return "samples"
    if not _template_ready(s):
        return "template_review"
    if s["mode"] == "guided":
        next_page = next((p for p in s["pages"] if not _current(s, p)
                          or p["confirmed_image_version"] != p["image_version"]), None)
        if next_page and _current(s, next_page):
            return "page_review"
    if not _all_current(s):
        return "page_generation"
    if not _all_reviewed(s):
        return "final_image_review" if s.get('requires_final_image_review') else "page_review" if s["mode"] == "guided" else "final_image_review"
    if s.get('output_preference') == 'image':
        delivery = s.get('focused_delivery') or {}
        return 'exported' if delivery.get('image_versions') == _image_snapshot(s) else 'ready_to_export'
    if not _all_traced(s):
        return "tracing"
    if not _qa_current(s):
        return "quality_check"
    if s["export_run"]:
        return "exporting"
    return "exported" if s["export"] else "ready_to_export"


def _invalidate_outputs(s: dict, pages: list[dict], stale_images: bool = False) -> None:
    s['focused_delivery'] = None
    s["images_confirmation"] = s["trace_batch"] = s["qa"] = s["export"] = None
    s["qa_run"] = s["export_run"] = None
    s["trace_error"] = s["qa_error"] = s["export_error"] = None
    for p in pages:
        p["confirmed_image_version"] = p["trace"] = None
        if stale_images:
            p["input_revision"] += 1
            p["generation_status"] = "idle"
            p["attempt"] = p["error"] = None


def _refresh_review(s: dict, event_id: str) -> None:
    if not _all_current(s) or s.get('requires_final_image_review'):
        return
    policy = s["review_policy"]
    if policy == "template_only" or (policy == "guided" and all(
            p["confirmed_image_version"] == p["image_version"] for p in s["pages"])):
        s["images_confirmation"] = {
            "policy": policy, "event_id": event_id,
            "basis": ('explicit_focused_generation_request' if s.get('creation_kind', 'deck') != 'deck' else
                      "explicit_batch_mode_preauthorization") if policy == "template_only" else "each_current_page_confirmed",
            "image_versions": _image_snapshot(s), "template_revision": s["template"]["revision"],
        }


def _get_page(s: dict, value: Any) -> dict:
    require(isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= len(s["pages"]), "Unknown page number")
    return s["pages"][value - 1]


def _guided_turn(s: dict, p: dict) -> None:
    if s["mode"] == "guided":
        next_page = next((x for x in s["pages"] if not _current(s, x)
                          or x["confirmed_image_version"] != x["image_version"]), None)
        require(next_page is not None and next_page["number"] == p["number"],
                "Confirm the previous current page before continuing")


def _artifact(s: dict, p: dict, payload: dict) -> dict:
    require(isinstance(payload.get("copy"), str) and payload["copy"].strip(), "Page copy is required")
    require(isinstance(payload.get("image_uri"), str) and payload["image_uri"].strip(), "Generated image URI is required")
    return {"copy": payload["copy"], "image_uri": payload["image_uri"],
            "input_revision": p["input_revision"],
            "template_revision": s["template"]["revision"],
            "requirements_revision": s["requirements"]["revision"]}


def _accept_trace_results(s: dict, batch: dict, artifacts: Any, require_all: bool) -> None:
    require(isinstance(artifacts, list) and all(isinstance(x, dict) for x in artifacts), "Trace pages must be objects")
    require(all(isinstance(x.get("number"), int) and not isinstance(x["number"], bool) for x in artifacts), "Trace page numbers must be integers")
    numbers = [x["number"] for x in artifacts]
    require(len(numbers) == len(set(numbers)), "A page can complete only once per tracing batch")
    require(set(numbers).issubset(batch["pending_pages"]), "Trace result is not a pending page")
    if require_all:
        require(set(numbers) == set(batch["pending_pages"]), "Every remaining page needs a trace artifact")
    for item in artifacts:
        p = _get_page(s, item["number"])
        require(isinstance(item.get("artifact_uri"), str) and item["artifact_uri"], "Trace artifact URI is required")
        require(isinstance(item.get("editable_objects"), list) and item["editable_objects"], "Editable object manifest is required")
        p["trace_version"] += 1
        p["trace"] = {"source_image_version": p["image_version"],
                      "artifact_uri": item["artifact_uri"], "editable_objects": deepcopy(item["editable_objects"])}
        batch["pending_pages"].remove(p["number"])
        batch["completed_pages"].append(p["number"])


def apply_event(state: dict, event: dict, expected_revision: int | None = None) -> dict:
    """Return a new JSON state; rejected events never mutate the caller's state.

    A completed event is idempotent by event_id + payload. Producer callbacks
    must carry the current attempt_id/batch_id to reject stale completions.
    """
    require(isinstance(event, dict), "Event must be an object")
    eid, kind, d = event.get("event_id"), event.get("type"), event.get("payload", {})
    require(isinstance(eid, str) and bool(eid.strip()) and isinstance(kind, str)
            and isinstance(d, dict), "Event needs event_id, type and object payload")
    digest = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()
    if eid in state["applied_events"]:
        require(state["applied_events"][eid] == digest, "Event ID reused with a different payload")
        return deepcopy(state)
    require(expected_revision is None or expected_revision == state["revision"], "State revision conflict")
    s = deepcopy(state)
    t = s["template"]
    if kind == "review_policy.changed":
        policy = d.get("review_policy")
        require(policy in REVIEW_POLICIES, "Unknown review policy")
        require(s.get('creation_kind', 'deck') == 'deck' or policy == 'template_only', 'Focused jobs use explicit generation authorization')
        if policy != s["review_policy"]:
            s["review_policy"] = policy
            s["mode"] = "guided" if policy == "guided" else "batch"
            # A review preference is not a design/content input. Keep the exact
            # current sample pair, page images and traces. Rebind only approval.
            s["images_confirmation"] = None
            if policy != "template_only":
                s["qa"] = s["qa_run"] = s["export"] = s["export_run"] = None
    elif kind == 'output_preference.changed':
        require(d.get('output_preference') in {'image', 'editable'}, 'Unknown output preference')
        s['output_preference'] = d['output_preference']
    elif kind == "requirement.selected":
        r = s["requirements"]
        key = d.get("key")
        require(key in r["steps"] and _nonempty(d.get("value")), "Unknown or empty requirement")
        index = r["steps"].index(key)
        require(all(k in r["values"] for k in r["steps"][:index]), "Requirements must be selected progressively")
        if key == "page_count":
            require(isinstance(d["value"], int) and not isinstance(d["value"], bool)
                    and (2 <= d['value'] <= 80 if s.get('creation_kind', 'deck') == 'deck' else
                         1 <= d['value'] <= (1 if s.get('creation_kind') == 'asset' else 8)),
                    "Page count does not match the creation scope")
        if r["values"].get(key) != d["value"]:
            # A requirement change makes every page stale; locked pages are
            # never silently regenerated (see pages.resized for count changes).
            _require_unlocked(s)
            for later in r["steps"][index + 1:]:
                r["values"].pop(later, None)
            r["values"][key] = deepcopy(d["value"])
            r["revision"] += 1
            t["confirmed_revision"] = None
            t["samples"] = {}
            t["sample_attempts"] = {}
            t["sample_errors"] = {}
            t["samples_revision"] += 1
            _invalidate_outputs(s, s["pages"], stale_images=True)
            count = r["values"].get("page_count")
            if count is not None:
                s["pages"] = s["pages"][:count] + [_page(i) for i in range(len(s["pages"]) + 1, count + 1)]
    elif kind == "template.selected":
        require(_requirements_ready(s), "Finish requirements before producing template samples")
        _require_unlocked(s)
        require(isinstance(d.get("template_id"), str) and d["template_id"].strip(), "Template ID is required")
        t.update(id=d["template_id"], revision=t["revision"] + 1, confirmed_revision=None,
                 samples_revision=0, samples={}, sample_attempts={}, sample_errors={}, confirmation_kind=None)
        _invalidate_outputs(s, s["pages"], stale_images=True)
        s['focused_authorization'] = None
    elif kind == 'template.choice_confirmed':
        require(_requirements_ready(s) and t['id'] is not None, 'Select a template after completing requirements')
        require(d.get('template_revision') == t['revision'], 'Template choice is stale')
        require(d.get('source_kind') in {'library', 'upload'}, 'Choose a library or uploaded template')
        t['confirmed_revision'] = t['revision']
        t['confirmation_kind'] = d['source_kind']
        s['requires_final_image_review'] = True
    elif kind == 'focused.requested':
        require(s.get('creation_kind', 'deck') in {'pages', 'asset'}, 'Only focused jobs accept direct generation')
        require(_requirements_ready(s) and t['id'] is not None, 'Complete requirements and select visual references first')
        require(d.get('generation_requested') is True, 'Explicit generation authorization is required')
        s['focused_authorization'] = {'event_id': eid, 'basis': 'explicit_focused_generation_request',
                                      'template_revision': t['revision'],
                                      'requirements_revision': s['requirements']['revision']}
    elif kind == 'focused.image_exported':
        require(s.get('output_preference') == 'image' and _all_reviewed(s), 'Current reviewed image delivery is required')
        require(isinstance(d.get('artifact_uri'), str) and d['artifact_uri'], 'Image PPTX artifact URI is required')
        s['focused_delivery'] = {'artifact_uri': d['artifact_uri'], 'image_versions': _image_snapshot(s)}
    elif kind == "sample.generate_started":
        require(s.get('creation_kind', 'deck') == 'deck', 'Focused jobs do not generate approval samples')
        require(_requirements_ready(s) and t["id"] is not None, "Select requirements and template first")
        role = d.get("role")
        require(role in {"cover", "first_body"}, "Only cover and first_body samples are accepted")
        require(isinstance(d.get("attempt_id"), str) and d["attempt_id"], "Sample attempt ID is required")
        require(d["attempt_id"] not in s["used_sample_attempt_ids"], "Sample attempt IDs must be unique")
        if _template_ready(s):
            _require_unlocked(s)
            t["confirmed_revision"] = None
            _invalidate_outputs(s, s["pages"], stale_images=True)
        # A deliberate newer request supersedes the role's in-flight attempt;
        # late callbacks from that previous request cannot replace its output.
        t["sample_attempts"][role] = {"id": d["attempt_id"], "template_revision": t["revision"],
                                       "requirements_revision": s["requirements"]["revision"]}
        t["sample_errors"].pop(role, None)
        t["samples_revision"] += 1
        s["used_sample_attempt_ids"].append(d["attempt_id"])
    elif kind in {"sample.generated", "sample.failed"}:
        require(_requirements_ready(s) and t["id"] is not None, "Select requirements and template first")
        role = d.get("role")
        attempt = t["sample_attempts"].get(role)
        require(attempt is not None and attempt["id"] == d.get("attempt_id"), "Stale or missing sample attempt")
        require(d.get("template_revision") == t["revision"], "Stale template sample")
        require(d.get("requirements_revision") == s["requirements"]["revision"], "Sample belongs to old requirements")
        if kind == "sample.failed":
            t["sample_errors"][role] = str(d.get("error", "Sample generation failed"))
        else:
            require(d.get("page_kind") == ("cover" if role == "cover" else "content"),
                    "The first body sample must be a content page, not a table of contents")
            p = s["pages"][0 if role == "cover" else 1]
            t["samples"][role] = _artifact(s, p, d)
            t["samples_revision"] += 1
        del t["sample_attempts"][role]
    elif kind == "template.confirmed":
        require(s.get('creation_kind', 'deck') == 'deck', 'Focused jobs do not fabricate template confirmation')
        require(_requirements_ready(s), "Requirements are incomplete")
        require(not _template_ready(s), "The current template is already confirmed")
        require(d.get("template_revision") == t["revision"], "Template confirmation is stale")
        require(d.get("samples_revision") == t["samples_revision"], "Confirm the current pair of samples")
        require(not t["sample_attempts"], "Wait for current sample generation before confirming")
        require(t["id"] is not None and set(t["samples"]) == {"cover", "first_body"}, "Both current samples are required")
        t["confirmed_revision"] = t["revision"]
        if d.get("reuse_samples", True):
            for number, role in [(1, "cover"), (2, "first_body")]:
                p = s["pages"][number - 1]
                p["artifact"] = _artifact(s, p, t["samples"][role])
                p["image_version"] += 1
                p["generation_status"] = "ready"
                p["attempt"] = p["error"] = None
                p["confirmed_image_version"] = p["image_version"] if s["mode"] == "guided" else None
    elif kind == "page.changed":
        require(_template_ready(s), "Confirm the template before editing final pages")
        p = _get_page(s, d.get("number"))
        require(_nonempty(d.get("change")), "Describe the requested page change")
        require(p.get("locked") is not True, "This page is locked; unlock it before changing it")
        require(d.get("fresh", False) in (True, False), "fresh must be a boolean")
        _invalidate_outputs(s, [p], stale_images=True)
        p["requested_change"] = deepcopy(d["change"])
        if d.get("fresh") is True:
            # The page content was re-planned: render from the new spec and the
            # confirmed template, not as an edit of the superseded image.
            p["fresh_render"] = True
    elif kind == "pages.locked":
        require(_template_ready(s), "Confirm the template before locking pages")
        numbers, locked = d.get("numbers"), d.get("locked")
        require(isinstance(numbers, list) and 1 <= len(numbers) <= 80 and len(set(numbers)) == len(numbers),
                "numbers must be a nonempty list of unique page numbers")
        require(type(locked) is bool, "locked must be a boolean")
        source = d.get("source", "user_ui")
        require(source in LOCK_SOURCES, "Unknown lock source")
        pages = [_get_page(s, number) for number in numbers]
        if locked:
            require(all(_current(s, p) for p in pages), "Only pages with a current generated image can be locked")
        for p in pages:
            p["locked"] = locked
            p["lock_source"] = source if locked else None
    elif kind == "pages.resized":
        require(_template_ready(s), "Confirm the template before changing the page count")
        count = d.get("count")
        require(isinstance(count, int) and not isinstance(count, bool), "count must be an integer")
        require(2 <= count <= 80 if s.get('creation_kind', 'deck') == 'deck' else 1 <= count <= 8,
                "Page count does not match the creation scope")
        removed = s["pages"][count:]
        require(all(p.get("locked") is not True for p in removed), "A locked page cannot be removed; unlock it first")
        # Only the overall delivery approval is revoked. Kept pages retain
        # their images, traces and review bindings; new pages start idle.
        _invalidate_outputs(s, [])
        s["pages"] = s["pages"][:count] + [_page(i) for i in range(len(s["pages"]) + 1, count + 1)]
        s["requirements"]["values"]["page_count"] = count
    elif kind == "page.generate_started":
        require(_template_ready(s), "Both modes require template confirmation")
        p = _get_page(s, d.get("number"))
        require(isinstance(d.get("attempt_id"), str) and d["attempt_id"], "Attempt ID is required")
        require(d["attempt_id"] not in s["used_attempt_ids"], "Attempt IDs must be unique, including retries")
        require(p["generation_status"] != "running", "Page already has a running attempt")
        require(p.get("locked") is not True, "A locked page is never regenerated")
        _guided_turn(s, p)
        _invalidate_outputs(s, [p])
        p["generation_status"] = "running"
        p["attempt"] = {"id": d["attempt_id"], "input_revision": p["input_revision"], "template_revision": t["revision"]}
        s["used_attempt_ids"].append(d["attempt_id"])
        p["error"] = None
    elif kind in {"page.generated", "page.failed"}:
        require(_template_ready(s), "Template was invalidated")
        p = _get_page(s, d.get("number"))
        require(p["generation_status"] == "running" and p["attempt"]
                and p["attempt"]["id"] == d.get("attempt_id"), "Stale or missing page attempt")
        if kind == "page.failed":
            p["generation_status"] = "failed"
            p["error"] = str(d.get("error", "Generation failed"))
        else:
            p["artifact"] = _artifact(s, p, d)
            p["image_version"] += 1
            p["generation_status"] = "ready"
            p["error"] = None
            p.pop("fresh_render", None)
        p["attempt"] = None
    elif kind == "page.confirmed":
        require(s["mode"] == "guided", "Batch does not require page-by-page confirmation")
        p = _get_page(s, d.get("number"))
        _guided_turn(s, p)
        require(_current(s, p) and d.get("image_version") == p["image_version"], "Only a generated current image can be confirmed")
        p["confirmed_image_version"] = p["image_version"]
    elif kind == "images.confirmed":
        require(s.get('requires_final_image_review') or s["review_policy"] == "template_and_final", "This policy does not add a final confirmation")
        require(_all_current(s) and d.get("image_versions") == _image_snapshot(s), "Confirm all current page versions")
        s["images_confirmation"] = {"policy": "template_and_final", "event_id": eid,
                                    "basis": "single_final_user_review", "image_versions": _image_snapshot(s),
                                    "template_revision": t["revision"]}
    elif kind == "trace.started":
        require(_all_reviewed(s), "All current images must satisfy the selected review policy")
        require(s["trace_batch"] is None, "Tracing is already running")
        require(isinstance(d.get("batch_id"), str) and d["batch_id"], "Batch ID is required")
        require(d["batch_id"] not in s["used_trace_batch_ids"], "Tracing batch IDs must be unique")
        pending = [p["number"] for p in s["pages"] if not _trace_current(p)]
        require(bool(pending), "All current pages have already been traced")
        s["trace_batch"] = {"id": d["batch_id"], "image_versions": _image_snapshot(s), "pending_pages": pending, "completed_pages": []}
        s["used_trace_batch_ids"].append(d["batch_id"])
        s["trace_error"] = s["qa"] = s["export"] = s["qa_run"] = s["export_run"] = None
    elif kind in {"trace.page_completed", "trace.completed", "trace.failed"}:
        b = s["trace_batch"]
        require(b is not None and b["id"] == d.get("batch_id")
                and _all_reviewed(s) and b["image_versions"] == _image_snapshot(s), "Stale or missing tracing batch")
        if kind == "trace.page_completed":
            _accept_trace_results(s, b, [d.get("page")], require_all=False)
        elif kind == "trace.failed":
            _accept_trace_results(s, b, d.get("pages", []), require_all=False)
            s["trace_error"] = str(d.get("error", "Tracing failed"))
            s["trace_batch"] = None
        else:
            _accept_trace_results(s, b, d.get("pages", []), require_all=True)
            s["trace_batch"] = None
    elif kind == "qa.started":
        require(_all_traced(s), "Quality checks require current traced artifacts")
        require(isinstance(d.get("run_id"), str) and d["run_id"], "QA run ID is required")
        require(d["run_id"] not in s["used_qa_run_ids"], "QA run IDs must be unique")
        s["qa_run"] = {"id": d["run_id"], "image_versions": _image_snapshot(s), "trace_versions": _trace_snapshot(s)}
        s["used_qa_run_ids"].append(d["run_id"])
        s["qa"] = s["export"] = s["export_run"] = s["qa_error"] = None
    elif kind in {"qa.completed", "qa.failed"}:
        require(_all_traced(s), "Quality checks require current traced artifacts")
        run = s["qa_run"]
        require(run is not None and run["id"] == d.get("run_id"), "Stale or missing QA run")
        require(d.get("image_versions") == _image_snapshot(s)
                and d.get("trace_versions") == _trace_snapshot(s), "QA callback belongs to old artifact versions")
        if kind == "qa.failed":
            s["qa_error"] = str(d.get("error", "QA execution failed"))
        else:
            require(isinstance(d.get("passed"), bool) and isinstance(d.get("checks"), dict)
                    and d["checks"], "QA needs an explicit result and check report")
            if d["passed"]:
                require(all(v is True or v == "not_applicable" for v in d["checks"].values()), "A failed check cannot pass QA")
            s["qa"] = {"run_id": run["id"], "passed": d["passed"], "checks": deepcopy(d["checks"]),
                       "report_uri": d.get("report_uri"), "image_versions": _image_snapshot(s),
                       "trace_versions": _trace_snapshot(s)}
        s["qa_run"] = None
        s["export"] = None
    elif kind == "export.started":
        require(_qa_current(s), "Export requires a passing quality check for current versions")
        require(isinstance(d.get("run_id"), str) and d["run_id"], "Export run ID is required")
        require(d["run_id"] not in s["used_export_run_ids"], "Export run IDs must be unique")
        s["export_run"] = {"id": d["run_id"], "qa_run_id": s["qa"]["run_id"],
                           "image_versions": _image_snapshot(s), "trace_versions": _trace_snapshot(s)}
        s["used_export_run_ids"].append(d["run_id"])
        s["export"] = s["export_error"] = None
    elif kind in {"export.completed", "export.failed"}:
        require(_qa_current(s), "Export requires a passing quality check for current versions")
        run = s["export_run"]
        require(run is not None and run["id"] == d.get("run_id") and run["qa_run_id"] == s["qa"]["run_id"], "Stale or missing export run")
        require(d.get("image_versions") == _image_snapshot(s)
                and d.get("trace_versions") == _trace_snapshot(s), "Export callback belongs to old artifact versions")
        if kind == "export.failed":
            s["export_error"] = str(d.get("error", "Export failed"))
        else:
            require(isinstance(d.get("pptx_uri"), str) and d["pptx_uri"].lower().endswith(".pptx"), "PPTX artifact URI is required")
            s["export"] = {"run_id": run["id"], "qa_run_id": run["qa_run_id"], "pptx_uri": d["pptx_uri"], "image_versions": _image_snapshot(s), "trace_versions": _trace_snapshot(s)}
            s["export_error"] = None
        s["export_run"] = None
    else:
        raise WorkflowError(f"Unknown event type: {kind}")
    _refresh_review(s, eid)
    s["revision"] += 1
    s["applied_events"][eid] = digest
    s["events"].append({"event_id": eid, "type": kind, "payload": deepcopy(d),
                        "revision": s["revision"], "recorded_at": datetime.now(timezone.utc).isoformat()})
    return s


class WorkflowStore:
    """Atomic JSON snapshots and exclusive per-project writes on one filesystem.

    Locks fail fast. A lock left by process death needs operator recovery after
    confirming no writer remains. This is intentionally not a distributed queue.
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: str) -> Path:
        require(bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", project_id)), "Invalid project ID")
        return self.directory / f"{project_id}.json"

    def load(self, project_id: str) -> dict:
        return json.loads(self._path(project_id).read_text(encoding="utf-8"))

    def _write(self, path: Path, state: dict) -> None:
        temporary = path.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _locked(self, project_id: str, action):
        path = self._path(project_id)
        lock = path.with_suffix(".lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise WorkflowError("Project has another writer or a stale lock") from exc
        try:
            os.close(descriptor)
            return action(path)
        finally:
            lock.unlink(missing_ok=True)

    def create(self, project_id: str, **options) -> dict:
        def action(path):
            require(not path.exists(), "Project already exists")
            state = new_state(project_id, **options)
            self._write(path, state)
            return state
        return self._locked(project_id, action)

    def apply(self, project_id: str, event: dict, expected_revision: int | None = None) -> dict:
        def action(path):
            before = json.loads(path.read_text(encoding="utf-8"))
            after = apply_event(before, event, expected_revision)
            self._write(path, after)
            return after
        return self._locked(project_id, action)
