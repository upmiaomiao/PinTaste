"""Codia cloud PDF-to-PPTX export service.

Converts generated slide images into an editable PPTX through Codia's hosted
Open API (no local GPU worker). Flow: images -> PDF -> upload -> pdf_to_ppt
task -> poll -> download.
"""
import os
import time

import requests

from services.export_service import ExportService

CODIA_API_BASE = 'https://openapi.codia.ai'


class CodiaCloudPPTXExporter:
    """Uploads a slide PDF to Codia and downloads the editable PPTX."""

    def __init__(self, api_key, api_base=CODIA_API_BASE, timeout=7200, poll_seconds=5, progress_callback=None):
        self.api_key = api_key
        self.api_base = api_base.rstrip('/')
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.progress_callback = progress_callback

    def _headers(self):
        return {'Authorization': f'Bearer {self.api_key}'}

    def _progress(self, step, message, percent):
        if self.progress_callback:
            self.progress_callback(step, message, percent)

    def run(self, image_paths, output_file, work_dir=None, aspect_ratio='16:9'):
        if not self.api_key:
            raise RuntimeError('CODIA_API_KEY is not configured')

        work_dir = work_dir or os.path.join(os.path.dirname(output_file), '.codia')
        os.makedirs(work_dir, exist_ok=True)

        # 1. images -> PDF (reuses the plain export path's img2pdf/Pillow logic)
        self._progress('build_pdf', '合成 PDF', 10)
        pdf_path = os.path.join(work_dir, 'input.pdf')
        ExportService.create_pdf_from_images(image_paths, output_file=pdf_path, aspect_ratio=aspect_ratio)

        # 2. upload PDF
        self._progress('upload', '上传 PDF 到 Codia', 20)
        upload_id = self._upload_pdf(pdf_path)

        # 3. create pdf_to_ppt task
        self._progress('create_task', '创建 pdf_to_ppt 任务', 30)
        task_id = self._create_task(upload_id)

        # 4. poll until succeeded
        self._progress('poll', 'Codia 转换中', 40)
        ppt_url = self._poll(task_id)

        # 5. download
        self._progress('download', '下载 PPTX', 90)
        self._download(ppt_url, output_file)

        self._progress('complete', 'Codia 可编辑 PPTX 导出完成', 100)
        return {'run_dir': work_dir, 'method': 'codia_cloud_api'}

    def _upload_pdf(self, pdf_path):
        with open(pdf_path, 'rb') as f:
            resp = requests.post(
                f'{self.api_base}/v2/open/uploads',
                headers=self._headers(),
                files={'file': ('input.pdf', f, 'application/pdf')},
                timeout=600,
            )
        return self._check(resp, 'upload', 'upload_id')

    def _create_task(self, upload_id):
        resp = requests.post(
            f'{self.api_base}/v2/open/tasks',
            headers={**self._headers(), 'Content-Type': 'application/json'},
            json={'operation': 'pdf_to_ppt', 'input': {'upload_id': upload_id, 'title': 'presentation'}},
            timeout=60,
        )
        return self._check(resp, 'create task', 'task_id')

    def _check(self, resp, action, field):
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(f'Codia {action} failed: HTTP {resp.status_code} {resp.text[:200]}')
        if not (200 <= resp.status_code < 300) or data.get('code') != 0:
            raise RuntimeError(f'Codia {action} failed: {data.get("message") or resp.text[:200]}')
        value = (data.get('data') or {}).get(field)
        if not value:
            raise RuntimeError(f'Codia {action} returned no {field}')
        return value

    def _poll(self, task_id):
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            resp = requests.get(f'{self.api_base}/v2/open/tasks/{task_id}', headers=self._headers(), timeout=30)
            data = resp.json()
            d = data.get('data') or {}
            status = d.get('status')
            if status == 'succeeded':
                ppt_url = (d.get('result') or {}).get('ppt_url')
                if not ppt_url:
                    raise RuntimeError('Codia task succeeded but returned no ppt_url')
                return ppt_url
            if status in ('failed', 'canceled'):
                raise RuntimeError(f'Codia task {status}: {d.get("error") or d}')
            time.sleep(self.poll_seconds)
        raise TimeoutError(f'Codia task {task_id} timed out')

    def _download(self, ppt_url, output_file):
        resp = requests.get(ppt_url, timeout=600)
        if resp.status_code != 200:
            raise RuntimeError(f'Codia download failed: HTTP {resp.status_code}')
        with open(output_file, 'wb') as f:
            f.write(resp.content)
