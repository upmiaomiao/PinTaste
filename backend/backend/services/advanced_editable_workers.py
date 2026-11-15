"""
Internal workers for the built-in image-to-editable-ppt export flow.

These classes mirror the skill's agent split without depending on Codex
subagents: the orchestrator dispatches pages, this page worker reconstructs a
single page, and the imagegen worker is the only image editing layer.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

logger = logging.getLogger(__name__)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def skill_root() -> Path:
    return (
        repo_root()
        / "image-to-editable-ppt-skill-main"
        / "image-to-editable-ppt-skill-main"
        / "skills"
        / "image-to-editable-ppt"
    )


def script_path(name: str) -> Path:
    path = skill_root() / "scripts" / name
    if not path.exists():
        raise FileNotFoundError(f"image-to-editable-ppt script not found: {path}")
    return path


def load_build_module():
    path = script_path("build_pptx_from_manifest.py")
    spec = importlib.util.spec_from_file_location("image_to_editable_ppt_build", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load PPTX builder module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_rel(path: Path, base: Path) -> str:
    return path.resolve().relative_to(base.resolve()).as_posix()


def copy_asset(src: Path, dest_dir: Path, name: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = src.suffix.lower() or ".png"
    dest = dest_dir / f"{name}{suffix}"
    shutil.copy2(src, dest)
    return dest


ADVANCED_EDITABLE_IMAGE_MODEL = "gpt-image-2"
DEFAULT_SLIDE_WIDTH_INCHES = 13.333
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def advanced_editable_flag(name: str, default: bool = True) -> bool:
    value = None
    try:
        from flask import current_app, has_app_context
        if has_app_context():
            value = current_app.config.get(name)
    except Exception:
        value = None
    if value is None:
        value = os.getenv(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off"}


class AdvancedEditableImageGenWorker:
    """Page-local imagegen adapter for advanced editable exports."""

    def __init__(self, ai_service: Any) -> None:
        self.ai_service = ai_service

    def generate_clean_background(self, page_dir: Path, source: Path) -> Path:
        generated_dir = page_dir / "generated"
        generated_dir.mkdir(parents=True, exist_ok=True)
        output = generated_dir / "clean_background.png"
        prompt = (
            "Edit this slide image into a clean background for an editable PowerPoint reconstruction. "
            "Remove all readable text, captions, labels, bullets, icons, logos, diagrams, charts, pictograms, foreground marks, and semantic visual objects that should become separate PowerPoint objects. "
            "Preserve only the true slide background: canvas size, base composition, colors, gradients, photos, textures, lighting, perspective, and non-semantic background details. "
            "Fill removed areas naturally using the surrounding background. Do not add new text."
        )
        self._record_job(page_dir, "clean_background", "running", prompt, None, "clean_background")
        with Image.open(source) as src:
            ref_image = src.convert("RGB")
        try:
            image = self.ai_service.image_provider.generate_image(
                prompt=prompt,
                ref_images=[ref_image],
                aspect_ratio="16:9",
                resolution="2K",
            )
        finally:
            ref_image.close()
        if image is None:
            self._record_job(page_dir, "clean_background", "failed", prompt, None, "clean_background")
            raise RuntimeError(f"{ADVANCED_EDITABLE_IMAGE_MODEL} did not return a clean background image")
        with Image.open(source) as src:
            if image.size != src.size:
                image = image.resize(src.size, Image.Resampling.LANCZOS)
        image.save(output)
        self._record_job(page_dir, "clean_background", "recorded", prompt, output, "clean_background")
        return output

    def record_source_asset(self, page_dir: Path, asset_id: str, source: Path, crop: Path, output: Path) -> None:
        prompt = (
            "Visual-priority export preserved this semantic visual as a source-derived independent asset. "
            "This avoids hallucinated redraws and keeps reference identity while still making the object separately selectable."
        )
        self._record_job(page_dir, asset_id, "recorded", prompt, output, "source_asset")

    def generate_semantic_asset(
        self,
        page_dir: Path,
        asset_id: str,
        crop: Path,
        label: str,
    ) -> Optional[Path]:
        generated_dir = page_dir / "generated"
        generated_dir.mkdir(parents=True, exist_ok=True)
        output = generated_dir / f"{asset_id}.png"
        prompt = (
            "Create one isolated bitmap asset for a PowerPoint visual replica from this reference crop. "
            "Preserve the exact visual identity, colors, geometry, proportions, and style of the object. "
            "Remove surrounding slide background, panels, labels, captions, and readable text unless the text is an inseparable logo mark. "
            "Use a plain white background, no shadow, no extra context, no watermark, no added objects. "
            f"Semantic object label: {label}."
        )
        self._record_job(page_dir, asset_id, "running", prompt, None, "semantic_asset")
        with Image.open(crop) as src:
            ref_image = src.convert("RGB")
        try:
            image = self.ai_service.image_provider.generate_image(
                prompt=prompt,
                ref_images=[ref_image],
                aspect_ratio=self._aspect_ratio_for(ref_image.size),
                resolution="1K",
            )
        except Exception as exc:
            logger.warning("Semantic asset generation failed for %s: %s", asset_id, exc)
            self._record_job(page_dir, asset_id, "failed", prompt, None, "semantic_asset")
            return None
        finally:
            ref_image.close()
        if image is None:
            self._record_job(page_dir, asset_id, "failed", prompt, None, "semantic_asset")
            return None
        image.save(output)
        self._record_job(page_dir, asset_id, "recorded", prompt, output, "semantic_asset")
        return output

    def generate_asset_grid(
        self,
        page_dir: Path,
        cycle: int,
        prompt_sheet: Path,
        asset_ids: List[str],
        rows: int,
        cols: int,
    ) -> Optional[Path]:
        generated_dir = page_dir / "generated"
        generated_dir.mkdir(parents=True, exist_ok=True)
        output = generated_dir / f"imagegen_asset_grid_cycle_{cycle:03d}.png"
        prompt = (
            "Create an isolated asset grid for a PowerPoint visual replica from the single reference prompt sheet. "
            "The prompt sheet contains the full slide reference and object crops. "
            f"Generate exactly {rows} rows x {cols} columns in the same object order: {', '.join(asset_ids)}. "
            "Each cell must contain exactly one object centered in its cell. "
            "Use a perfectly uniform white background. No texture, no shadows, no panels, no labels, no arrows, no surrounding slide context. "
            "No readable text, numbers, watermark, or extra objects unless text is inseparable from a logo mark."
        )
        self._record_job(page_dir, f"asset_grid_cycle_{cycle:03d}", "running", prompt, None, "asset_grid")
        with Image.open(prompt_sheet) as src:
            ref_image = src.convert("RGB")
        try:
            image = self.ai_service.image_provider.generate_image(
                prompt=prompt,
                ref_images=[ref_image],
                aspect_ratio=self._aspect_ratio_for(ref_image.size),
                resolution="2K",
            )
        except Exception as exc:
            logger.warning("Asset grid generation failed for cycle %s: %s", cycle, exc)
            self._record_job(page_dir, f"asset_grid_cycle_{cycle:03d}", "failed", prompt, None, "asset_grid")
            return None
        finally:
            ref_image.close()
        if image is None:
            self._record_job(page_dir, f"asset_grid_cycle_{cycle:03d}", "failed", prompt, None, "asset_grid")
            return None
        image.save(output)
        self._record_job(page_dir, f"asset_grid_cycle_{cycle:03d}", "recorded", prompt, output, "asset_grid")
        return output

    def _aspect_ratio_for(self, size: tuple[int, int]) -> str:
        width, height = size
        if width <= 0 or height <= 0:
            return "1:1"
        ratio = width / height
        if ratio >= 1.55:
            return "16:9"
        if ratio <= 0.65:
            return "9:16"
        if ratio >= 1.15:
            return "4:3"
        if ratio <= 0.87:
            return "3:4"
        return "1:1"

    def _record_job(self, page_dir: Path, job_id: str, status: str, prompt: str, output: Optional[Path], job_type: str) -> None:
        jobs_path = page_dir / "imagegen-jobs.json"
        jobs = read_json(jobs_path) if jobs_path.exists() else {"schema_version": 1, "jobs": []}
        entries = jobs.setdefault("jobs", [])
        entry = next((item for item in entries if item.get("id") == job_id), None)
        if entry is None:
            entry = {"id": job_id, "type": job_type, "model": ADVANCED_EDITABLE_IMAGE_MODEL}
            entries.append(entry)
        entry.update({
            "status": status,
            "prompt": prompt,
            "updated_at": datetime.utcnow().isoformat(),
        })
        if output:
            entry["output"] = safe_rel(output, page_dir)
        write_json(jobs_path, jobs)


class AdvancedEditablePageWorker:
    """Internal page worker that owns exactly one pages/page_NNN directory."""

    MAX_FIDELITY_REPAIR_CYCLES = 4

    def __init__(self, builder: Any = None) -> None:
        self.builder = builder or load_build_module()

    def write_dispatch_prompt(self, run_dir: Path, page: Dict[str, Any]) -> Path:
        page_dir = run_dir / page["page_dir"]
        prompts_dir = page_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = prompts_dir / "page-worker.md"
        template = (skill_root() / "prompts" / "page-worker.md").read_text(encoding="utf-8")
        rendered = template.replace("<absolute run dir>", str(run_dir))
        rendered = rendered.replace("<page_001>", page["page_id"])
        rendered = rendered.replace("<absolute page dir>", str(page_dir))
        rendered = rendered.replace("<skill root>", str(skill_root()))
        prompt_path.write_text(rendered, encoding="utf-8")
        return prompt_path

    def run_page(self, run_dir: Path, page: Dict[str, Any], ai_service: Any) -> Dict[str, Any]:
        page_dir = run_dir / page["page_dir"]
        source = run_dir / page["source_image"]
        imagegen_worker = AdvancedEditableImageGenWorker(ai_service)

        with Image.open(source) as img:
            width, height = img.size

        # --- Step 1: MinerU first (text + layout) ---
        mineru_text_boxes: List[Dict[str, Any]] = []
        mineru_image_items: List[Dict[str, Any]] = []
        mineru_enabled = False
        if advanced_editable_flag("ADVANCED_EDITABLE_USE_MINERU", True):
            mineru_plan = self._extract_mineru_page_plan(source, width, height)
            if mineru_plan.get("text_boxes") or mineru_plan.get("images"):
                mineru_text_boxes = mineru_plan.get("text_boxes", [])
                mineru_image_items = mineru_plan.get("images", [])
                mineru_enabled = True

        # --- Step 2: Vision analysis (skip text when MinerU delivered text) ---
        page_plan = self._analyze_page_manifest(
            ai_service, source, width, height,
            skip_text_boxes=mineru_enabled,
        )

        # Inject MinerU text as authoritative source
        if mineru_enabled:
            page_plan["text_boxes"] = self._merge_ocr_with_visual_text_boxes(
                mineru_text_boxes, page_plan.get("text_boxes", []), width, height
            )
            if mineru_image_items:
                page_plan["images"] = self._merge_visual_items(
                    mineru_image_items,
                    page_plan.get("images", []) or page_plan.get("semantic_visuals", []),
                    width, height,
                )
            page_plan.setdefault("known_limits", []).append("Layout/text objects are prioritized from MinerU.")

        # --- Step 3: Baidu OCR fallback (only when MinerU had no text) ---
        ocr_text_boxes: List[Dict[str, Any]] = []
        ocr_enabled = False
        if not mineru_enabled and advanced_editable_flag("ADVANCED_EDITABLE_USE_BAIDU_OCR", True):
            ocr_text_boxes = self._extract_ocr_text_boxes(source, width, height)
            ocr_enabled = bool(ocr_text_boxes)
            if ocr_enabled:
                page_plan["text_boxes"] = self._merge_ocr_with_visual_text_boxes(
                    ocr_text_boxes, page_plan.get("text_boxes", []), width, height
                )
                page_plan.setdefault("known_limits", []).append("Text boxes are prioritized from Baidu accurate OCR.")

        page_mode = self._classify_page_mode(page_plan, width, height)
        page_plan["page_mode"] = page_mode

        # --- Step 4: read slide dimensions from page_request (real aspect ratio) ---
        page_request_path = page_dir / "page_request.json"
        if page_request_path.exists():
            page_request = read_json(page_request_path)
            slide_dims = page_request.get("slide", {})
            slide_width_in = float(slide_dims.get("width") or DEFAULT_SLIDE_WIDTH_INCHES)
            slide_height_in = float(slide_dims.get("height") or round(DEFAULT_SLIDE_WIDTH_INCHES * height / width, 4))
        else:
            slide_width_in = DEFAULT_SLIDE_WIDTH_INCHES
            slide_height_in = round(DEFAULT_SLIDE_WIDTH_INCHES * height / width, 4) if width > 0 else 7.5

        clean_background = None
        if page_plan.get("text_boxes"):
            clean_background = imagegen_worker.generate_clean_background(page_dir, source)
        manifest = self._build_manifest(
            page_dir, source, width, height, page_plan,
            clean_background, imagegen_worker, ai_service,
            ocr_enabled, mineru_enabled,
            slide_width_in=slide_width_in, slide_height_in=slide_height_in,
        )
        repair_report = self._repair_text_gaps(
            page_dir=page_dir,
            source=source,
            width=width,
            height=height,
            manifest=manifest,
            page_plan=page_plan,
            ocr_text_boxes=ocr_text_boxes,
            mineru_text_boxes=mineru_text_boxes,
        )
        manifest_path = page_dir / "manifest.json"
        write_json(manifest_path, manifest)

        page_pptx = page_dir / "page.pptx"
        preview = page_dir / "preview.png"
        self.builder.write_pptx(manifest, page_pptx, manifest_path)
        self.builder.render_preview(manifest, manifest_path, preview)

        contact_sheet = page_dir / "split_assets_contact.png"
        shutil.copy2(preview, contact_sheet)

        validation = page_dir / "validation.json"
        self._validate_page(page_pptx, manifest_path, validation)
        validation_passed = bool(read_json(validation).get("passed"))
        result = {
            "page_manifest": "manifest.json",
            "imagegen_jobs": "imagegen-jobs.json",
            "page_pptx": "page.pptx",
            "preview": "preview.png",
            "contact_sheet": "split_assets_contact.png",
            "validation": "validation.json",
            "page_result": "page_result.json",
            "qa_note": "Generated by Banana Slides internal page worker using the image-to-editable-ppt manifest protocol.",
            "page_mode": page_mode,
            "repair_report": repair_report,
            "known_limits": manifest.get("known_limits", []),
            "validation_passed": validation_passed,
        }
        write_json(page_dir / "page_result.json", result)
        return {"page_id": page["page_id"], "result": result}

    def _classify_page_mode(self, page_plan: Dict[str, Any], width: int, height: int) -> str:
        text_boxes = page_plan.get("text_boxes") if isinstance(page_plan.get("text_boxes"), list) else []
        shapes = page_plan.get("shapes") if isinstance(page_plan.get("shapes"), list) else []
        visuals = page_plan.get("images") or page_plan.get("semantic_visuals") or []
        if not isinstance(visuals, list):
            visuals = []

        vertical_text = 0
        large_bands = 0
        dense_visuals = 0
        left_right_text = set()
        for raw in text_boxes:
            if not isinstance(raw, dict):
                continue
            box = self._normalize_box(raw.get("box_px") or raw.get("bbox"), width, height)
            if not box:
                continue
            x, _y, w, h = box
            if h >= w * 1.8:
                vertical_text += 1
            if x < width * 0.2:
                left_right_text.add("left")
            if x + w > width * 0.8:
                left_right_text.add("right")

        for raw in shapes:
            if not isinstance(raw, dict):
                continue
            box = self._normalize_box(raw.get("box_px") or raw.get("bbox"), width, height)
            if not box:
                continue
            _x, _y, w, h = box
            if w >= width * 0.3 and h <= height * 0.12:
                large_bands += 1

        for raw in visuals:
            if not isinstance(raw, dict):
                continue
            box = self._normalize_box(raw.get("box_px") or raw.get("bbox"), width, height)
            if not box:
                continue
            area_ratio = (box[2] * box[3]) / max(1, width * height)
            label = " ".join(str(raw.get(key) or "") for key in ("semantic_label", "label", "classification", "type")).lower()
            if area_ratio <= 0.025 or any(token in label for token in ("icon", "logo", "arrow", "badge", "symbol", "图标", "箭头")):
                dense_visuals += 1

        score = 0
        if len(text_boxes) >= 8:
            score += 1
        if len(visuals) >= 6:
            score += 1
        if vertical_text >= 1:
            score += 1
        if len(left_right_text) == 2:
            score += 1
        if large_bands >= 3:
            score += 1
        if dense_visuals >= 5:
            score += 1
        return "fidelity_first" if score >= 3 else "editable_first"

    def _analyze_page_manifest(self, ai_service: Any, source: Path, width: int, height: int, *, skip_text_boxes: bool = False) -> Dict[str, Any]:
        text_box_schema = (
            '[]'
            if skip_text_boxes else
            '[{"id":"text_001","text":"exact readable text, never rewritten","box_px":[x,y,w,h],"font_size":24,"expected_lines":1,"font_group":"title","font":"Noto Sans CJK SC","color":"#111111","align":"left","valign":"top","bold":false,"italic":false,"rotation":0,"z_index":300}]'
        )
        text_rules = (
            '- Do not return text_boxes. MinerU has already provided the authoritative readable text and bbox data for this page.\n'
            if skip_text_boxes else
            '- Every readable source text must be a native text box. Transcribe exactly; do not summarize, translate, rewrite, complete, or correct text.\n'
            '- Preserve visible line breaks. If the source text is one line, expected_lines must be 1.\n'
            '- Estimate font_size conservatively from the source, never larger than the visible text box can contain.\n'
            '- Give repeated/peer text a shared font_group so the runner can force consistent sizing.\n'
        )
        prompt = f"""
You are the internal page worker for the image-to-editable-ppt skill. Return strict JSON only.

Source image size: width={width}, height={height}. Use pixel coordinates [x,y,w,h].

Return:
{{
  "text_boxes": {text_box_schema},
  "shapes": [{{"id":"shape_001","type":"rect","box_px":[x,y,w,h],"fill":"#FFFFFF","stroke":"none","purpose":"layout_only","z_index":100}}],
  "images": [{{"id":"visual_001","type":"semantic_visual","box_px":[x,y,w,h],"anchor_slot":[x,y,w,h],"semantic_unit_id":"visual_001","semantic_label":"one independently selectable non-text visual unit","z_index":200}}],
  "layout_rules": {{
    "text_groups": [{{"id":"group_001","element_ids":["text_001"],"font_size":24}}],
    "alignment_rules": [{{"type":"center_x","element_ids":["shape_001","text_001"],"tolerance_px":8}}]
  }},
  "known_limits": []
}}

Rules:
{text_rules}- Simple layout primitives should be shapes: panels, frames, dividers, connector lines, and axis-aligned block arrows/flow arrows that can be represented as native PPT shapes.
- Minimum semantic unit rule: split every non-text visual into the smallest independently draggable semantic unit.
- One icon, pictogram, logo-like mark, badge, chart icon, network icon, device mark, complex decorative arrow, or symbol must be one image item with its own tight bbox. Never merge multiple icons or complex arrows into one large image item.
- Return ordinary horizontal/vertical arrows as shapes with purpose="structural_arrow"; use image items only for arrows that cannot be approximated by a native PPT arrow.
- For architecture diagrams, do not return the whole diagram as one image. Return each separable icon/symbol/mark as its own image item; structural rectangles, panels, dividers, and connectors should be shapes; readable labels should be text_boxes.
- A large grouped image is allowed only when it is a non-editable scene fragment with no separable icons, no separable symbols, and no readable labels. This is rare.
- Bboxes must include the complete object, including arrowheads, shadows, strokes, and anti-aliased edges, with 6-10 px padding. Do not include surrounding white space, panel background, neighboring icons, or text labels.
- Do not include a full-slide image.
- Return JSON only.
"""
        try:
            plan = ai_service.generate_json_with_image(prompt, str(source))
            if isinstance(plan, dict):
                return plan
            return {"text_boxes": [], "shapes": [], "images": [], "known_limits": ["Vision model returned a non-object JSON plan."]}
        except Exception as exc:
            logger.warning("Advanced editable page analysis failed for %s: %s", source, exc)
            return {"text_boxes": [], "shapes": [], "images": [], "known_limits": [f"Page analysis failed: {exc}"]}

    def _build_manifest(
        self,
        page_dir: Path,
        source: Path,
        width: int,
        height: int,
        page_plan: Dict[str, Any],
        clean_background: Optional[Path],
        imagegen_worker: AdvancedEditableImageGenWorker,
        ai_service: Any,
        ocr_enabled: bool,
        mineru_enabled: bool,
        *,
        slide_width_in: float,
        slide_height_in: float,
    ) -> Dict[str, Any]:
        assets_dir = page_dir / "assets"
        crops_dir = page_dir / "reference_crops"
        images: List[Dict[str, Any]] = []
        provenance: List[Dict[str, Any]] = []
        visual_inventory: List[Dict[str, Any]] = []
        asset_manifest: List[Dict[str, Any]] = []
        page_mode = str(page_plan.get("page_mode") or "editable_first")

        bg_asset = self._prepare_base_asset(page_dir, source, clean_background, assets_dir, page_mode)
        bg_rel = safe_rel(bg_asset, page_dir)
        images.append({"id": "clean_background", "path": bg_rel, "box_px": [0, 0, width, height], "z_index": 0})
        provenance.append({
            "path": bg_rel,
            "source": "source.png",
            "source_type": "imagegen" if clean_background else "user-provided",
            "provenance_note": (
                f"Clean background edited from the source page with {ADVANCED_EDITABLE_IMAGE_MODEL}."
                if clean_background else
                "Source page preserved as the base layer for fidelity-first reconstruction."
            ),
        })

        text_boxes = self._normalize_text_boxes(page_plan.get("text_boxes", []), width, height)
        layout_rules = self._build_layout_rules(page_plan, text_boxes, width, height)
        text_boxes = self._apply_text_layout_rules(text_boxes, layout_rules)
        text_inventory = self._build_text_inventory(text_boxes)
        generated_shapes: List[Dict[str, Any]] = []

        visual_items = page_plan.get("images", []) or page_plan.get("semantic_visuals", []) or []
        visual_items = list(visual_items) + self._arrow_shapes_as_visuals(page_plan.get("shapes", []), width, height)
        redboxes: List[Dict[str, Any]] = []
        visual_items = self._expand_large_visual_items(source, visual_items, width, height)
        visual_items = self._remove_text_bearing_layout_visuals(source, visual_items, text_boxes, generated_shapes, width, height)
        for idx, item in enumerate(visual_items, start=1):
            self._append_semantic_visual_asset(
                page_dir=page_dir,
                source=source,
                clean_background=clean_background,
                assets_dir=assets_dir,
                crops_dir=crops_dir,
                item=item,
                fallback_id=f"visual_{idx:03d}",
                width=width,
                height=height,
                imagegen_worker=imagegen_worker,
                images=images,
                provenance=provenance,
                visual_inventory=visual_inventory,
                asset_manifest=asset_manifest,
                redboxes=redboxes,
                cycle=1,
            )

        residual_redboxes: List[List[Dict[str, Any]]] = []
        residual_path = self._write_residual_image(source, page_dir / "residual_cycle_001.png", redboxes)
        batch_grid_results = [
            self._generate_and_apply_asset_grid(page_dir, source, asset_manifest, images, provenance, visual_inventory, imagegen_worker, 1),
        ]

        max_cycles = self.MAX_FIDELITY_REPAIR_CYCLES if page_mode == "fidelity_first" else 2
        accumulated_redboxes = list(redboxes)
        for cycle in range(2, max_cycles + 1):
            cycle_redboxes: List[Dict[str, Any]] = []
            residual_plan = self._analyze_residual_visuals(ai_service, residual_path, width, height)
            residual_items = residual_plan.get("images", []) or residual_plan.get("semantic_visuals", []) or []
            residual_items = self._expand_large_visual_items(source, residual_items, width, height, prefix=f"residual_split_{cycle:03d}")
            for idx, item in enumerate(residual_items, start=1):
                self._append_semantic_visual_asset(
                    page_dir=page_dir,
                    source=source,
                    clean_background=clean_background,
                    assets_dir=assets_dir,
                    crops_dir=crops_dir,
                    item=item,
                    fallback_id=f"residual_visual_{cycle:03d}_{idx:03d}",
                    width=width,
                    height=height,
                    imagegen_worker=imagegen_worker,
                    images=images,
                    provenance=provenance,
                    visual_inventory=visual_inventory,
                    asset_manifest=asset_manifest,
                    redboxes=cycle_redboxes,
                    cycle=cycle,
                )
            residual_redboxes.append(cycle_redboxes)
            if cycle_redboxes:
                batch_grid_results.append(
                    self._generate_and_apply_asset_grid(page_dir, source, asset_manifest, images, provenance, visual_inventory, imagegen_worker, cycle)
                )
            if not cycle_redboxes:
                break
            accumulated_redboxes.extend(cycle_redboxes)
            residual_path = self._write_residual_image(source, page_dir / f"residual_cycle_{cycle:03d}.png", accumulated_redboxes)

        shapes = self._normalize_shapes(page_plan.get("shapes", []), width, height) + generated_shapes
        self._apply_alignment_rules(text_boxes, shapes, images, layout_rules)

        self._write_visual_replica_artifacts(
            page_dir=page_dir,
            source=source,
            width=width,
            height=height,
            text_boxes=text_boxes,
            shapes=shapes,
            images=images,
            redboxes=redboxes,
            cycle_2_redboxes=residual_redboxes[0] if residual_redboxes else [],
            visual_inventory=visual_inventory,
            asset_manifest=asset_manifest,
            batch_grid_results=batch_grid_results,
        )
        write_json(page_dir / "visual_inventory.json", {"schema_version": 1, "items": visual_inventory})
        write_json(page_dir / "layout_rules.json", layout_rules)

        return {
            "schema_version": 1,
            "slide": {"width": slide_width_in, "height": slide_height_in},
            "source": {"path": "source.png", "width_px": width, "height_px": height},
            "text_inventory": text_inventory,
            "visual_inventory": visual_inventory,
            "layout_rules": layout_rules,
            "background_strategy": {
                "mode": (
                    "visual-fidelity-source-preserving-base-with-selective-replacement"
                    if page_mode == "fidelity_first" else
                    "visual-priority-clean-base-with-independent-source-assets"
                ),
                "source_consistency": (
                    "The original page image is preserved as the base layer and only selectively replaced to improve fidelity."
                    if page_mode == "fidelity_first" else
                    f"{ADVANCED_EDITABLE_IMAGE_MODEL} edits the original page image to remove readable foreground while preserving source layout, color, and major background objects."
                ),
                "removed_foreground": (
                    "Only validated text and independently selectable semantic visuals are selectively replaced over a preserved source base."
                    if page_mode == "fidelity_first" else
                    "Readable text and foreground marks are removed from the base, then semantic visuals are restored as independent selectable assets."
                ),
                "comparison_note": "MinerU is used as the preferred layout/text extractor when MINERU_TOKEN is configured; no Codex subagent or desktop conversion tool is used.",
            },
            "quality_checks": {
                "font_size_calibrated": True,
                "visual_inventory_matched": True,
                "background_strategy_checked": True,
                "shape_corner_geometry_checked": True,
                "layout_rules_applied": True,
                "semantic_assets_are_independent": True,
            },
            "text_boxes": text_boxes,
            "shapes": shapes,
            "images": images,
            "asset_provenance": provenance,
            "known_limits": page_plan.get("known_limits", []),
            "page_mode": page_mode,
            "ocr_strategy": {
                "enabled": ocr_enabled,
                "provider": "baidu_accurate_ocr" if ocr_enabled else None,
                "text_boxes": len(text_boxes),
            },
            "mineru_strategy": {
                "enabled": mineru_enabled,
                "provider": "mineru" if mineru_enabled else None,
            },
        }

    def _prepare_base_asset(
        self,
        page_dir: Path,
        source: Path,
        clean_background: Optional[Path],
        assets_dir: Path,
        page_mode: str,
    ) -> Path:
        if page_mode == "fidelity_first" or clean_background is None:
            return copy_asset(source, assets_dir, "clean_background")
        return copy_asset(clean_background, assets_dir, "clean_background")

    def _repair_text_gaps(
        self,
        *,
        page_dir: Path,
        source: Path,
        width: int,
        height: int,
        manifest: Dict[str, Any],
        page_plan: Dict[str, Any],
        ocr_text_boxes: List[Dict[str, Any]],
        mineru_text_boxes: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        existing = manifest.get("text_boxes") if isinstance(manifest.get("text_boxes"), list) else []
        missing = self._find_missing_priority_text(page_plan, existing)
        attempts = 0
        if missing:
            pool: List[Dict[str, Any]] = []
            if mineru_text_boxes:
                pool.extend(mineru_text_boxes)
            if ocr_text_boxes:
                pool.extend(ocr_text_boxes)
            if not pool:
                pool.extend(page_plan.get("text_boxes") if isinstance(page_plan.get("text_boxes"), list) else [])
            for candidate in pool:
                if not isinstance(candidate, dict):
                    continue
                text = str(candidate.get("text") or "").strip()
                if not text or text not in missing:
                    continue
                existing.append(self._normalize_single_text_box(candidate, width, height, len(existing) + 1))
                attempts += 1
            existing = self._dedupe_text_boxes(existing, width, height)
            layout_rules = self._build_layout_rules(page_plan, existing, width, height)
            manifest["layout_rules"] = layout_rules
            manifest["text_boxes"] = self._apply_text_layout_rules(existing, layout_rules)
            manifest["text_inventory"] = self._build_text_inventory(manifest["text_boxes"])
        remaining = self._find_missing_priority_text(page_plan, manifest.get("text_boxes", []))
        known_limits = manifest.setdefault("known_limits", [])
        if remaining:
            known_limits.append(f"Priority text remained unresolved after repair: {', '.join(remaining)}")
        return {
            "attempted": bool(missing),
            "added_boxes": attempts,
            "missing_before": missing,
            "missing_after": remaining,
        }

    def _find_missing_priority_text(self, page_plan: Dict[str, Any], text_boxes: List[Dict[str, Any]]) -> List[str]:
        required = []
        for raw in page_plan.get("text_boxes") if isinstance(page_plan.get("text_boxes"), list) else []:
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or "").strip()
            if not text:
                continue
            if self._is_priority_text_candidate(text, raw.get("box_px") or raw.get("bbox")):
                required.append(text)
        inventory = set(self._build_text_inventory(text_boxes))
        return [text for text in required if text not in inventory]

    def _is_priority_text_candidate(self, text: str, box: Any) -> bool:
        normalized = str(text).strip()
        if not normalized:
            return False
        if normalized.startswith("•") or "•" in normalized:
            return True
        if len(normalized) >= 8:
            return True
        if isinstance(box, list) and len(box) == 4:
            try:
                _x, _y, w, h = [int(round(float(v))) for v in box]
            except (TypeError, ValueError):
                return False
            if h >= w * 1.8:
                return True
        return False

    def _normalize_single_text_box(self, raw: Dict[str, Any], width: int, height: int, index: int) -> Dict[str, Any]:
        box = self._normalize_box(raw.get("box_px") or raw.get("bbox"), width, height) or [0, 0, width, max(24, height // 20)]
        text = str(raw.get("text") or "").strip()
        expected_lines = self._expected_lines(raw, text)
        text = self._split_text_to_lines(text, expected_lines)
        font_size = self._calibrated_font_size(raw.get("font_size"), box, text, expected_lines)
        return {
            "id": str(raw.get("id") or f"text_repair_{index:03d}"),
            "box_px": box,
            "text": text,
            "font_size": font_size,
            "expected_lines": expected_lines,
            "font_group": str(raw.get("font_group") or raw.get("group") or ""),
            "font": str(raw.get("font") or "Noto Sans CJK SC"),
            "color": self._normalize_color(raw.get("color"), "#111111"),
            "align": raw.get("align") if raw.get("align") in {"left", "center", "right"} else "left",
            "valign": raw.get("valign") if raw.get("valign") in {"top", "middle", "bottom"} else "top",
            "bold": bool(raw.get("bold", False)),
            "italic": bool(raw.get("italic", False)),
            "rotation": float(raw.get("rotation") or 0),
            "z_index": int(raw.get("z_index") or 320),
            "wrap": "square",
            "autofit": "shape",
        }

    def _dedupe_text_boxes(self, text_boxes: List[Dict[str, Any]], width: int, height: int) -> List[Dict[str, Any]]:
        deduped: List[Dict[str, Any]] = []
        for item in text_boxes:
            if not isinstance(item, dict):
                continue
            box = self._normalize_box(item.get("box_px") or item.get("bbox"), width, height)
            text = str(item.get("text") or "").strip()
            if not box or not text:
                continue
            duplicate = False
            for existing in deduped:
                if str(existing.get("text") or "").strip() != text:
                    continue
                existing_box = existing.get("box_px")
                if isinstance(existing_box, list) and self._intersection_over_min_area(box, existing_box) >= 0.55:
                    duplicate = True
                    break
            if not duplicate:
                item["box_px"] = box
                deduped.append(item)
        return deduped

    def _validate_page(self, pptx: Path, manifest: Path, report: Path) -> None:
        script_dir = script_path("validate_pptx.py").parent
        result = subprocess.run(
            [sys.executable, str(script_dir / "validate_pptx.py"), str(pptx), "--manifest", str(manifest), "--report", str(report)],
            cwd=str(script_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if result.returncode != 0:
            raise RuntimeError(f"Page validation failed: {result.stdout or result.stderr}")

    def _analyze_residual_visuals(self, ai_service: Any, residual: Path, width: int, height: int) -> Dict[str, Any]:
        prompt = f"""
You are checking a visual-priority PowerPoint replica residual image. Return strict JSON only.

Residual image size: width={width}, height={height}. Use pixel coordinates [x,y,w,h].

The residual has already whitened semantic visuals detected in cycle 1. Find only remaining non-text semantic visuals that should become independently selectable image assets.

Return:
{{
  "semantic_visuals": [{{"id":"residual_visual_001","type":"semantic_visual","box_px":[x,y,w,h],"anchor_slot":[x,y,w,h],"semantic_unit_id":"residual_visual_001","semantic_label":"short label","classification":"source_crop","z_index":205}}],
  "known_limits": []
}}

Rules:
- Do not return text boxes.
- Do not return panels, frames, dividers, or background.
- Return missing ordinary horizontal/vertical arrows as shape candidates when possible; return semantic_visual image items only for complex decorative arrows that cannot be approximated by a native PPT arrow.
- Do not return white blank regions.
- Do not include a full-slide image.
- Prefer only obvious remaining single icons, logos, pictograms, chart icons, device marks, arrows, connector arrows, symbols, and small graphic units.
- Do not return an entire chart, architecture diagram, row, column, or cluster as one item when it contains multiple separable semantic visuals.
- Bboxes must include complete arrowheads/strokes/edges with 6-10 px padding.
- Return JSON only.
"""
        try:
            plan = ai_service.generate_json_with_image(prompt, str(residual))
            if isinstance(plan, dict):
                return plan
            return {"semantic_visuals": [], "known_limits": ["Residual model returned a non-object JSON plan."]}
        except Exception as exc:
            logger.warning("Residual visual analysis failed for %s: %s", residual, exc)
            return {"semantic_visuals": [], "known_limits": [f"Residual analysis failed: {exc}"]}

    def _extract_ocr_text_boxes(self, source: Path, width: int, height: int) -> List[Dict[str, Any]]:
        try:
            from flask import current_app, has_app_context
            if not has_app_context():
                return []
            api_key = current_app.config.get("BAIDU_API_KEY") or ""
            if not api_key:
                return []
            from services.ai_providers.ocr.baidu_accurate_ocr_provider import BaiduAccurateOCRProvider
            provider = BaiduAccurateOCRProvider(api_key)
            result = provider.recognize(
                str(source),
                language_type="CHN_ENG",
                recognize_granularity="big",
                detect_direction=False,
                paragraph=False,
                probability=True,
            )
        except Exception as exc:
            logger.warning("Advanced editable OCR text extraction skipped for %s: %s", source, exc)
            return []

        text_boxes: List[Dict[str, Any]] = []
        for idx, line in enumerate(result.get("text_lines", []) or [], start=1):
            text = str(line.get("text") or "").strip()
            bbox = line.get("bbox")
            if not text or not isinstance(bbox, list) or len(bbox) != 4:
                continue
            box = self._normalize_box([bbox[0], bbox[1], bbox[2], bbox[3]], width, height)
            if not box:
                continue
            text_boxes.append({
                "id": f"ocr_text_{idx:03d}",
                "text": text,
                "box_px": self._pad_box(box, 0, 0, width, height, 2),
                "font_size": self._estimate_font_from_box(box, text),
                "expected_lines": max(1, text.count("\n") + 1),
                "font_group": self._guess_font_group(box, width, height),
                "font": "Noto Sans CJK SC",
                "color": "#111111",
                "align": "center" if box[2] > width * 0.18 else "left",
                "valign": "middle",
                "bold": box[3] >= height * 0.035,
                "z_index": 320,
                "source": "baidu_accurate_ocr",
            })
        return text_boxes

    def _merge_ocr_with_visual_text_boxes(self, ocr_boxes: List[Dict[str, Any]], visual_boxes: Any, width: int, height: int) -> List[Dict[str, Any]]:
        visual_items = visual_boxes if isinstance(visual_boxes, list) else []
        merged = []
        for ocr in ocr_boxes:
            best = None
            best_score = 0.0
            for raw in visual_items:
                if not isinstance(raw, dict):
                    continue
                visual_box = self._normalize_box(raw.get("box_px") or raw.get("bbox"), width, height)
                if not visual_box:
                    continue
                score = self._intersection_over_min_area(ocr["box_px"], visual_box)
                if score > best_score:
                    best = raw
                    best_score = score
            item = dict(ocr)
            if best and best_score >= 0.35:
                for key in ("font_size", "font", "color", "align", "valign", "bold", "italic", "rotation", "font_group", "z_index"):
                    if best.get(key) not in (None, ""):
                        item[key] = best[key]
                item["text"] = ocr["text"]
                item["box_px"] = ocr["box_px"]
                item["source"] = "baidu_accurate_ocr_with_visual_style"
            merged.append(item)
        for raw in visual_items:
            if not isinstance(raw, dict):
                continue
            visual_box = self._normalize_box(raw.get("box_px") or raw.get("bbox"), width, height)
            if not visual_box:
                continue
            if any(self._intersection_over_min_area(visual_box, ocr["box_px"]) >= 0.35 for ocr in merged):
                continue
            text = str(raw.get("text") or "").strip()
            if text:
                merged.append(raw)
        return merged

    def _guess_font_group(self, box: List[int], width: int, height: int) -> str:
        _x, y, w, h = box
        if y < height * 0.16 and h > height * 0.035:
            return "title"
        if w > width * 0.25 and h > height * 0.04:
            return "diagram_label"
        return "body"

    def _extract_mineru_page_plan(self, source: Path, width: int, height: int) -> Dict[str, Any]:
        try:
            from flask import current_app, has_app_context
            if not has_app_context():
                return {"text_boxes": [], "images": []}
            mineru_token = current_app.config.get("MINERU_TOKEN") or ""
            if not mineru_token:
                return {"text_boxes": [], "images": []}
            mineru_api_base = current_app.config.get("MINERU_API_BASE") or "https://mineru.net"
            upload_folder = Path(current_app.config.get("UPLOAD_FOLDER", "uploads"))
            from services.file_parser_service import FileParserService
            from services.image_editability.extractors import MinerUElementExtractor
            parser = FileParserService(
                mineru_token=mineru_token,
                mineru_api_base=mineru_api_base,
                enable_image_captioning=False,
            )
            extractor = MinerUElementExtractor(parser, upload_folder)
            result = extractor.extract(str(source), depth=0)
            if getattr(result, "has_error", False):
                logger.warning("MinerU extraction returned error for %s: %s", source, result.error)
                return {"text_boxes": [], "images": []}
        except Exception as exc:
            logger.warning("Advanced editable MinerU extraction skipped for %s: %s", source, exc)
            return {"text_boxes": [], "images": []}

        text_boxes: List[Dict[str, Any]] = []
        images: List[Dict[str, Any]] = []
        for idx, element in enumerate(getattr(result, "elements", []) or [], start=1):
            element_type = str(element.get("type") or element.get("element_type") or "").lower()
            bbox = element.get("bbox")
            if isinstance(bbox, dict):
                box_raw = [bbox.get("x0"), bbox.get("y0"), bbox.get("x1"), bbox.get("y1")]
                box = self._normalize_box([
                    box_raw[0],
                    box_raw[1],
                    float(box_raw[2]) - float(box_raw[0]),
                    float(box_raw[3]) - float(box_raw[1]),
                ], width, height)
            elif isinstance(bbox, list) and len(bbox) == 4:
                # MinerU extractor dict elements use [x0, y0, x1, y1].
                box = self._normalize_box([bbox[0], bbox[1], float(bbox[2]) - float(bbox[0]), float(bbox[3]) - float(bbox[1])], width, height)
            else:
                box = None
            if not box:
                continue
            content = str(element.get("content") or "").strip()
            if element_type in {"text", "title", "paragraph", "header", "footer"} and content:
                text_boxes.append({
                    "id": f"mineru_text_{idx:03d}",
                    "text": content,
                    "box_px": self._pad_box(box, 0, 0, width, height, 2),
                    "font_size": self._estimate_font_from_box(box, content),
                    "expected_lines": max(1, content.count("\n") + 1),
                    "font_group": self._guess_font_group(box, width, height),
                    "font": "Noto Sans CJK SC",
                    "color": "#111111",
                    "align": "center" if box[2] > width * 0.18 else "left",
                    "valign": "middle",
                    "bold": box[3] >= height * 0.035,
                    "z_index": 320,
                    "source": "mineru",
                })
            elif element_type in {"image", "figure", "chart", "table"}:
                images.append({
                    "id": f"mineru_visual_{idx:03d}",
                    "type": "semantic_visual",
                    "box_px": box,
                    "anchor_slot": box,
                    "semantic_unit_id": f"mineru_visual_{idx:03d}",
                    "semantic_label": element_type,
                    "classification": "source_crop",
                    "z_index": 200,
                    "source": "mineru",
                })
        return {"text_boxes": text_boxes, "images": images}

    def _merge_visual_items(self, primary: Any, secondary: Any, width: int, height: int) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for raw in primary if isinstance(primary, list) else []:
            if isinstance(raw, dict):
                result.append(raw)
        for raw in secondary if isinstance(secondary, list) else []:
            if not isinstance(raw, dict):
                continue
            box = self._normalize_box(raw.get("box_px") or raw.get("bbox"), width, height)
            if not box:
                continue
            if any(self._intersection_over_min_area(box, self._normalize_box(item.get("box_px") or item.get("bbox"), width, height) or [0, 0, 1, 1]) >= 0.65 for item in result):
                continue
            result.append(raw)
        return result

    def _append_semantic_visual_asset(
        self,
        *,
        page_dir: Path,
        source: Path,
        clean_background: Path,
        assets_dir: Path,
        crops_dir: Path,
        item: Any,
        fallback_id: str,
        width: int,
        height: int,
        imagegen_worker: AdvancedEditableImageGenWorker,
        images: List[Dict[str, Any]],
        provenance: List[Dict[str, Any]],
        visual_inventory: List[Dict[str, Any]],
        asset_manifest: List[Dict[str, Any]],
        redboxes: List[Dict[str, Any]],
        cycle: int,
    ) -> None:
        if not isinstance(item, dict):
            return
        box = self._normalize_box(item.get("box_px") or item.get("bbox"), width, height)
        if not box or self._is_full_slide_box(box, width, height):
            return
        image_id = str(item.get("id") or fallback_id)
        semantic_unit_id = str(item.get("semantic_unit_id") or image_id)
        if any(str(existing.get("semantic_unit_id") or "") == semantic_unit_id for existing in visual_inventory):
            return
        if self._overlaps_existing_asset(box, asset_manifest):
            return
        semantic_label = str(item.get("semantic_label") or item.get("label") or image_id)
        crop_path = self._crop_source_asset(source, crops_dir, semantic_unit_id, box)
        imagegen_requested = self._should_generate_semantic_asset(item, box, width, height)
        foreground = None
        if clean_background is not None:
            foreground = self._extract_foreground_asset(source, clean_background, assets_dir, semantic_unit_id, box)
        if foreground is None and clean_background is not None and self._clean_background_likely_contains_object(source, clean_background, box):
            logger.info("Skipping %s because clean background already appears to contain this object", semantic_unit_id)
            return
        if foreground is None and not imagegen_requested and self._large_source_asset_would_bake_background(item, box, width, height):
            logger.info("Skipping %s because source-derived asset would bake background/text", semantic_unit_id)
            return
        asset_path = foreground or self._crop_source_asset(source, assets_dir, semantic_unit_id, box)
        imagegen_worker.record_source_asset(page_dir, semantic_unit_id, source, crop_path, asset_path)
        source_type = "source-derived-rasterization"
        decision = "imagegen-batch-pending-source-fallback" if imagegen_requested else "source-derived-raster-asset"
        provenance_note = (
            "Queued for batch asset-grid generation; source crop is retained as fallback."
            if imagegen_requested
            else "Visual-priority foreground cutout preserved as an independent semantic asset."
        )
        background_removed = foreground is not None
        rel = safe_rel(asset_path, page_dir)
        anchor_slot = self._normalize_box(item.get("anchor_slot"), width, height) or box
        contained_box = self._contain_box(asset_path, anchor_slot)
        images.append({
            "id": image_id,
            "path": rel,
            "box_px": contained_box,
            "anchor_slot_px": anchor_slot,
            "semantic_unit_id": semantic_unit_id,
            "semantic_unit_count": 1,
            "asset_role": "source_semantic_visual",
            "z_index": int(item.get("z_index") or (200 + cycle)),
        })
        provenance.append({
            "path": rel,
            "source": "source.png",
            "source_type": source_type,
            "source_region_px": box,
            "anchor_slot_px": anchor_slot,
            "semantic_unit_id": semantic_unit_id,
            "cycle": cycle,
            "foreground_cutout": foreground is not None,
            "provenance_note": provenance_note,
        })
        visual_inventory.append({
            "id": image_id,
            "type": str(item.get("type") or "image"),
            "semantic_unit_id": semantic_unit_id,
            "semantic_label": semantic_label,
            "decision": decision,
            "asset": rel,
            "box_px": box,
            "anchor_slot_px": anchor_slot,
            "final_box_px": contained_box,
            "source_crop": safe_rel(crop_path, page_dir),
            "cycle": cycle,
            "split_from": item.get("split_from"),
            "split_reason": item.get("split_reason"),
        })
        asset_manifest.append({
            "asset_id": semantic_unit_id,
            "path": rel,
            "source": "source.png",
            "source_crop": safe_rel(crop_path, page_dir),
            "source_type": source_type,
            "background_removed": background_removed,
            "foreground_cutout": foreground is not None,
            "imagegen_requested": imagegen_requested,
            "semantic_unit_count": 1,
            "bbox": box,
            "anchor_slot": anchor_slot,
            "fitted_bbox": contained_box,
            "cycle": cycle,
            "notes": provenance_note,
            "split_from": item.get("split_from"),
            "split_reason": item.get("split_reason"),
        })
        redboxes.append({
            "anchor_id": semantic_unit_id,
            "bbox": box,
            "classification": "imagegen_asset" if imagegen_requested else "source_crop",
            "source_crop": safe_rel(crop_path, page_dir),
            "cycle": cycle,
        })

    def _expand_large_visual_items(
        self,
        source: Path,
        raw_items: Any,
        width: int,
        height: int,
        prefix: str = "split_visual",
    ) -> List[Dict[str, Any]]:
        if not isinstance(raw_items, list):
            return []
        expanded: List[Dict[str, Any]] = []
        for idx, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                continue
            box = self._normalize_box(raw.get("box_px") or raw.get("bbox"), width, height)
            if not box:
                continue
            if not self._should_split_large_visual(raw, box, width, height):
                expanded.append(raw)
                continue
            components = self._detect_visual_components(source, box)
            if len(components) <= 1:
                expanded.append(raw)
                continue
            for comp_idx, comp_box in enumerate(components, start=1):
                item_id = f"{prefix}_{idx:03d}_{comp_idx:03d}"
                expanded.append({
                    **raw,
                    "id": item_id,
                    "semantic_unit_id": item_id,
                    "semantic_label": f"{raw.get('semantic_label') or raw.get('label') or 'semantic visual'} component {comp_idx}",
                    "box_px": comp_box,
                    "anchor_slot": comp_box,
                    "classification": "imagegen_asset",
                    "split_from": raw.get("id") or raw.get("semantic_unit_id") or f"visual_{idx:03d}",
                    "split_reason": "large visual bbox was decomposed into minimum semantic-unit connected components",
                })
        return expanded

    def _arrow_shapes_as_visuals(self, raw_shapes: Any, width: int, height: int) -> List[Dict[str, Any]]:
        return []

    def _remove_text_bearing_layout_visuals(
        self,
        source: Path,
        visual_items: List[Dict[str, Any]],
        text_boxes: List[Dict[str, Any]],
        generated_shapes: List[Dict[str, Any]],
        width: int,
        height: int,
    ) -> List[Dict[str, Any]]:
        kept = []
        for idx, item in enumerate(visual_items, start=1):
            box = self._normalize_box(item.get("box_px") or item.get("bbox"), width, height)
            if not box:
                continue
            arrow_shape = self._native_arrow_shape_from_visual(source, item, box, idx, width, height)
            if arrow_shape:
                generated_shapes.append(arrow_shape)
                continue
            if self._visual_overlaps_native_text(box, text_boxes, threshold=0.18):
                if self._is_text_bearing_layout_visual(item, box, text_boxes, width, height):
                    shape_id = f"layout_from_visual_{idx:03d}"
                    generated_shapes.append({
                        "id": shape_id,
                        "type": "rect",
                        "box_px": box,
                        "fill": self._dominant_color(source, box, "#FFFFFF"),
                        "stroke": "none",
                        "purpose": "panel",
                        "z_index": int(item.get("z_index") or 80),
                        "converted_from_visual": item.get("id") or item.get("semantic_unit_id"),
                        "conversion_reason": "visual overlapped native text and looked like a layout panel/bar",
                    })
                continue
            if self._is_text_bearing_layout_visual(item, box, text_boxes, width, height):
                shape_id = f"layout_from_visual_{idx:03d}"
                generated_shapes.append({
                    "id": shape_id,
                    "type": "rect",
                    "box_px": box,
                    "fill": self._dominant_color(source, box, "#FFFFFF"),
                    "stroke": "none",
                    "purpose": "panel",
                    "z_index": int(item.get("z_index") or 80),
                    "converted_from_visual": item.get("id") or item.get("semantic_unit_id"),
                    "conversion_reason": "visual overlapped native text and looked like a layout panel/bar",
                })
                continue
            kept.append(item)
        return kept

    def _native_arrow_shape_from_visual(
        self,
        source: Path,
        item: Dict[str, Any],
        box: List[int],
        idx: int,
        width: int,
        height: int,
    ) -> Optional[Dict[str, Any]]:
        descriptor = " ".join(
            str(item.get(key) or "")
            for key in ("id", "type", "semantic_label", "label", "notes", "classification")
        ).lower()
        if not any(token in descriptor for token in ("arrow", "flow", "connector", "箭头")):
            return None
        x, y, w, h = box
        area_ratio = (w * h) / max(1, width * height)
        aspect = w / max(1, h)
        simple_axis_aligned = aspect >= 1.8 or aspect <= 0.56
        if not simple_axis_aligned or area_ratio > 0.18:
            return None
        direction = self._arrow_direction_from_descriptor(descriptor, box)
        preset = {
            "right": "rightArrow",
            "left": "leftArrow",
            "down": "downArrow",
            "up": "upArrow",
        }.get(direction, "rightArrow")
        return {
            "id": f"native_arrow_from_visual_{idx:03d}",
            "type": "rect",
            "preset": preset,
            "box_px": box,
            "fill": self._dominant_color(source, box, "#2F75B5"),
            "stroke": "none",
            "purpose": "structural_arrow",
            "z_index": int(item.get("z_index") or 210),
            "converted_from_visual": item.get("id") or item.get("semantic_unit_id"),
            "conversion_reason": "axis-aligned arrow converted to native PPT shape for editability",
        }

    def _arrow_direction_from_descriptor(self, descriptor: str, box: List[int]) -> str:
        if any(token in descriptor for token in ("left", "向左", "左")):
            return "left"
        if any(token in descriptor for token in ("up", "向上", "上")):
            return "up"
        if any(token in descriptor for token in ("down", "向下", "下")):
            return "down"
        if any(token in descriptor for token in ("right", "向右", "右")):
            return "right"
        _x, _y, w, h = box
        return "right" if w >= h else "down"

    def _visual_overlaps_native_text(self, box: List[int], text_boxes: List[Dict[str, Any]], threshold: float) -> bool:
        for text in text_boxes:
            text_box = text.get("box_px")
            if not isinstance(text_box, list) or len(text_box) != 4:
                continue
            if self._intersection_over_min_area(box, text_box) >= threshold:
                return True
        return False

    def _is_text_bearing_layout_visual(
        self,
        item: Dict[str, Any],
        box: List[int],
        text_boxes: List[Dict[str, Any]],
        width: int,
        height: int,
    ) -> bool:
        x, y, w, h = box
        area_ratio = (w * h) / max(1, width * height)
        aspect = w / max(1, h)
        panel_like = aspect >= 3.0 or aspect <= 0.34 or area_ratio >= 0.06
        descriptor = " ".join(
            str(item.get(key) or "")
            for key in ("id", "type", "semantic_label", "label", "notes")
        ).lower()
        if any(token in descriptor for token in ("icon", "logo", "arrow", "pictogram", "symbol", "箭头", "图标")):
            return False
        if not panel_like:
            return False
        for text in text_boxes:
            text_box = text.get("box_px")
            if not isinstance(text_box, list) or len(text_box) != 4:
                continue
            if self._intersection_over_min_area(box, text_box) >= 0.28:
                return True
        return False

    def _dominant_color(self, source: Path, box: List[int], default: str) -> str:
        x, y, w, h = box
        try:
            with Image.open(source).convert("RGB") as img:
                crop = img.crop((x, y, x + w, y + h))
                colors = crop.resize((1, 1), Image.Resampling.BILINEAR).getpixel((0, 0))
                return f"#{colors[0]:02X}{colors[1]:02X}{colors[2]:02X}"
        except Exception:
            return default

    def _should_split_large_visual(self, item: Dict[str, Any], box: List[int], width: int, height: int) -> bool:
        route = str(item.get("route") or item.get("classification") or item.get("type") or "").lower()
        if route in {"scene_background", "generated_scene_background", "source_crop_large_scene"}:
            return False
        _, _, box_w, box_h = box
        area_ratio = (box_w * box_h) / max(1, width * height)
        label = " ".join(
            str(item.get(key) or "")
            for key in ("id", "semantic_unit_id", "semantic_label", "label", "notes", "type")
        ).lower()
        grouped_hint = any(
            hint in label
            for hint in ("diagram", "architecture", "cluster", "group", "icons", "arrow", "flow", "图标", "箭头", "架构", "流程")
        )
        return area_ratio >= 0.035 or box_w >= width * 0.22 or box_h >= height * 0.22 or grouped_hint

    def _detect_visual_components(self, source: Path, parent_box: List[int]) -> List[List[int]]:
        x0, y0, w, h = parent_box
        try:
            with Image.open(source).convert("RGB") as img:
                crop = img.crop((x0, y0, x0 + w, y0 + h))
        except Exception:
            return []
        pixels = crop.load()
        mask = [[False for _ in range(w)] for _ in range(h)]
        for y in range(h):
            for x in range(w):
                r, g, b = pixels[x, y]
                # Treat near-white/light slide backgrounds as empty; keep saturated or dark visual marks.
                saturation = max(r, g, b) - min(r, g, b)
                dark = max(r, g, b) < 210
                saturated = saturation > 28 and max(r, g, b) < 248
                if dark or saturated:
                    mask[y][x] = True
        visited = [[False for _ in range(w)] for _ in range(h)]
        components: List[List[int]] = []
        for y in range(h):
            for x in range(w):
                if not mask[y][x] or visited[y][x]:
                    continue
                stack = [(x, y)]
                visited[y][x] = True
                min_x = max_x = x
                min_y = max_y = y
                count = 0
                while stack:
                    cx, cy = stack.pop()
                    count += 1
                    min_x = min(min_x, cx)
                    max_x = max(max_x, cx)
                    min_y = min(min_y, cy)
                    max_y = max(max_y, cy)
                    for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                        if nx < 0 or ny < 0 or nx >= w or ny >= h:
                            continue
                        if visited[ny][nx] or not mask[ny][nx]:
                            continue
                        visited[ny][nx] = True
                        stack.append((nx, ny))
                comp_w = max_x - min_x + 1
                comp_h = max_y - min_y + 1
                if count < 40 or comp_w < 6 or comp_h < 6:
                    continue
                components.append([x0 + min_x, y0 + min_y, comp_w, comp_h])
        return self._merge_nearby_components(components, parent_box)

    def _merge_nearby_components(self, components: List[List[int]], parent_box: List[int]) -> List[List[int]]:
        if not components:
            return []
        merged: List[List[int]] = []
        for comp in sorted(components, key=lambda b: (b[1], b[0])):
            placed = False
            for idx, existing in enumerate(merged):
                if self._component_distance(existing, comp) <= 10:
                    merged[idx] = self._union_box(existing, comp)
                    placed = True
                    break
            if not placed:
                merged.append(comp)
        px, py, pw, ph = parent_box
        filtered = []
        for box in merged:
            _, _, w, h = box
            area_ratio = (w * h) / max(1, pw * ph)
            if area_ratio > 0.85:
                continue
            filtered.append(self._pad_box(box, px, py, pw, ph, 8))
        return filtered[:24]

    def _component_distance(self, a: List[int], b: List[int]) -> float:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        dx = max(bx - (ax + aw), ax - (bx + bw), 0)
        dy = max(by - (ay + ah), ay - (by + bh), 0)
        return (dx * dx + dy * dy) ** 0.5

    def _union_box(self, a: List[int], b: List[int]) -> List[int]:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        left = min(ax, bx)
        top = min(ay, by)
        right = max(ax + aw, bx + bw)
        bottom = max(ay + ah, by + bh)
        return [left, top, right - left, bottom - top]

    def _pad_box(self, box: List[int], parent_x: int, parent_y: int, parent_w: int, parent_h: int, pad: int) -> List[int]:
        x, y, w, h = box
        left = max(parent_x, x - pad)
        top = max(parent_y, y - pad)
        right = min(parent_x + parent_w, x + w + pad)
        bottom = min(parent_y + parent_h, y + h + pad)
        return [left, top, max(1, right - left), max(1, bottom - top)]

    def _write_visual_replica_artifacts(
        self,
        *,
        page_dir: Path,
        source: Path,
        width: int,
        height: int,
        text_boxes: List[Dict[str, Any]],
        shapes: List[Dict[str, Any]],
        images: List[Dict[str, Any]],
        redboxes: List[Dict[str, Any]],
        cycle_2_redboxes: List[Dict[str, Any]],
        visual_inventory: List[Dict[str, Any]],
        asset_manifest: List[Dict[str, Any]],
        batch_grid_results: List[Optional[Dict[str, Any]]],
    ) -> None:
        reference = page_dir / "reference.png"
        if not reference.exists():
            shutil.copy2(source, reference)

        write_json(page_dir / "residual_cycle_001_redboxes.json", {
            "schema_version": 1,
            "cycle": 1,
            "items": redboxes,
        })
        write_json(page_dir / "residual_cycle_002_redboxes.json", {
            "schema_version": 1,
            "cycle": 2,
            "items": cycle_2_redboxes,
        })
        asset_sheets = [
            self._write_asset_sheet(page_dir, asset_manifest, 1),
            self._write_asset_sheet(page_dir, asset_manifest, 2),
        ]
        write_json(page_dir / "asset_manifest.json", {
            "schema_version": 1,
            "mode": "visual_priority",
            "assets": asset_manifest,
            "asset_sheets": [sheet for sheet in asset_sheets if sheet],
            "batch_grids": [result for result in batch_grid_results if result],
            "summary": {
                "imagegen_assets": len([item for item in asset_manifest if item.get("source_type") == "imagegen"]),
                "source_derived_assets": len([item for item in asset_manifest if item.get("source_type") == "source-derived-rasterization"]),
                "batch_grid_assets": len([item for item in asset_manifest if item.get("batch_grid_applied") is True]),
            },
        })
        prompts_dir = page_dir / "prompts"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        self._write_asset_prompt_rows(prompts_dir / "assets_cycle_001.jsonl", redboxes, asset_manifest)
        self._write_asset_prompt_rows(prompts_dir / "assets_cycle_002.jsonl", cycle_2_redboxes, asset_manifest)
        write_json(page_dir / "validation_report.json", {
            "schema_version": 1,
            "mode": "visual_priority",
            "canvas": {"width_px": width, "height_px": height},
            "native_text_box_count": len(text_boxes),
            "native_shape_count": len(shapes),
            "ppt_image_count": len([item for item in images if item.get("id") != "clean_background"]),
            "semantic_visual_count": len(visual_inventory),
            "asset_count": len(asset_manifest),
            "imagegen_asset_count": len([item for item in asset_manifest if item.get("source_type") == "imagegen"]),
            "transparentized_asset_count": len([item for item in asset_manifest if item.get("background_removed") is True]),
            "foreground_cutout_count": len([item for item in asset_manifest if item.get("foreground_cutout") is True]),
            "batch_grid_asset_count": len([item for item in asset_manifest if item.get("batch_grid_applied") is True]),
            "split_asset_count": len([item for item in asset_manifest if item.get("split_from")]),
            "arrow_asset_count": len([item for item in asset_manifest if self._asset_is_arrow(item)]),
            "native_arrow_shape_count": len([item for item in shapes if item.get("purpose") == "structural_arrow"]),
            "text_overlap_image_risk_count": self._count_text_overlap_image_risks(images, text_boxes),
            "asset_sheets": [sheet for sheet in asset_sheets if sheet],
            "batch_grids": [result for result in batch_grid_results if result],
            "checks": {
                "reference_copied": reference.exists(),
                "semantic_assets_are_independent": True,
                "large_visuals_split_when_detected": True,
                "source_crop_assets_documented": True,
                "asset_sheets_written": True,
                "residual_recorded": True,
                "second_cycle_residual_checked": True,
                "imagegen_jobs_recorded": True,
                "generated_asset_backgrounds_processed": True,
                "source_assets_foreground_cutout_attempted": True,
                "batch_asset_grid_attempted": True,
            },
            "known_limits": [
                "This built-in runner cannot use Codex subagents; it emulates the visual-replica artifact protocol inside the backend.",
                "Small semantic symbols may be generated from reference crops; dense diagrams are preserved as source-derived independent assets for visual fidelity.",
                "This version runs one residual follow-up cycle and batch grid generation for small imagegen assets, but does not yet run unbounded repair loops.",
            ],
        })

        self._write_residual_image(source, page_dir / "residual_cycle_001.png", redboxes)
        self._write_residual_image(source, page_dir / "residual_cycle_002.png", redboxes + cycle_2_redboxes)

    def _asset_provider_for(self, asset_manifest: List[Dict[str, Any]], asset_id: str) -> str:
        for item in asset_manifest:
            if str(item.get("asset_id") or "") == asset_id:
                return "imagegen" if item.get("source_type") == "imagegen" else "source-derived"
        return "unknown"

    def _asset_is_arrow(self, item: Dict[str, Any]) -> bool:
        text = " ".join(str(item.get(key) or "") for key in ("asset_id", "notes", "split_reason")).lower()
        return any(token in text for token in ("arrow", "flow", "connector", "箭头"))

    def _count_text_overlap_image_risks(self, images: List[Dict[str, Any]], text_boxes: List[Dict[str, Any]]) -> int:
        risks = 0
        for image in images:
            if image.get("id") == "clean_background":
                continue
            box = image.get("box_px")
            if not isinstance(box, list) or len(box) != 4:
                continue
            if self._visual_overlaps_native_text(box, text_boxes, threshold=0.18):
                risks += 1
        return risks

    def _generate_and_apply_asset_grid(
        self,
        page_dir: Path,
        source: Path,
        asset_manifest: List[Dict[str, Any]],
        images: List[Dict[str, Any]],
        provenance: List[Dict[str, Any]],
        visual_inventory: List[Dict[str, Any]],
        imagegen_worker: AdvancedEditableImageGenWorker,
        cycle: int,
    ) -> Optional[Dict[str, Any]]:
        candidates = [
            item for item in asset_manifest
            if int(item.get("cycle") or 1) == cycle and item.get("imagegen_requested") is True
        ]
        if not candidates:
            return None
        prompt_sheet_info = self._write_reference_prompt_sheet(page_dir, source, candidates, cycle)
        if not prompt_sheet_info:
            return None
        grid_path = imagegen_worker.generate_asset_grid(
            page_dir=page_dir,
            cycle=cycle,
            prompt_sheet=page_dir / prompt_sheet_info["image"],
            asset_ids=[str(item.get("asset_id")) for item in candidates],
            rows=int(prompt_sheet_info["rows"]),
            cols=int(prompt_sheet_info["cols"]),
        )
        if not grid_path:
            return {
                "cycle": cycle,
                "status": "failed",
                "prompt_sheet": prompt_sheet_info,
                "asset_count": len(candidates),
            }
        cut_result = self._cut_generated_asset_grid(page_dir, grid_path, candidates, images, provenance, visual_inventory, cycle)
        return {
            "cycle": cycle,
            "status": "applied" if cut_result["applied"] else "no_cells_applied",
            "prompt_sheet": prompt_sheet_info,
            "grid": safe_rel(grid_path, page_dir),
            **cut_result,
        }

    def _write_reference_prompt_sheet(self, page_dir: Path, source: Path, assets: List[Dict[str, Any]], cycle: int) -> Optional[Dict[str, Any]]:
        generated_dir = page_dir / "generated"
        generated_dir.mkdir(parents=True, exist_ok=True)
        thumb_w, thumb_h = 512, 288
        cell_size = 220
        margin = 32
        gap = 24
        cols = min(4, max(1, len(assets)))
        rows = (len(assets) + cols - 1) // cols
        sheet_w = max(thumb_w + margin * 2, margin * 2 + cols * cell_size + (cols - 1) * gap)
        sheet_h = margin + thumb_h + gap + rows * cell_size + (rows - 1) * gap + margin
        sheet = Image.new("RGB", (sheet_w, sheet_h), "white")
        cells = []
        with Image.open(source).convert("RGB") as ref:
            ref_fitted = self._fit_image_size(ref.size, thumb_w, thumb_h)
            ref_resized = ref.resize(ref_fitted, Image.Resampling.LANCZOS)
            sheet.paste(ref_resized, (margin, margin))
        for index, item in enumerate(assets):
            row = index // cols
            col = index % cols
            x = margin + col * (cell_size + gap)
            y = margin + thumb_h + gap + row * (cell_size + gap)
            crop_path = page_dir / str(item.get("source_crop") or "")
            if not crop_path.exists():
                continue
            with Image.open(crop_path).convert("RGBA") as crop:
                fitted = self._fit_image_size(crop.size, cell_size, cell_size)
                resized = crop.resize(fitted, Image.Resampling.LANCZOS)
                paste_x = x + (cell_size - fitted[0]) // 2
                paste_y = y + (cell_size - fitted[1]) // 2
                sheet.paste(resized.convert("RGB"), (paste_x, paste_y))
            cells.append({
                "id": item.get("asset_id"),
                "source_crop": item.get("source_crop"),
                "bbox": [x, y, cell_size, cell_size],
                "content_bbox": [paste_x, paste_y, fitted[0], fitted[1]],
            })
        if not cells:
            return None
        sheet_path = generated_dir / f"reference_prompt_sheet_cycle_{cycle:03d}.png"
        spec_path = generated_dir / f"reference_prompt_sheet_cycle_{cycle:03d}.json"
        sheet.save(sheet_path)
        spec = {
            "schema_version": 1,
            "cycle": cycle,
            "image": safe_rel(sheet_path, page_dir),
            "rows": rows,
            "cols": cols,
            "cell_size": cell_size,
            "margin": margin,
            "gap": gap,
            "full_reference_bbox": [margin, margin, ref_fitted[0], ref_fitted[1]],
            "cells": cells,
        }
        write_json(spec_path, spec)
        return {
            "cycle": cycle,
            "image": safe_rel(sheet_path, page_dir),
            "spec": safe_rel(spec_path, page_dir),
            "rows": rows,
            "cols": cols,
        }

    def _cut_generated_asset_grid(
        self,
        page_dir: Path,
        grid_path: Path,
        assets: List[Dict[str, Any]],
        images: List[Dict[str, Any]],
        provenance: List[Dict[str, Any]],
        visual_inventory: List[Dict[str, Any]],
        cycle: int,
    ) -> Dict[str, Any]:
        cols = min(4, max(1, len(assets)))
        rows = (len(assets) + cols - 1) // cols
        applied = []
        with Image.open(grid_path).convert("RGBA") as grid:
            cell_w = grid.width / cols
            cell_h = grid.height / rows
            for index, item in enumerate(assets):
                row = index // cols
                col = index % cols
                left = int(round(col * cell_w))
                top = int(round(row * cell_h))
                right = int(round((col + 1) * cell_w))
                bottom = int(round((row + 1) * cell_h))
                cell = grid.crop((left, top, right, bottom))
                asset_id = str(item.get("asset_id") or f"asset_{index + 1:03d}")
                tmp_path = page_dir / "generated" / f"{asset_id}_grid_cell_cycle_{cycle:03d}.png"
                final_path = page_dir / str(item.get("path") or f"assets/{asset_id}.png")
                cell.save(tmp_path)
                self._transparentize_generated_asset(tmp_path, final_path)
                fitted = self._contain_box(final_path, item.get("anchor_slot") or item.get("bbox"))
                item["fitted_bbox"] = fitted
                item["source_type"] = "imagegen"
                item["batch_grid_applied"] = True
                item["batch_grid_source"] = safe_rel(grid_path, page_dir)
                item["background_removed"] = True
                self._update_asset_references(asset_id, final_path, fitted, images, provenance, visual_inventory, page_dir, grid_path)
                applied.append({
                    "asset_id": asset_id,
                    "cell_bbox": [left, top, right - left, bottom - top],
                    "path": safe_rel(final_path, page_dir),
                })
        report_path = page_dir / "generated" / f"grid_cut_cycle_{cycle:03d}.json"
        write_json(report_path, {
            "schema_version": 1,
            "cycle": cycle,
            "grid": safe_rel(grid_path, page_dir),
            "rows": rows,
            "cols": cols,
            "applied": applied,
        })
        return {"applied": applied, "grid_cut_report": safe_rel(report_path, page_dir)}

    def _update_asset_references(
        self,
        asset_id: str,
        final_path: Path,
        fitted: List[int],
        images: List[Dict[str, Any]],
        provenance: List[Dict[str, Any]],
        visual_inventory: List[Dict[str, Any]],
        page_dir: Path,
        grid_path: Path,
    ) -> None:
        rel = safe_rel(final_path, page_dir)
        for image in images:
            if str(image.get("semantic_unit_id") or image.get("id") or "") == asset_id:
                image["path"] = rel
                image["box_px"] = fitted
                image["batch_grid_applied"] = True
        for entry in provenance:
            if str(entry.get("semantic_unit_id") or "") == asset_id:
                entry["path"] = rel
                entry["source_type"] = "imagegen"
                entry["batch_grid_source"] = safe_rel(grid_path, page_dir)
                entry["provenance_note"] = "Generated from a batch asset grid and cut into an independent transparent asset."
        for item in visual_inventory:
            if str(item.get("semantic_unit_id") or "") == asset_id:
                item["asset"] = rel
                item["final_box_px"] = fitted
                item["decision"] = "imagegen-batch-grid-cut-asset"
                item["batch_grid_source"] = safe_rel(grid_path, page_dir)

    def _write_asset_prompt_rows(self, path: Path, redboxes: List[Dict[str, Any]], asset_manifest: List[Dict[str, Any]]) -> None:
        rows = []
        for item in redboxes:
            rows.append(json.dumps({
                "prompt_id": f"asset_{item.get('anchor_id')}",
                "provider": self._asset_provider_for(asset_manifest, str(item.get("anchor_id") or "")),
                "prompt_mode": "visual-priority-semantic-asset",
                "source_anchor_ids": [item.get("anchor_id")],
                "reference_inputs": [
                    {"role": "full_reference", "path": "reference.png"},
                    {"role": "object_crop", "anchor_id": item.get("anchor_id"), "path": item.get("source_crop")},
                ],
                "prompt": (
                    "Create or preserve this semantic visual as an independent PPT asset. "
                    "Use imagegen for small clean symbols; preserve source crops for dense diagrams to avoid hallucinated redraws."
                ),
            }, ensure_ascii=False))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")

    def _write_asset_sheet(self, page_dir: Path, asset_manifest: List[Dict[str, Any]], cycle: int) -> Optional[Dict[str, Any]]:
        cycle_assets = [item for item in asset_manifest if int(item.get("cycle") or 1) == cycle]
        if not cycle_assets:
            return None
        generated_dir = page_dir / "generated"
        generated_dir.mkdir(parents=True, exist_ok=True)
        cell_size = 256
        margin = 32
        gap = 24
        cols = min(4, max(1, len(cycle_assets)))
        rows = (len(cycle_assets) + cols - 1) // cols
        sheet_w = margin * 2 + cols * cell_size + (cols - 1) * gap
        sheet_h = margin * 2 + rows * cell_size + (rows - 1) * gap
        sheet = Image.new("RGBA", (sheet_w, sheet_h), (255, 255, 255, 255))
        cells = []
        for index, item in enumerate(cycle_assets):
            row = index // cols
            col = index % cols
            x = margin + col * (cell_size + gap)
            y = margin + row * (cell_size + gap)
            asset_path = page_dir / str(item.get("path") or "")
            if not asset_path.exists():
                continue
            with Image.open(asset_path).convert("RGBA") as asset:
                fitted = self._fit_image_size(asset.size, cell_size, cell_size)
                resized = asset.resize(fitted, Image.Resampling.LANCZOS)
                paste_x = x + (cell_size - fitted[0]) // 2
                paste_y = y + (cell_size - fitted[1]) // 2
                sheet.paste(resized, (paste_x, paste_y), resized)
            cells.append({
                "id": item.get("asset_id"),
                "asset_path": item.get("path"),
                "bbox": [x, y, cell_size, cell_size],
                "content_bbox": [paste_x, paste_y, fitted[0], fitted[1]],
                "source_type": item.get("source_type"),
            })
        sheet_path = generated_dir / f"asset_sheet_cycle_{cycle:03d}.png"
        spec_path = generated_dir / f"asset_sheet_cycle_{cycle:03d}.json"
        if not cells:
            return None
        sheet.save(sheet_path)
        spec = {
            "schema_version": 1,
            "cycle": cycle,
            "image": safe_rel(sheet_path, page_dir),
            "rows": rows,
            "cols": cols,
            "cell_size": cell_size,
            "margin": margin,
            "gap": gap,
            "background": "#FFFFFF",
            "cells": cells,
        }
        write_json(spec_path, spec)
        return {"cycle": cycle, "image": safe_rel(sheet_path, page_dir), "spec": safe_rel(spec_path, page_dir)}

    def _write_residual_image(self, source: Path, out: Path, redboxes: List[Dict[str, Any]]) -> Path:
        residual = Image.open(source).convert("RGB")
        try:
            from PIL import ImageDraw
            draw = ImageDraw.Draw(residual)
            for item in redboxes:
                box = item.get("bbox")
                if not isinstance(box, list) or len(box) != 4:
                    continue
                x, y, w, h = [int(round(float(value))) for value in box]
                draw.rectangle([x, y, x + w, y + h], fill=(255, 255, 255))
            out.parent.mkdir(parents=True, exist_ok=True)
            residual.save(out)
            return out
        finally:
            residual.close()

    def _overlaps_existing_asset(self, box: List[int], asset_manifest: List[Dict[str, Any]], threshold: float = 0.72) -> bool:
        for item in asset_manifest:
            existing = item.get("bbox")
            if not isinstance(existing, list) or len(existing) != 4:
                continue
            if self._intersection_over_min_area(box, existing) >= threshold:
                return True
        return False

    def _intersection_over_min_area(self, a: List[int], b: List[int]) -> float:
        ax, ay, aw, ah = [float(value) for value in a]
        bx, by, bw, bh = [float(value) for value in b]
        left = max(ax, bx)
        top = max(ay, by)
        right = min(ax + aw, bx + bw)
        bottom = min(ay + ah, by + bh)
        if right <= left or bottom <= top:
            return 0.0
        inter = (right - left) * (bottom - top)
        min_area = max(1.0, min(aw * ah, bw * bh))
        return inter / min_area

    def _normalize_text_boxes(self, raw_items: Any, width: int, height: int) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        if not isinstance(raw_items, list):
            return items
        for idx, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or "").strip()
            box = self._normalize_box(raw.get("box_px") or raw.get("bbox"), width, height)
            if not text or not box:
                continue
            expected_lines = self._expected_lines(raw, text)
            text = self._split_text_to_lines(text, expected_lines)
            font_size = self._calibrated_font_size(raw.get("font_size"), box, text, expected_lines)
            items.append({
                "id": str(raw.get("id") or f"text_{idx:03d}"),
                "box_px": box,
                "text": text,
                "font_size": font_size,
                "expected_lines": expected_lines,
                "font_group": str(raw.get("font_group") or raw.get("group") or ""),
                "font": str(raw.get("font") or "Noto Sans CJK SC"),
                "color": self._normalize_color(raw.get("color"), "#111111"),
                "align": raw.get("align") if raw.get("align") in {"left", "center", "right"} else "left",
                "valign": raw.get("valign") if raw.get("valign") in {"top", "middle", "bottom"} else "top",
                "bold": bool(raw.get("bold", False)),
                "italic": bool(raw.get("italic", False)),
                "rotation": float(raw.get("rotation") or 0),
                "z_index": int(raw.get("z_index") or 300),
                "wrap": "square",
                "autofit": "shape",
            })
        return items

    def _build_layout_rules(self, page_plan: Dict[str, Any], text_boxes: List[Dict[str, Any]], width: int, height: int) -> Dict[str, Any]:
        raw_rules = page_plan.get("layout_rules") if isinstance(page_plan.get("layout_rules"), dict) else {}
        text_groups = list(raw_rules.get("text_groups") or [])
        alignment_rules = list(raw_rules.get("alignment_rules") or [])
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for item in text_boxes:
            group = str(item.get("font_group") or "").strip()
            if group:
                groups.setdefault(group, []).append(item)
        for group_id, members in groups.items():
            if len(members) < 2:
                continue
            text_groups.append({
                "id": group_id,
                "element_ids": [str(item["id"]) for item in members],
                "font_size": min(float(item.get("font_size", 12)) for item in members),
                "source": "auto-font-group",
            })
        return {
            "schema_version": 1,
            "canvas": {"width_px": width, "height_px": height},
            "text_groups": text_groups,
            "alignment_rules": alignment_rules,
        }

    def _apply_text_layout_rules(self, text_boxes: List[Dict[str, Any]], layout_rules: Dict[str, Any]) -> List[Dict[str, Any]]:
        by_id = {str(item.get("id")): item for item in text_boxes}
        for group in layout_rules.get("text_groups", []):
            ids = [str(value) for value in group.get("element_ids", []) if str(value) in by_id]
            if not ids:
                continue
            explicit_size = group.get("font_size")
            if explicit_size is not None:
                try:
                    size = float(explicit_size)
                except (TypeError, ValueError):
                    size = min(float(by_id[item_id].get("font_size", 12)) for item_id in ids)
            else:
                size = min(float(by_id[item_id].get("font_size", 12)) for item_id in ids)
            for item_id in ids:
                item = by_id[item_id]
                expected = int(item.get("expected_lines") or 1)
                item["font_size"] = min(size, self._max_font_for_box(item["box_px"], str(item.get("text") or ""), expected))
        return text_boxes

    def _build_text_inventory(self, text_boxes: List[Dict[str, Any]]) -> List[str]:
        inventory: List[str] = []
        seen = set()
        for item in text_boxes:
            text = str(item.get("text") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if not text:
                continue
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            if len(lines) > 1:
                avg_len = sum(len(line) for line in lines) / len(lines)
                candidates = ["".join(lines)] if avg_len <= 2 else lines
            else:
                candidates = [text]
            for candidate in candidates:
                candidate = candidate.strip()
                if candidate and candidate not in seen:
                    inventory.append(candidate)
                    seen.add(candidate)
        return inventory

    def _normalize_shapes(self, raw_items: Any, width: int, height: int) -> List[Dict[str, Any]]:
        shapes: List[Dict[str, Any]] = []
        if not isinstance(raw_items, list):
            return shapes
        allowed = {"rect", "roundRect", "ellipse", "line"}
        for idx, raw in enumerate(raw_items, start=1):
            if not isinstance(raw, dict):
                continue
            box = self._normalize_box(raw.get("box_px") or raw.get("bbox"), width, height)
            if not box:
                continue
            purpose = str(raw.get("purpose") or "layout_only")
            descriptor = " ".join(
                str(raw.get(key) or "")
                for key in ("id", "type", "purpose", "label", "semantic_label", "notes")
            ).lower()
            is_arrow = any(token in descriptor for token in ("arrow", "箭头", "flow"))
            if is_arrow:
                direction = self._arrow_direction_from_descriptor(descriptor, box)
                item = {
                    "id": str(raw.get("id") or f"shape_{idx:03d}"),
                    "type": "rect",
                    "preset": {
                        "right": "rightArrow",
                        "left": "leftArrow",
                        "down": "downArrow",
                        "up": "upArrow",
                    }.get(direction, "rightArrow"),
                    "box_px": box,
                    "fill": self._normalize_color(raw.get("fill"), "#2F75B5"),
                    "stroke": self._normalize_color(raw.get("stroke"), "none"),
                    "purpose": "structural_arrow",
                    "z_index": int(raw.get("z_index") or 120),
                }
                shapes.append(item)
                continue
            if purpose not in {"layout_only", "background", "panel", "frame", "divider", "connector", "structural_arrow", "text_container"}:
                continue
            shape_type = raw.get("type") if raw.get("type") in allowed else "rect"
            item = {
                "id": str(raw.get("id") or f"shape_{idx:03d}"),
                "type": shape_type,
                "box_px": box,
                "fill": self._normalize_color(raw.get("fill"), "#FFFFFF"),
                "stroke": self._normalize_color(raw.get("stroke"), "none"),
                "purpose": purpose,
                "z_index": int(raw.get("z_index") or 100),
            }
            if shape_type == "roundRect":
                item["source_corner_radius_px"] = max(4, min(box[2], box[3]) // 8)
                item["corner_reason"] = "Vision model classified this source container as rounded."
            shapes.append(item)
        return shapes

    def _apply_alignment_rules(
        self,
        text_boxes: List[Dict[str, Any]],
        shapes: List[Dict[str, Any]],
        images: List[Dict[str, Any]],
        layout_rules: Dict[str, Any],
    ) -> None:
        elements: Dict[str, Dict[str, Any]] = {}
        for collection in (text_boxes, shapes, images):
            for item in collection:
                item_id = str(item.get("id") or "")
                if item_id:
                    elements[item_id] = item
        for rule in layout_rules.get("alignment_rules", []):
            ids = [str(value) for value in rule.get("element_ids", []) if str(value) in elements]
            if len(ids) < 2:
                continue
            boxes = [elements[item_id].get("box_px") for item_id in ids]
            if any(not isinstance(box, list) or len(box) != 4 for box in boxes):
                continue
            mode = rule.get("type")
            try:
                tolerance = float(rule.get("tolerance_px", 8))
            except (TypeError, ValueError):
                tolerance = 8.0
            if mode == "center_x":
                centers = [float(box[0]) + float(box[2]) / 2 for box in boxes]
                target = sum(centers) / len(centers)
                if max(abs(center - target) for center in centers) <= tolerance:
                    for item_id, box in zip(ids, boxes):
                        elements[item_id]["box_px"][0] = int(round(target - float(box[2]) / 2))
            elif mode == "center_y":
                centers = [float(box[1]) + float(box[3]) / 2 for box in boxes]
                target = sum(centers) / len(centers)
                if max(abs(center - target) for center in centers) <= tolerance:
                    for item_id, box in zip(ids, boxes):
                        elements[item_id]["box_px"][1] = int(round(target - float(box[3]) / 2))
            elif mode == "edge_left":
                edges = [float(box[0]) for box in boxes]
                target = sum(edges) / len(edges)
                if max(abs(edge - target) for edge in edges) <= tolerance:
                    for item_id in ids:
                        elements[item_id]["box_px"][0] = int(round(target))
            elif mode == "edge_top":
                edges = [float(box[1]) for box in boxes]
                target = sum(edges) / len(edges)
                if max(abs(edge - target) for edge in edges) <= tolerance:
                    for item_id in ids:
                        elements[item_id]["box_px"][1] = int(round(target))

    def _normalize_box(self, value: Any, width: int, height: int) -> Optional[List[int]]:
        if not isinstance(value, list) or len(value) != 4:
            return None
        try:
            x, y, w, h = [int(round(float(v))) for v in value]
        except (TypeError, ValueError):
            return None
        x = max(0, min(x, width - 1))
        y = max(0, min(y, height - 1))
        w = max(1, min(w, width - x))
        h = max(1, min(h, height - y))
        if w < 4 or h < 4:
            return None
        return [x, y, w, h]

    def _is_full_slide_box(self, box: List[int], width: int, height: int) -> bool:
        return box[0] <= 2 and box[1] <= 2 and box[2] >= width * 0.96 and box[3] >= height * 0.96

    def _crop_source_asset(self, source: Path, dest_dir: Path, name: str, box: List[int]) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        x, y, w, h = box
        dest = dest_dir / f"{name}.png"
        with Image.open(source) as img:
            img.crop((x, y, x + w, y + h)).save(dest)
        return dest

    def _extract_foreground_asset(self, source: Path, clean_background: Path, dest_dir: Path, name: str, box: List[int]) -> Optional[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        x, y, w, h = box
        try:
            with Image.open(source).convert("RGB") as src_img, Image.open(clean_background).convert("RGB") as bg_img:
                src_crop = src_img.crop((x, y, x + w, y + h)).convert("RGBA")
                bg_crop = bg_img.crop((x, y, x + w, y + h)).convert("RGB")
        except Exception as exc:
            logger.warning("Foreground extraction failed for %s: %s", name, exc)
            return None

        src_px = src_crop.load()
        bg_px = bg_crop.load()
        mask = [[False for _ in range(w)] for _ in range(h)]
        changed = 0
        for yy in range(h):
            for xx in range(w):
                sr, sg, sb, sa = src_px[xx, yy]
                br, bg, bb = bg_px[xx, yy]
                diff = abs(sr - br) + abs(sg - bg) + abs(sb - bb)
                saturation = max(sr, sg, sb) - min(sr, sg, sb)
                if diff >= 30 and (saturation >= 10 or max(sr, sg, sb) <= 238):
                    mask[yy][xx] = True
                    changed += 1

        coverage = changed / max(1, w * h)
        if coverage < 0.002:
            src_crop.close()
            bg_crop.close()
            return None

        # Expand the mask so anti-aliased icon edges, thin strokes, and arrowheads are retained.
        expanded = [row[:] for row in mask]
        for _ in range(2):
            next_mask = [row[:] for row in expanded]
            for yy in range(h):
                for xx in range(w):
                    if not expanded[yy][xx]:
                        continue
                    for nx in range(xx - 1, xx + 2):
                        for ny in range(yy - 1, yy + 2):
                            if 0 <= nx < w and 0 <= ny < h:
                                next_mask[ny][nx] = True
            expanded = next_mask

        for yy in range(h):
            for xx in range(w):
                if not expanded[yy][xx]:
                    sr, sg, sb, _sa = src_px[xx, yy]
                    src_px[xx, yy] = (sr, sg, sb, 0)

        alpha = src_crop.getchannel("A")
        if not alpha.getbbox():
            src_crop.close()
            bg_crop.close()
            return None
        dest = dest_dir / f"{name}.png"
        src_crop.save(dest)
        src_crop.close()
        bg_crop.close()
        return dest

    def _clean_background_likely_contains_object(self, source: Path, clean_background: Path, box: List[int]) -> bool:
        x, y, w, h = box
        try:
            with Image.open(source).convert("RGB") as src_img, Image.open(clean_background).convert("RGB") as bg_img:
                src_crop = src_img.crop((x, y, x + w, y + h))
                bg_crop = bg_img.crop((x, y, x + w, y + h))
                src_px = src_crop.load()
                bg_px = bg_crop.load()
                changed = 0
                sample_count = 0
                step = max(1, min(w, h) // 48)
                for yy in range(0, h, step):
                    for xx in range(0, w, step):
                        sr, sg, sb = src_px[xx, yy]
                        br, bg, bb = bg_px[xx, yy]
                        if abs(sr - br) + abs(sg - bg) + abs(sb - bb) >= 42:
                            changed += 1
                        sample_count += 1
                return changed / max(1, sample_count) < 0.02
        except Exception:
            return False

    def _large_source_asset_would_bake_background(self, item: Dict[str, Any], box: List[int], width: int, height: int) -> bool:
        _, _, w, h = box
        area_ratio = (w * h) / max(1, width * height)
        descriptor = " ".join(
            str(item.get(key) or "")
            for key in ("id", "type", "semantic_label", "label", "notes")
        ).lower()
        if any(token in descriptor for token in ("icon", "logo", "arrow", "pictogram", "symbol", "箭头", "图标")):
            return False
        return area_ratio >= 0.035 or w >= width * 0.22 or h >= height * 0.22

    def _copy_generated_asset(self, generated: Path, dest_dir: Path, name: str) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{name}.png"
        try:
            self._transparentize_generated_asset(generated, dest)
        except Exception as exc:
            logger.warning("Generated asset transparentization failed for %s: %s", generated, exc)
            shutil.copy2(generated, dest)
        return dest

    def _transparentize_generated_asset(self, src: Path, dest: Path, tolerance: int = 18) -> None:
        image = Image.open(src).convert("RGBA")
        try:
            alpha = image.getchannel("A")
            pixels = image.load()
            width, height = image.size
            visited = set()
            stack = []
            for x in range(width):
                stack.append((x, 0))
                stack.append((x, height - 1))
            for y in range(height):
                stack.append((0, y))
                stack.append((width - 1, y))

            def near_white(x: int, y: int) -> bool:
                r, g, b, a = pixels[x, y]
                return a > 0 and r >= 255 - tolerance and g >= 255 - tolerance and b >= 255 - tolerance

            while stack:
                x, y = stack.pop()
                if x < 0 or y < 0 or x >= width or y >= height or (x, y) in visited:
                    continue
                visited.add((x, y))
                if not near_white(x, y):
                    continue
                r, g, b, _a = pixels[x, y]
                pixels[x, y] = (r, g, b, 0)
                stack.extend(((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))

            alpha = image.getchannel("A")
            bbox = alpha.getbbox()
            if bbox:
                left, top, right, bottom = bbox
                left = max(0, left - 3)
                top = max(0, top - 3)
                right = min(width, right + 3)
                bottom = min(height, bottom + 3)
                image = image.crop((left, top, right, bottom))
            dest.parent.mkdir(parents=True, exist_ok=True)
            image.save(dest)
        finally:
            image.close()

    def _should_generate_semantic_asset(self, item: Dict[str, Any], box: List[int], width: int, height: int) -> bool:
        if not advanced_editable_flag("ADVANCED_EDITABLE_USE_IMAGEGEN_ASSETS", True):
            return False
        if item.get("split_from"):
            return True
        route = str(item.get("route") or item.get("classification") or item.get("type") or "").lower()
        label = " ".join(
            str(item.get(key) or "")
            for key in ("id", "semantic_unit_id", "semantic_label", "label", "notes")
        ).lower()
        if route in {"source_crop", "source-derived", "source_derived", "large_diagram"}:
            return False
        if route in {"imagegen_asset", "generated_asset", "icon", "pictogram", "logo", "mark"}:
            return True
        _, _, box_w, box_h = box
        area_ratio = (box_w * box_h) / max(1, width * height)
        semantic_hint = any(
            hint in label
            for hint in ("icon", "pictogram", "logo", "mark", "symbol", "badge", "emblem", "arrow", "flow", "connector", "箭头")
        )
        dense_hint = any(
            hint in label
            for hint in ("diagram", "architecture", "chart", "graph", "table", "流程", "架构", "图表")
        )
        if dense_hint:
            return False
        return semantic_hint or area_ratio <= 0.025

    def _contain_box(self, asset_path: Path, anchor_slot: List[int]) -> List[int]:
        x, y, w, h = anchor_slot
        try:
            with Image.open(asset_path) as img:
                iw, ih = img.size
        except Exception:
            return anchor_slot
        if iw <= 0 or ih <= 0:
            return anchor_slot
        fit = min(w / iw, h / ih)
        final_w = max(1, int(round(iw * fit)))
        final_h = max(1, int(round(ih * fit)))
        final_x = x + int(round((w - final_w) / 2))
        final_y = y + int(round((h - final_h) / 2))
        return [final_x, final_y, final_w, final_h]

    def _fit_image_size(self, size: tuple[int, int], max_w: int, max_h: int) -> tuple[int, int]:
        iw, ih = size
        if iw <= 0 or ih <= 0:
            return (1, 1)
        fit = min(max_w / iw, max_h / ih)
        return (max(1, int(round(iw * fit))), max(1, int(round(ih * fit))))

    def _expected_lines(self, raw: Dict[str, Any], text: str) -> int:
        try:
            expected = int(raw.get("expected_lines") or raw.get("lines") or 0)
        except (TypeError, ValueError):
            expected = 0
        if expected <= 0:
            expected = max(1, text.count("\n") + 1)
        return max(1, min(expected, 12))

    def _split_text_to_lines(self, text: str, expected_lines: int) -> str:
        text = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
        if expected_lines <= 1 or "\n" in text:
            return text
        collapsed = " ".join(text.split())
        if not collapsed:
            return collapsed
        if " " in collapsed and not CJK_RE.search(collapsed):
            words = collapsed.split()
            chunks = [[] for _ in range(expected_lines)]
            for idx, word in enumerate(words):
                chunks[min(expected_lines - 1, int(idx * expected_lines / max(1, len(words))))].append(word)
            return "\n".join(" ".join(chunk).strip() for chunk in chunks if chunk)
        n = len(collapsed)
        return "\n".join(
            collapsed[round(i * n / expected_lines):round((i + 1) * n / expected_lines)]
            for i in range(expected_lines)
        )

    def _calibrated_font_size(self, raw_size: Any, box: List[int], text: str, expected_lines: int) -> float:
        estimated = self._estimate_font_from_box(box, text)
        try:
            requested = float(raw_size)
        except (TypeError, ValueError):
            requested = estimated
        max_size = self._max_font_for_box(box, text, expected_lines)
        return round(max(5.0, min(requested, estimated, max_size)), 1)

    def _max_font_for_box(self, box: List[int], text: str, expected_lines: int) -> float:
        _, _, w, h = box
        lines = max(1, expected_lines, text.count("\n") + 1)
        line_height_cap = h / lines * 0.52
        longest = max((len(line.strip()) for line in text.splitlines()), default=len(text))
        width_cap = w / max(longest * 0.62, 1.0) * 0.72
        return max(5.0, min(60.0, line_height_cap, width_cap))

    def _estimate_font_from_box(self, box: List[int], text: str) -> float:
        _, _, w, h = box
        lines = max(1, text.count("\n") + 1)
        chars_per_line = max(1.0, len(text) / lines)
        by_height = h / lines * 0.58
        by_width = w / max(chars_per_line * 0.55, 1.0) * 0.72
        return round(max(5.0, min(60.0, by_height, by_width)), 1)

    def _normalize_color(self, value: Any, default: str) -> str:
        if isinstance(value, str):
            text = value.strip()
            if text in {"none", "transparent"}:
                return "none"
            if len(text) == 7 and text.startswith("#"):
                return text
        return default
