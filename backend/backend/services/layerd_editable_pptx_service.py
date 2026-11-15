"""Runner for the MinerU + direct LayerD editable PPTX export."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

import requests


class LayerDEditablePPTXRunner:
    def __init__(
        self,
        *,
        paper2any_root: str,
        api_base: str,
        api_key: str,
        image_model: str = "gemini-3.1-flash-image-preview",
        python_executable: Optional[str] = None,
        timeout: int = 7200,
        progress_callback: Optional[Callable[[str, str, int], None]] = None,
    ):
        self.paper2any_root = Path(paper2any_root)
        self.api_base = api_base
        self.api_key = api_key
        self.image_model = image_model
        runtime_root = self.paper2any_root.parents[1]
        bundled_python_candidates = (
            runtime_root / "paper2any-venv" / "bin" / "python",
            runtime_root / "paper2any-venv" / "Scripts" / "python.exe",
        )
        self.python_executable = (
            python_executable
            or next(
                (
                    str(candidate)
                    for candidate in bundled_python_candidates
                    if candidate.exists()
                ),
                None,
            )
            or sys.executable
        )
        self.timeout = timeout
        self.progress_callback = progress_callback

    def _progress(self, step: str, message: str, percent: int) -> None:
        if self.progress_callback:
            self.progress_callback(step, message, percent)

    def run(
        self,
        *,
        image_paths: Iterable[str],
        output_file: str,
        work_dir: str,
    ) -> dict:
        script = self.paper2any_root / "script" / "run_layerd_direct_export.py"
        if not script.exists():
            raise RuntimeError(f"LayerD direct runner not found: {script}")
        if not self.api_key:
            raise RuntimeError("LayerD editable export requires an API key")

        run_dir = Path(work_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        resolved_images = [str(Path(path).resolve()) for path in image_paths]
        manifest_path = run_dir / "input_images.json"
        manifest_path.write_text(json.dumps(resolved_images), encoding="utf-8")

        pipeline_log = run_dir / "pipeline.log"
        events_path = run_dir / "events.jsonl"
        summary_path = run_dir / "run_summary.json"
        started_at = datetime.now(timezone.utc)
        started_clock = time.monotonic()

        def write_event(event: str, **data) -> None:
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **data,
            }
            with events_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

        def write_summary(status: str, **data) -> None:
            summary_path.write_text(
                json.dumps(
                    {
                        "status": status,
                        "started_at": started_at.isoformat(),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "duration_seconds": round(time.monotonic() - started_clock, 3),
                        "page_count": len(resolved_images),
                        "pipeline_log": str(pipeline_log),
                        "events": str(events_path),
                        **data,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        env = os.environ.copy()
        env.update(
            {
                "DF_API_URL": self.api_base,
                "DF_API_KEY": self.api_key,
                "DF_IMAGE_API_URL": self.api_base,
                "DF_IMAGE_API_KEY": self.api_key,
                "PAPER2PPT_ENABLE_STRUCTURE_ANALYSIS": "0",
                "PAPER2PPT_EDITABLE_IR_STRICT": "1",
                "PAPER2PPT_LAYERD_DIRECT": "1",
                "PAPER2PPT_LOCAL_VISUAL_FALLBACK": "0",
                "PAPER2PPT_MINERU_ENABLED": os.environ.get(
                    "PAPER2PPT_MINERU_ENABLED", "1"
                ),
                "PAPER2PPT_MINERU_REQUIRED": "1",
                "PAPER2PPT_MINERU_API_URL": os.environ.get(
                    "PAPER2PPT_MINERU_API_URL", ""
                ),
                "PAPER2PPT_MINERU_CMD": os.environ.get(
                    "PAPER2PPT_MINERU_CMD", ""
                ),
                "PAPER2PPT_MINERU_DEVICE": os.environ.get(
                    "PAPER2PPT_MINERU_DEVICE", "cuda"
                ),
                "PAPER2PPT_ICON_BG_REMOVE_MODE": "off",
                "LAYERD_ROOT": os.environ.get(
                    "LAYERD_ROOT", "/app/experiments/LayerD"
                ),
                "HF_HOME": os.environ.get(
                    "HF_HOME", "/app/experiments/LayerD/models/hf"
                ),
                "TORCH_HOME": os.environ.get(
                    "TORCH_HOME", "/app/experiments/LayerD/models/torch"
                ),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
            }
        )
        command = [
            self.python_executable,
            str(script),
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(run_dir),
            "--output",
            str(output_file),
            "--api-url",
            self.api_base,
            "--image-model",
            self.image_model,
        ]

        self._progress(
            "layerd_direct",
            "Running MinerU OCR before direct LayerD GPU decomposition",
            12,
        )
        write_event("pipeline_started", page_count=len(resolved_images))
        process = subprocess.Popen(
            command,
            cwd=str(self.paper2any_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output_lines = []
        pipeline_stream = pipeline_log.open("w", encoding="utf-8")
        try:
            assert process.stdout is not None
            for line in process.stdout:
                clean_line = line.rstrip()
                pipeline_stream.write(clean_line + "\n")
                pipeline_stream.flush()
                output_lines.append(clean_line)
                if len(output_lines) > 200:
                    output_lines = output_lines[-200:]
                if "[LayerD][decompose]" in line:
                    write_event("visual_extraction", message=clean_line)
                elif "[MinerU]" in line:
                    write_event("ocr", message=clean_line)
                elif "[structure]" in line:
                    write_event("structure_analysis", message=clean_line)
                elif "[LayerD]" in line:
                    write_event("adaptive_layering", message=clean_line)
                    if "started" in line:
                        self._progress(
                            "adaptive_layering",
                            clean_line,
                            78,
                        )
                if "PPT Generated" in line:
                    self._progress(
                        "local_render",
                        "PPT rebuilt; completing fidelity checks",
                        90,
                    )
            return_code = process.wait(timeout=self.timeout)
        except Exception:
            process.kill()
            process.wait()
            write_event("pipeline_interrupted")
            write_summary("interrupted", error_tail=output_lines[-40:])
            raise
        finally:
            pipeline_stream.close()

        if return_code != 0:
            details = "\n".join(output_lines[-40:])
            write_event("pipeline_failed", return_code=return_code)
            write_summary(
                "failed",
                return_code=return_code,
                error_tail=output_lines[-40:],
            )
            raise RuntimeError(f"LayerD editable export failed ({return_code}):\n{details}")

        generated = Path(output_file)
        if not generated.exists():
            write_event("pipeline_failed", reason="missing_output")
            write_summary("failed", error="PPTX artifact was not created")
            raise RuntimeError("LayerD workflow completed without a PPTX artifact")

        ir_path = run_dir / "editable_deck_ir.json"
        visual_count = 0
        if ir_path.exists():
            ir_data = json.loads(ir_path.read_text(encoding="utf-8"), strict=False)
            visual_count = sum(
                len(page.get("visual_elements", []))
                for page in ir_data.get("pages", [])
            )
        if visual_count == 0:
            write_event("pipeline_failed", reason="zero_visual_elements")
            write_summary("failed", error="No independent visual elements")
            raise RuntimeError("LayerD workflow produced zero independent visual elements")

        write_event("pipeline_completed", visual_element_count=visual_count)
        write_summary(
            "complete",
            visual_element_count=visual_count,
            output_file=str(generated),
            editable_deck_ir=str(ir_path),
            layerd_direct=True,
        )
        self._progress("complete", "LayerD editable PPTX generated", 100)
        return {
            "run_dir": str(run_dir),
            "output_file": str(output_file),
            "visual_element_count": visual_count,
            "pipeline_log": str(pipeline_log),
            "events": str(events_path),
            "run_summary": str(summary_path),
            "layerd_direct": True,
        }


class RemoteLayerDEditablePPTXRunner:
    """Client for the persistent GPU LayerD worker."""

    def __init__(
        self,
        *,
        worker_url: str,
        worker_token: str,
        image_model: str = "gemini-3.1-flash-image-preview",
        timeout: int = 7200,
        poll_seconds: float = 3,
        progress_callback: Optional[Callable[[str, str, int], None]] = None,
    ):
        self.worker_url = worker_url.rstrip('/')
        self.worker_token = worker_token
        self.image_model = image_model
        self.timeout = timeout
        self.poll_seconds = max(0.2, poll_seconds)
        self.progress_callback = progress_callback

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.worker_token}"}

    def _progress(self, step: str, message: str, percent: int) -> None:
        if self.progress_callback:
            self.progress_callback(step, message, percent)

    def run(
        self,
        *,
        image_paths: Iterable[str],
        output_file: str,
        work_dir: str,
    ) -> dict:
        if not self.worker_url or not self.worker_token:
            raise RuntimeError("GPU LayerD worker URL and token are required")

        run_dir = Path(work_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        package_path = run_dir / "gpu_worker_input.zip"
        images = [Path(path).resolve() for path in image_paths]
        with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "job.json",
                json.dumps(
                    {
                        "image_model": self.image_model,
                        "page_count": len(images),
                    }
                ),
            )
            for index, image_path in enumerate(images, 1):
                archive.write(
                    image_path,
                    f"images/{index:04d}{image_path.suffix.lower() or '.png'}",
                )

        self._progress("gpu_upload", "Uploading pages to GPU worker", 8)
        with package_path.open("rb") as stream:
            response = requests.post(
                f"{self.worker_url}/v1/jobs",
                headers={**self._headers, "Content-Type": "application/zip"},
                data=stream,
                timeout=(30, 600),
            )
        response.raise_for_status()
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + self.timeout

        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"GPU LayerD job {job_id} timed out")
            status_response = requests.get(
                f"{self.worker_url}/v1/jobs/{job_id}",
                headers=self._headers,
                timeout=30,
            )
            status_response.raise_for_status()
            job = status_response.json()
            status = job.get("status")
            if status == "completed":
                break
            if status == "failed":
                raise RuntimeError(
                    f"GPU LayerD job failed: {job.get('error') or 'unknown error'}"
                )
            percent = int(job.get("percent") or (15 if status == "queued" else 45))
            self._progress(
                "gpu_worker",
                job.get("current_step") or f"GPU job {status}",
                min(percent, 94),
            )
            time.sleep(self.poll_seconds)

        self._progress("gpu_download", "Downloading editable PPTX", 96)
        artifact_response = requests.get(
            f"{self.worker_url}/v1/jobs/{job_id}/artifact",
            headers=self._headers,
            stream=True,
            timeout=(30, 600),
        )
        artifact_response.raise_for_status()
        result_zip = run_dir / "gpu_worker_result.zip"
        with result_zip.open("wb") as stream:
            for chunk in artifact_response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    stream.write(chunk)

        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        copied = {}
        with zipfile.ZipFile(result_zip) as archive:
            for source_name, destination in (
                ("output.pptx", output_path),
                ("editable_deck_ir.json", run_dir / "editable_deck_ir.json"),
                ("render_refine_report.json", run_dir / "render_refine_report.json"),
                ("run_summary.json", run_dir / "run_summary.json"),
                ("pipeline.log", run_dir / "pipeline.log"),
                ("events.jsonl", run_dir / "events.jsonl"),
            ):
                if source_name not in archive.namelist():
                    continue
                with archive.open(source_name) as source, Path(destination).open("wb") as target:
                    shutil.copyfileobj(source, target)
                copied[source_name] = str(destination)
        if not output_path.exists():
            raise RuntimeError("GPU worker result did not contain output.pptx")

        summary_data = {}
        summary_path = run_dir / "run_summary.json"
        if summary_path.exists():
            summary_data = json.loads(summary_path.read_text(encoding="utf-8"))
        self._progress("complete", "GPU LayerD editable PPTX generated", 100)
        return {
            "run_dir": str(run_dir),
            "output_file": str(output_path),
            "visual_element_count": summary_data.get("visual_element_count", 0),
            "pipeline_log": copied.get("pipeline.log"),
            "events": copied.get("events.jsonl"),
            "run_summary": copied.get("run_summary.json"),
            "layerd_direct": True,
            "gpu_worker": True,
            "gpu_job_id": job_id,
        }
