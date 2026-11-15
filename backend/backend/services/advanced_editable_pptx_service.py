"""
Advanced editable PPTX export orchestrator.

This adapts the local image-to-editable-ppt skill into Banana Slides without
Codex CLI, Codex subagents, MinerU, or desktop tools. The orchestrator owns only
run preparation, page dispatch/result recording, finalization, and task progress.
Page reconstruction is delegated to internal worker classes.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PIL import Image

logger = logging.getLogger(__name__)

ADVANCED_EDITABLE_TEXT_MODEL = "gpt-5.5"
ADVANCED_EDITABLE_IMAGE_MODEL = "gpt-image-2"
DEFAULT_SLIDE_WIDTH_INCHES = 13.333


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _skill_root() -> Path:
    return (
        _repo_root()
        / "image-to-editable-ppt-skill-main"
        / "image-to-editable-ppt-skill-main"
        / "skills"
        / "image-to-editable-ppt"
    )


def _script_path(name: str) -> Path:
    path = _skill_root() / "scripts" / name
    if not path.exists():
        raise FileNotFoundError(f"image-to-editable-ppt script not found: {path}")
    return path


def _load_build_module():
    path = _script_path("build_pptx_from_manifest.py")
    spec = importlib.util.spec_from_file_location("image_to_editable_ppt_build", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load PPTX builder module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _slide_size_inches(width_px: int, height_px: int) -> Dict[str, float]:
    if width_px <= 0 or height_px <= 0:
        return {"width": DEFAULT_SLIDE_WIDTH_INCHES, "height": 7.5}
    return {
        "width": DEFAULT_SLIDE_WIDTH_INCHES,
        "height": round(DEFAULT_SLIDE_WIDTH_INCHES * height_px / width_px, 4),
    }


def _safe_rel(path: Path, base: Path) -> str:
    return path.resolve().relative_to(base.resolve()).as_posix()


class AdvancedEditablePPTXRunner:
    """Deck-level orchestrator for the built-in skill-compatible export."""

    def __init__(
        self,
        upload_folder: str,
        progress_callback: Optional[Callable[[str, str, int], None]] = None,
    ) -> None:
        self.upload_folder = Path(upload_folder)
        self.progress_callback = progress_callback
        self._builder = None

    def run(
        self,
        *,
        project_id: str,
        image_paths: List[str],
        output_file: str,
        task_id: str,
        max_workers: int = 4,
        max_depth: int = 1,
        export_extractor_method: str = "hybrid",
        export_inpaint_method: str = "hybrid",
        enable_icon_subject_extraction: bool = True,
        text_attribute_extractor: Any = None,
        fail_fast: bool = True,
    ) -> Dict[str, Any]:
        if not image_paths:
            raise ValueError("No generated images found for project")

        run_dir = self.upload_folder / project_id / "exports" / "advanced_editable_runs" / task_id
        final_dir = run_dir / "final"
        pages_dir = run_dir / "pages"
        final_dir.mkdir(parents=True, exist_ok=True)
        pages_dir.mkdir(parents=True, exist_ok=True)

        output_path = Path(output_file)
        final_output = final_dir / output_path.name
        run_id = task_id or str(uuid.uuid4())

        self._progress("准备", f"准备 {len(image_paths)} 页高级可编辑导出", 2)
        deck = self._prepare_run_manifest(
            run_id=run_id,
            run_dir=run_dir,
            image_paths=[Path(p) for p in image_paths],
            final_output=final_output,
            max_workers=max_workers,
        )

        self._progress("页面分发", "写入 page worker prompt 并记录 dispatch", 8)
        from services.advanced_editable_workers import AdvancedEditablePageWorker

        page_worker = AdvancedEditablePageWorker(self._get_builder())
        agent_ids: Dict[str, str] = {}
        for page in deck["pages"]:
            page_worker.write_dispatch_prompt(run_dir, page)
            agent_id = f"banana-internal-page-worker-{page['page_id']}"
            agent_ids[page["page_id"]] = agent_id
            self._run_skill_script(
                "record_page_dispatch.py",
                run_dir,
                "--page",
                page["page_id"],
                "--agent-id",
                agent_id,
                "--agent-nickname",
                "banana-internal-page-worker",
                "--prompt-file",
                "prompts/page-worker.md",
            )

        self._progress("页面重建", "内部 page worker 开始重建页面", 12)
        from flask import current_app, has_app_context
        flask_app = current_app._get_current_object() if has_app_context() else None

        page_results: List[Dict[str, Any]] = [None] * len(deck["pages"])  # type: ignore[list-item]
        failed_pages: List[Dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 8))) as executor:
            futures = {
                executor.submit(
                    self._process_page,
                    run_dir,
                    page,
                    agent_ids[page["page_id"]],
                    flask_app,
                    max_depth,
                    export_extractor_method,
                    export_inpaint_method,
                    enable_icon_subject_extraction,
                    text_attribute_extractor,
                ): idx
                for idx, page in enumerate(deck["pages"])
            }
            completed = 0
            for future in as_completed(futures):
                idx = futures[future]
                page = deck["pages"][idx]
                try:
                    page_results[idx] = future.result()
                except Exception as exc:
                    if fail_fast:
                        raise
                    failed_pages.append({"page_id": page["page_id"], "error": str(exc)})
                    self._mark_page_failed(run_dir, page["page_id"], str(exc))
                    continue
                self._run_skill_script(
                    "record_page_result.py",
                    run_dir,
                    "--page",
                    page_results[idx]["page_id"],
                    "--agent-id",
                    page_results[idx]["agent_id"],
                    "--page-result",
                    "page_result.json",
                )
                completed += 1
                percent = 12 + int(58 * completed / len(deck["pages"]))
                self._progress("页面重建", f"已完成 {completed}/{len(deck['pages'])} 页", percent)

        if failed_pages:
            if not page_results or not any(page_results):
                raise RuntimeError(f"Advanced editable export failed for all pages: {failed_pages[0]['error']}")
            deck = self._prune_failed_pages(run_dir)

        self._progress("组装PPTX", "调用 skill finalize 脚本组装最终 PPTX", 76)
        self._run_skill_script("finalize_deck_run.py", run_dir)

        deck_validation = final_dir / "validation.json"
        if not deck_validation.exists() or not _read_json(deck_validation).get("passed"):
            raise RuntimeError(f"Deck validation failed: {deck_validation}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(final_output, output_path)

        summary = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "complete",
            "page_count": len(deck["pages"]),
            "output": str(output_path),
            "run_dir": str(run_dir),
            "validation": str(deck_validation),
            "completed_at": datetime.utcnow().isoformat(),
        }
        _write_json(final_dir / "run_summary.json", summary)
        self._progress("完成", "高级可编辑 PPTX 已生成", 100)
        return summary

    def _progress(self, step: str, message: str, percent: int) -> None:
        logger.info("[advanced editable pptx %s%%] %s: %s", percent, step, message)
        if self.progress_callback:
            self.progress_callback(step, message, percent)

    def _get_builder(self):
        if self._builder is None:
            self._builder = _load_build_module()
        return self._builder

    def _run_skill_script(self, name: str, run_dir: Path, *args: str) -> str:
        script = _script_path(name)
        result = subprocess.run(
            [sys.executable, str(script), str(run_dir), *[str(arg) for arg in args]],
            cwd=str(script.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if result.returncode != 0:
            raise RuntimeError(f"{name} failed: {result.stdout or result.stderr}")
        return result.stdout

    def _mark_page_failed(self, run_dir: Path, page_id: str, error: str) -> None:
        jobs_path = run_dir / "page_jobs.json"
        jobs = _read_json(jobs_path)
        for page in jobs.get("pages", []):
            if page.get("page_id") == page_id:
                page["status"] = "failed"
                page["blocker"] = error
                break
        _write_json(jobs_path, jobs)

    def _prune_failed_pages(self, run_dir: Path) -> Dict[str, Any]:
        deck_path = run_dir / "deck_manifest.json"
        jobs_path = run_dir / "page_jobs.json"
        deck = _read_json(deck_path)
        jobs = _read_json(jobs_path)
        successful_ids = {
            page.get("page_id")
            for page in jobs.get("pages", [])
            if page.get("status") in {"recorded", "accepted"}
        }
        deck["pages"] = [page for page in deck.get("pages", []) if page.get("page_id") in successful_ids]
        deck["page_count"] = len(deck["pages"])
        jobs["pages"] = [page for page in jobs.get("pages", []) if page.get("page_id") in successful_ids]
        _write_json(deck_path, deck)
        _write_json(jobs_path, jobs)
        return deck

    def _prepare_run_manifest(
        self,
        *,
        run_id: str,
        run_dir: Path,
        image_paths: List[Path],
        final_output: Path,
        max_workers: int,
    ) -> Dict[str, Any]:
        pages = []
        for idx, image_path in enumerate(image_paths, start=1):
            page_id = f"page_{idx:03d}"
            page_dir = run_dir / "pages" / page_id
            page_dir.mkdir(parents=True, exist_ok=True)
            source = page_dir / "source.png"
            shutil.copy2(image_path, source)
            with Image.open(source) as img:
                width_px, height_px = img.size

            slide_size = _slide_size_inches(width_px, height_px)

            page = {
                "page_id": page_id,
                "page_index": idx,
                "status": "pending",
                "page_dir": f"pages/{page_id}",
                "source": f"pages/{page_id}/source.png",
                "source_image": f"pages/{page_id}/source.png",
                "page_request": f"pages/{page_id}/page_request.json",
                "manifest": f"pages/{page_id}/manifest.json",
                "validation": f"pages/{page_id}/validation.json",
                "dispatch": None,
                "result": None,
                "repair": [],
                "blocker": None,
                "accepted": False,
            }
            request = {
                "schema_version": 1,
                "run_id": run_id,
                "page_id": page_id,
                "page_index": idx,
                "page_dir": str(page_dir),
                "source_image": str(source),
                "source_size_px": {"width": width_px, "height": height_px},
                "slide": slide_size,
                "max_concurrent_pages": max_workers,
                "allowed_write_scope": str(page_dir),
                "required_outputs": {
                    "manifest": str(page_dir / "manifest.json"),
                    "imagegen_jobs": str(page_dir / "imagegen-jobs.json"),
                    "page_pptx": str(page_dir / "page.pptx"),
                    "preview": str(page_dir / "preview.png"),
                    "contact_sheet": str(page_dir / "split_assets_contact.png"),
                    "validation": str(page_dir / "validation.json"),
                    "page_result": str(page_dir / "page_result.json"),
                },
            }
            _write_json(page_dir / "page_request.json", request)
            _write_json(page_dir / "imagegen-jobs.json", {"schema_version": 1, "run_id": run_id, "page_id": page_id, "jobs": []})
            pages.append(page)

        deck = {
            "schema_version": 1,
            "run_id": run_id,
            "job_dir": str(run_dir),
            "input_type": "images",
            "page_count": len(pages),
            "pages": pages,
            "notes_manifest": "notes_manifest.json",
            "page_jobs": "page_jobs.json",
            "output": _safe_rel(final_output, run_dir),
            "max_concurrent_pages": max_workers,
            "prepared_at": datetime.utcnow().isoformat(),
        }
        jobs = {
            "schema_version": 1,
            "run_id": run_id,
            "run_status": "inputs_prepared",
            "max_concurrent_pages": max_workers,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "pages": pages,
        }
        _write_json(run_dir / "deck_manifest.json", deck)
        _write_json(run_dir / "page_jobs.json", jobs)
        _write_json(run_dir / "notes_manifest.json", {"source": None, "notes": []})
        return deck

    def _process_page(
        self,
        run_dir: Path,
        page: Dict[str, Any],
        agent_id: str,
        flask_app: Any,
        max_depth: int,
        export_extractor_method: str,
        export_inpaint_method: str,
        enable_icon_subject_extraction: bool,
        text_attribute_extractor: Any,
    ) -> Dict[str, Any]:
        if flask_app is not None:
            with flask_app.app_context():
                return self._process_page_in_context(
                    run_dir,
                    page,
                    agent_id,
                    max_depth,
                    export_extractor_method,
                    export_inpaint_method,
                    enable_icon_subject_extraction,
                    text_attribute_extractor,
                )
        return self._process_page_in_context(
            run_dir,
            page,
            agent_id,
            max_depth,
            export_extractor_method,
            export_inpaint_method,
            enable_icon_subject_extraction,
            text_attribute_extractor,
        )

    def _process_page_in_context(
        self,
        run_dir: Path,
        page: Dict[str, Any],
        agent_id: str,
        max_depth: int,
        export_extractor_method: str,
        export_inpaint_method: str,
        enable_icon_subject_extraction: bool,
        text_attribute_extractor: Any,
    ) -> Dict[str, Any]:
        from flask import current_app
        from services.advanced_editable_workers import AdvancedEditablePageWorker
        from services.ai_providers import get_text_provider
        from services.ai_providers.image.openai_provider import OpenAIImageProvider
        from services.ai_service import AIService

        text_model = current_app.config.get("ADVANCED_EDITABLE_TEXT_MODEL") or ADVANCED_EDITABLE_TEXT_MODEL
        image_model = current_app.config.get("ADVANCED_EDITABLE_IMAGE_MODEL") or ADVANCED_EDITABLE_IMAGE_MODEL
        api_key = current_app.config.get("IMAGE_API_KEY") or current_app.config.get("OPENAI_API_KEY") or ""
        api_base = current_app.config.get("IMAGE_API_BASE") or current_app.config.get("OPENAI_API_BASE") or None
        if not api_key:
            raise ValueError(f"{image_model} export requires OPENAI_API_KEY or IMAGE_API_KEY")

        text_provider = get_text_provider(model=text_model)
        image_provider = OpenAIImageProvider(
            api_key=api_key,
            api_base=api_base,
            model=image_model,
            image_api_protocol="images",
        )
        ai_service = AIService(text_provider=text_provider, image_provider=image_provider)
        page_worker = AdvancedEditablePageWorker(self._get_builder())
        page_result = page_worker.run_page(run_dir, page, ai_service)
        page_result["agent_id"] = agent_id
        return page_result
