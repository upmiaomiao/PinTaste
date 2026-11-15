"""Render the actual exported PPTX, never substitute its input page images."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from .wuyu_errors import PipelineError, check_cancel


def render_exported_pptx(pptx_path, config, expected_pages=None, cancel_check=None, max_pages=None, output_dir=None, page_numbers=None):
    configured = config.get('WUYU_SOFFICE_PATH')
    executable = str(configured) if configured else (shutil.which('libreoffice') or shutil.which('soffice') if os.name != 'nt' else None)
    if not executable or not Path(executable).is_file():
        return {'available': False, 'paths': [], 'status': 'unverified', 'reason': 'renderer_not_configured', 'page_count': None}
    source = Path(pptx_path).resolve()
    if not source.is_file() or source.suffix.lower() != '.pptx':
        raise PipelineError('render_input', '渲染输入必须是实际导出的PPTX。')
    timeout = max(5, min(180, float(config.get('WUYU_RENDER_TIMEOUT', 90))))
    with tempfile.TemporaryDirectory(prefix='wuyu-export-render-') as work:
        work = Path(work)
        pdf_dir = work / 'pdf'
        pdf_dir.mkdir()
        profile = work / 'profile'
        cmd = [executable, '-env:UserInstallation=' + profile.as_uri(), '--headless', '--convert-to', 'pdf', '--outdir', str(pdf_dir), str(source)]
        options = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            options['start_new_session'] = True
        process = subprocess.Popen(cmd, **options)
        deadline = time.monotonic() + timeout
        try:
            while True:
                check_cancel(cancel_check)
                if time.monotonic() > deadline:
                    raise PipelineError('render_timeout', '实际PPTX预览渲染超过时间限制。', True)
                try:
                    stdout, stderr = process.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=10)
            raise
        pdf = pdf_dir / (source.stem + '.pdf')
        if process.returncode != 0 or not pdf.is_file():
            return {'available': False, 'paths': [], 'status': 'unverified', 'reason': 'renderer_failed', 'page_count': None}
        doc, cancellation_error = None, None
        try:
            import pymupdf
            doc = pymupdf.open(pdf)
            if expected_pages is not None and len(doc) != expected_pages:
                raise PipelineError('render_page_count', '实际PPTX渲染页数不符。')
            out_dir = Path(output_dir).resolve() if output_dir else source.with_name(source.stem + '.previews')
            out_dir.mkdir(parents=True, exist_ok=True)
            paths = []
            selected = set(page_numbers) if page_numbers is not None else None
            if selected is not None and (not selected or any(type(n) is not int or not 1 <= n <= len(doc) for n in selected)):
                raise PipelineError('render_page_selection', '模板页号必须来自实际文档。')
            rendered_numbers = []
            for number, page in enumerate(doc, 1):
                if max_pages is not None and number > max_pages:
                    break
                if selected is not None and number not in selected:
                    continue
                try:
                    check_cancel(cancel_check)
                except Exception as error:
                    cancellation_error = error
                    raise
                if page.rect.width <= 0 or page.rect.height <= 0:
                    raise PipelineError('render_dimensions', '实际PPTX渲染页面尺寸无效。')
                scale = min(1280 / page.rect.width, 4096 / page.rect.height)
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                dest = out_dir / f'page-{number:03d}.png'
                pixmap.save(dest)
                paths.append(str(dest.resolve()))
                rendered_numbers.append(number)
            document_page_count = len(doc)
            return {'available': True, 'paths': paths, 'page_numbers': rendered_numbers, 'status': 'rendered_actual_export', 'renderer': 'LibreOffice + PyMuPDF', 'page_count': len(paths), 'document_page_count': document_page_count, 'fidelity': 'unverified'}
        except PipelineError:
            raise
        except Exception:
            if cancellation_error is not None:
                raise cancellation_error
            return {'available': False, 'paths': [], 'status': 'unverified', 'reason': 'pdf_preview_decode_failed', 'page_count': None}
        finally:
            if doc is not None:
                doc.close()
