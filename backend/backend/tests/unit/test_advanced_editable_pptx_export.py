import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from services.advanced_editable_pptx_service import AdvancedEditablePPTXRunner, _slide_size_inches
from services.advanced_editable_workers import AdvancedEditableImageGenWorker, AdvancedEditablePageWorker


def test_advanced_editable_runner_builds_deck_with_mocked_pages(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    project_id = "project-1"
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    image_path = source_dir / "slide.png"
    Image.new("RGB", (640, 360), "white").save(image_path)

    def fake_process_page(self, run_dir, page, agent_id, flask_app, *_args):
        page_dir = run_dir / page["page_dir"]
        manifest = {
            "schema_version": 1,
            "slide": {"width": 13.333, "height": 7.4998},
            "source": {"path": "source.png", "width_px": 640, "height_px": 360},
            "text_inventory": [],
            "visual_inventory": [],
            "background_strategy": {"mode": "native-or-script"},
            "quality_checks": {
                "font_size_calibrated": True,
                "visual_inventory_matched": True,
                "background_strategy_checked": True,
                "shape_corner_geometry_checked": True,
            },
            "text_boxes": [],
            "shapes": [],
            "images": [
                {"id": "background", "path": "source.png", "box_px": [0, 0, 640, 360], "z_index": 0}
            ],
            "asset_provenance": [
                {
                    "path": "source.png",
                    "source": "source.png",
                    "source_type": "user-provided",
                    "provenance_note": "Mock source background for runner test.",
                }
            ],
        }
        manifest_path = page_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        builder = self._get_builder()
        page_pptx = page_dir / "page.pptx"
        preview = page_dir / "preview.png"
        builder.write_pptx(manifest, page_pptx, manifest_path)
        builder.render_preview(manifest, manifest_path, preview)
        (page_dir / "split_assets_contact.png").write_bytes(preview.read_bytes())
        validation = page_dir / "validation.json"
        validation.write_text(json.dumps({"passed": True}), encoding="utf-8")
        result = {
            "page_manifest": "manifest.json",
            "imagegen_jobs": "imagegen-jobs.json",
            "page_pptx": "page.pptx",
            "preview": "preview.png",
            "contact_sheet": "split_assets_contact.png",
            "validation": "validation.json",
            "page_result": "page_result.json",
            "qa_note": "mock",
            "known_limits": [],
            "validation_passed": True,
        }
        (page_dir / "page_result.json").write_text(json.dumps(result), encoding="utf-8")
        return {"page_id": page["page_id"], "agent_id": agent_id, "result": result}

    monkeypatch.setattr(AdvancedEditablePPTXRunner, "_process_page", fake_process_page)

    output = upload_dir / project_id / "exports" / "advanced.pptx"
    runner = AdvancedEditablePPTXRunner(upload_folder=str(upload_dir))
    summary = runner.run(
        project_id=project_id,
        image_paths=[str(image_path)],
        output_file=str(output),
        task_id="task-1",
        max_workers=1,
    )

    run_dir = Path(summary["run_dir"])
    page_request = json.loads((run_dir / "pages" / "page_001" / "page_request.json").read_text(encoding="utf-8"))
    assert output.exists()
    assert (run_dir / "deck_manifest.json").exists()
    assert (run_dir / "page_jobs.json").exists()
    assert (run_dir / "pages" / "page_001" / "manifest.json").exists()
    assert (run_dir / "final" / "validation.json").exists()
    assert page_request["slide"] == {"width": 13.333, "height": 7.4998}
    assert summary["status"] == "complete"


def test_advanced_editable_export_api_creates_task(client, app):
    from models import db, Page, Project
    from services.task_manager import task_manager

    with app.app_context():
        project = Project(
            id="project-advanced-missing-image",
            creation_type="idea",
            idea_prompt="test",
            status="COMPLETED",
        )
        ok_page = Page(
            id="page-advanced-has-image",
            project_id=project.id,
            order_index=0,
            status="COMPLETED",
            generated_image_path=f"{project.id}/pages/page1.png",
        )
        missing_page = Page(
            id="page-advanced-no-image",
            project_id=project.id,
            order_index=1,
            status="COMPLETED",
            generated_image_path=None,
        )
        db.session.add_all([project, ok_page, missing_page])
        db.session.commit()

    with patch.object(task_manager, "submit_task") as submit_task:
        response = client.post(
            "/api/projects/project-advanced-missing-image/export/advanced-editable-pptx",
            json={"page_ids": ["page-advanced-has-image", "page-advanced-no-image"]},
        )

    assert response.status_code == 400
    data = response.get_json()
    assert data["success"] is False
    assert data["error"]["message"] == "Selected pages are missing generated images"
    submit_task.assert_not_called()


    from models import db, Page, Project, Task
    from services.task_manager import task_manager

    with app.app_context():
        project = Project(
            id="project-advanced-api",
            creation_type="idea",
            idea_prompt="test",
            status="COMPLETED",
        )
        page = Page(
            id="page-advanced-api-1",
            project_id=project.id,
            order_index=0,
            status="COMPLETED",
            generated_image_path=f"{project.id}/pages/page.png",
        )
        db.session.add(project)
        db.session.add(page)
        db.session.commit()

    with patch.object(task_manager, "submit_task") as submit_task:
        response = client.post(
            "/api/projects/project-advanced-api/export/advanced-editable-pptx",
            json={"page_ids": ["page-advanced-api-1"]},
        )

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["data"]["method"] == "advanced_builtin_runner"
    assert data["data"]["task_id"]
    submit_task.assert_called_once()

    with app.app_context():
        task = Task.query.get(data["data"]["task_id"])
        assert task is not None
        assert task.task_type == "EXPORT_ADVANCED_EDITABLE_PPTX"
        assert task.status == "PENDING"


def test_advanced_editable_task_passes_partial_setting(app):
    from models import db, Page, Project, Task
    from services.task_manager import export_advanced_editable_pptx_task

    with app.app_context():
        project = Project(
            id="project-advanced-partial",
            creation_type="idea",
            idea_prompt="test",
            status="COMPLETED",
            export_allow_partial=True,
        )
        page = Page(
            id="page-advanced-partial-1",
            project_id=project.id,
            order_index=0,
            status="COMPLETED",
            generated_image_path=f"{project.id}/pages/page.png",
        )
        task = Task(project_id=project.id, task_type='EXPORT_ADVANCED_EDITABLE_PPTX', status='PENDING')
        db.session.add_all([project, page, task])
        db.session.commit()
        task_id = task.id

    class DummyFileService:
        def __init__(self, upload_folder):
            self.upload_folder = upload_folder

        def get_absolute_path(self, relative_path):
            import os
            return os.path.join(self.upload_folder, relative_path)

    import os
    page_dir = os.path.join(app.config['UPLOAD_FOLDER'], 'project-advanced-partial', 'pages')
    os.makedirs(page_dir, exist_ok=True)
    image_path = os.path.join(page_dir, 'page.png')
    Image.new('RGB', (64, 64), 'white').save(image_path)

    with patch('services.advanced_editable_pptx_service.AdvancedEditablePPTXRunner.run', return_value={'run_dir': 'x', 'validation': 'y'}) as run_mock:
        export_advanced_editable_pptx_task(
            task_id=task_id,
            project_id='project-advanced-partial',
            filename='out.pptx',
            file_service=DummyFileService(app.config['UPLOAD_FOLDER']),
            app=app,
        )

    assert run_mock.call_args.kwargs['fail_fast'] is False


# ── slide size helpers ────────────────────────────────────────────────────────

def test_slide_size_inches_16x9():
    size = _slide_size_inches(1920, 1080)
    assert size["width"] == 13.333
    assert abs(size["height"] - 7.5) < 0.001


def test_slide_size_inches_4x3():
    size = _slide_size_inches(1024, 768)
    expected_height = round(13.333 * 768 / 1024, 4)
    assert size["width"] == 13.333
    assert size["height"] == expected_height


def test_slide_size_inches_zero_guard():
    size = _slide_size_inches(0, 0)
    assert size["width"] == 13.333
    assert size["height"] == 7.5


def test_prepare_run_manifest_uses_real_aspect_ratio(tmp_path, monkeypatch):
    """page_request.json must store the real slide dimensions, not hardcoded 16:9."""
    upload_dir = tmp_path / "uploads"
    project_id = "proj-ratio"
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    # 4:3 image
    image_path = source_dir / "slide.png"
    Image.new("RGB", (1024, 768), "white").save(image_path)

    def fake_process_page(self, run_dir, page, agent_id, flask_app, *_args):
        page_dir = run_dir / page["page_dir"]
        page_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": 1,
            "slide": {"width": 13.333, "height": 10.0},
            "source": {"path": "source.png", "width_px": 1024, "height_px": 768},
            "text_inventory": [], "visual_inventory": [],
            "background_strategy": {"mode": "test"},
            "quality_checks": {
                "font_size_calibrated": True, "visual_inventory_matched": True,
                "background_strategy_checked": True, "shape_corner_geometry_checked": True,
            },
            "text_boxes": [], "shapes": [],
            "images": [{"id": "bg", "path": "source.png", "box_px": [0, 0, 1024, 768], "z_index": 0}],
            "asset_provenance": [{"path": "source.png", "source": "source.png", "source_type": "user-provided", "provenance_note": "test"}],
        }
        manifest_path = page_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        builder = self._get_builder()
        page_pptx = page_dir / "page.pptx"
        preview = page_dir / "preview.png"
        builder.write_pptx(manifest, page_pptx, manifest_path)
        builder.render_preview(manifest, manifest_path, preview)
        (page_dir / "split_assets_contact.png").write_bytes(preview.read_bytes())
        (page_dir / "validation.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
        result = {
            "page_manifest": "manifest.json", "imagegen_jobs": "imagegen-jobs.json",
            "page_pptx": "page.pptx", "preview": "preview.png",
            "contact_sheet": "split_assets_contact.png", "validation": "validation.json",
            "page_result": "page_result.json", "qa_note": "mock",
            "known_limits": [], "validation_passed": True,
        }
        (page_dir / "page_result.json").write_text(json.dumps(result), encoding="utf-8")
        return {"page_id": page["page_id"], "agent_id": agent_id, "result": result}

    monkeypatch.setattr(AdvancedEditablePPTXRunner, "_process_page", fake_process_page)

    output = upload_dir / project_id / "exports" / "out.pptx"
    runner = AdvancedEditablePPTXRunner(upload_folder=str(upload_dir))
    summary = runner.run(
        project_id=project_id,
        image_paths=[str(image_path)],
        output_file=str(output),
        task_id="task-ratio",
        max_workers=1,
    )
    run_dir = Path(summary["run_dir"])
    page_request = json.loads((run_dir / "pages" / "page_001" / "page_request.json").read_text(encoding="utf-8"))
    slide = page_request["slide"]
    assert slide["width"] == 13.333
    # 4:3 → height should be ~10.0, not 7.5
    assert abs(slide["height"] - round(13.333 * 768 / 1024, 4)) < 0.001
    assert abs(slide["height"] - 7.5) > 0.5  # must NOT be the old hardcoded value


# ── MinerU text priority ──────────────────────────────────────────────────────

def test_analyze_page_manifest_skips_text_when_mineru_active(tmp_path):
    """When skip_text_boxes=True the Vision prompt must not request text_boxes
    and the call result must return empty text_boxes."""
    worker = AdvancedEditablePageWorker.__new__(AdvancedEditablePageWorker)

    ai_service = MagicMock()
    ai_service.generate_json_with_image.return_value = {
        "text_boxes": [],
        "shapes": [],
        "images": [{"id": "visual_001", "type": "semantic_visual",
                    "box_px": [10, 10, 200, 150], "anchor_slot": [10, 10, 200, 150],
                    "semantic_unit_id": "visual_001", "semantic_label": "logo", "z_index": 200}],
        "layout_rules": {"text_groups": [], "alignment_rules": []},
        "known_limits": [],
    }

    source = tmp_path / "slide.png"
    Image.new("RGB", (640, 360), "white").save(source)

    plan = worker._analyze_page_manifest(ai_service, source, 640, 360, skip_text_boxes=True)

    # Vision was called
    ai_service.generate_json_with_image.assert_called_once()
    call_prompt = ai_service.generate_json_with_image.call_args[0][0]
    # The prompt must NOT ask for text transcription
    assert "do not return text_boxes" in call_prompt.lower() or "MinerU has already provided" in call_prompt
    # text_boxes in the returned plan must be empty
    assert plan.get("text_boxes", []) == []


def test_run_page_uses_mineru_text_before_vision(tmp_path, monkeypatch):
    """When MinerU returns text boxes they must become the page_plan text_boxes
    and Vision must be called with skip_text_boxes=True."""
    mineru_boxes = [
        {"id": "m_text_001", "text": "MinerU Title", "box_px": [50, 50, 400, 60],
         "font_size": 32, "expected_lines": 1, "font_group": "title",
         "font": "Noto Sans CJK SC", "color": "#111111", "align": "center",
         "valign": "middle", "bold": True, "z_index": 320, "source": "mineru"},
    ]

    captured = {}

    def fake_extract_mineru(self, source, w, h):
        return {"text_boxes": mineru_boxes, "images": []}

    def fake_analyze(self, ai_service, source, w, h, *, skip_text_boxes=False):
        captured["skip_text_boxes"] = skip_text_boxes
        return {"text_boxes": [], "shapes": [], "images": [], "layout_rules": {}, "known_limits": []}

    def fake_generate_clean_bg(self, page_dir, source):
        output = page_dir / "generated" / "clean_background.png"
        output.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (640, 360), "white").save(output)
        return output

    def fake_build_manifest(self, page_dir, source, w, h, page_plan, clean_bg,
                             imagegen_worker, ai_service, ocr_enabled, mineru_enabled,
                             *, slide_width_in, slide_height_in):
        return {
            "schema_version": 1,
            "slide": {"width": slide_width_in, "height": slide_height_in},
            "source": {"path": "source.png", "width_px": w, "height_px": h},
            "text_inventory": [], "visual_inventory": [], "layout_rules": {},
            "background_strategy": {"mode": "test"},
            "quality_checks": {
                "font_size_calibrated": True, "visual_inventory_matched": True,
                "background_strategy_checked": True, "shape_corner_geometry_checked": True,
                "layout_rules_applied": True, "semantic_assets_are_independent": True,
            },
            "text_boxes": page_plan.get("text_boxes", []),
            "shapes": [], "images": [],
            "asset_provenance": [],
            "known_limits": page_plan.get("known_limits", []),
            "ocr_strategy": {"enabled": ocr_enabled, "provider": None, "text_boxes": 0},
            "mineru_strategy": {"enabled": mineru_enabled, "provider": "mineru" if mineru_enabled else None},
        }

    def fake_validate(self, pptx, manifest, report):
        report.write_text(json.dumps({"passed": True}), encoding="utf-8")

    monkeypatch.setattr(AdvancedEditablePageWorker, "_extract_mineru_page_plan", fake_extract_mineru)
    monkeypatch.setattr(AdvancedEditablePageWorker, "_analyze_page_manifest", fake_analyze)
    monkeypatch.setattr(AdvancedEditableImageGenWorker, "generate_clean_background", fake_generate_clean_bg)
    monkeypatch.setattr(AdvancedEditablePageWorker, "_build_manifest", fake_build_manifest)
    monkeypatch.setattr(AdvancedEditablePageWorker, "_validate_page", fake_validate)

    # Patch builder
    fake_builder = MagicMock()
    fake_builder.write_pptx.return_value = None
    fake_builder.render_preview.side_effect = lambda m, mp, p: Image.new("RGB", (320, 180)).save(p)
    worker = AdvancedEditablePageWorker(builder=fake_builder)

    run_dir = tmp_path / "run"
    page_dir = run_dir / "pages" / "page_001"
    page_dir.mkdir(parents=True, exist_ok=True)
    source = page_dir / "source.png"
    Image.new("RGB", (640, 360), "white").save(source)
    page_request = {
        "schema_version": 1, "run_id": "r1", "page_id": "page_001", "page_index": 1,
        "page_dir": str(page_dir), "source_image": str(source),
        "source_size_px": {"width": 640, "height": 360},
        "slide": {"width": 13.333, "height": 7.5},
        "max_concurrent_pages": 1, "allowed_write_scope": str(page_dir), "required_outputs": {},
    }
    (page_dir / "page_request.json").write_text(json.dumps(page_request), encoding="utf-8")
    (page_dir / "imagegen-jobs.json").write_text(json.dumps({"schema_version": 1, "jobs": []}), encoding="utf-8")

    page = {
        "page_id": "page_001", "page_index": 1, "status": "pending",
        "page_dir": "pages/page_001", "source": "pages/page_001/source.png",
        "source_image": "pages/page_001/source.png",
    }
    result = worker.run_page(run_dir, page, MagicMock())

    # Vision must have been called with skip_text_boxes=True (MinerU had text)
    assert captured.get("skip_text_boxes") is True
    # The manifest written must contain the MinerU text box and preserve slide size from page_request
    manifest_data = json.loads((page_dir / "manifest.json").read_text(encoding="utf-8"))
    texts = [t["text"] for t in manifest_data.get("text_boxes", [])]
    assert "MinerU Title" in texts
    assert manifest_data["slide"] == {"width": 13.333, "height": 7.5}
