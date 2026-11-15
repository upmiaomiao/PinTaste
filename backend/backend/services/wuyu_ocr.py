"""OCR: DeepSeek official OCR through the private model gateway (op 175), else local Tesseract.

No source-instruction execution. The remote route is used only when the backend is
fenced to the private gateway and WUYU_OCR_PROVIDER is not 'local'; if the gateway
does not offer OCR (404) or the call fails, the existing local engine is tried.
"""
import csv
import io
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
from .wuyu_errors import PipelineError, check_cancel

REMOTE_OCR_PURPOSE = 'text_recognition'
REMOTE_OCR_MAX_EDGE = 4096
REMOTE_OCR_MAX_LINES = 1000
_HTML_TAG = re.compile(r'<[^>]{1,200}>')


def _remote_transport(config):
    """(origin, internal key) of the fenced private gateway, or None."""
    provider = str(config.get('WUYU_OCR_PROVIDER', os.getenv('WUYU_OCR_PROVIDER') or 'auto')).lower()
    if provider not in ('auto', 'deepseek', 'local'):
        raise PipelineError('ocr_config', 'OCR引擎配置只支持auto、deepseek或local。')
    if provider == 'local':
        return None
    try:
        from .wuyu_providers import WuyuGateway
        return WuyuGateway(config)._forced_transport()
    except Exception:
        # op 176: any configuration problem means "no remote OCR"; local parsing must
        # never fail because the optional remote route cannot be resolved.
        return None


def remote_text_lines(text):
    """Official page text (markdown, HTML tables) -> plain evidence lines."""
    text = str(text or '')
    # One table row per line: OCR emits each <td> on its own line.
    text = re.sub(r'<tr[^>]{0,100}>(.*?)</tr>', lambda m: re.sub(r'\s*\n\s*', ' ', m.group(1)) + '\n',
                  text, flags=re.I | re.S)
    text = re.sub(r'</t[dh]>', ' | ', text, flags=re.I)
    text = re.sub(r'</tr>|<br\s*/?>', '\n', text, flags=re.I)
    text = _HTML_TAG.sub('', text)
    lines, previous = [], None
    for raw in text.splitlines():
        line = re.sub(r'\s+', ' ', raw).strip().strip('|').strip()
        line = re.sub(r'^#{1,6}\s+', '', line)
        # A page that hit the output limit may loop; never repeat one line back to back.
        if not line or line == previous:
            continue
        lines.append(line)
        previous = line
        if len(lines) >= REMOTE_OCR_MAX_LINES:
            break
    return lines


def remote_ocr_image(image, config, cancel_check=None, timeout=None):
    """Return an ocr_image-shaped result, or None when the gateway has no OCR route."""
    transport = _remote_transport(config)
    if not transport:
        return None
    origin, key = transport
    check_cancel(cancel_check)
    if image.width * image.height > 40_000_000 or min(image.size) <= 0:
        raise PipelineError('image_limit', 'OCR输入像素超过限制。')
    if image.mode == 'RGBA' or 'transparency' in image.info:
        from PIL import Image
        rgba = image.convert('RGBA')
        prepared = Image.new('RGB', image.size, 'white'); prepared.paste(rgba, mask=rgba.getchannel('A'))
    else:
        prepared = image.convert('RGB')
    prepared.thumbnail((REMOTE_OCR_MAX_EDGE, REMOTE_OCR_MAX_EDGE))
    buffer = io.BytesIO()
    prepared.save(buffer, 'PNG')
    seconds = min(190, max(5, float(timeout if timeout is not None else 190)))
    import httpx
    factory = config.get('WUYU_OCR_CLIENT_FACTORY') if config.get('TESTING') else None
    try:
        with (factory or httpx.Client)(timeout=seconds, follow_redirects=False, trust_env=False) as client:
            response = client.post(origin + '/v1/tools/ocr', headers={'Authorization': 'Bearer ' + key},
                                   data={'purpose': REMOTE_OCR_PURPOSE},
                                   files={'file': ('page.png', buffer.getvalue(), 'image/png')})
    except Exception:
        check_cancel(cancel_check)
        return {'available': False, 'status': 'pending', 'lines': [], 'reason': 'remote_ocr_unavailable'}
    check_cancel(cancel_check)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        return {'available': False, 'status': 'pending', 'lines': [],
                'reason': 'remote_ocr_rate_limited' if response.status_code == 429 else 'remote_ocr_failed'}
    try:
        value = response.json()
        page = value['pages'][0]
        text, finish = page.get('text', ''), page.get('finish_reason')
    except Exception:
        return {'available': False, 'status': 'pending', 'lines': [], 'reason': 'remote_ocr_invalid_response'}
    width, height = image.size
    lines = [{'text': line, 'confidence': 0.9, 'region': [0, 0, width, height]} for line in remote_text_lines(text)]
    return {'available': True, 'status': 'completed' if lines else 'pending',
            'reason': None if lines else 'remote_ocr_no_recognized_text', 'lines': lines,
            'engine': 'deepseek_ocr', 'language': 'auto', 'machine_ocr': True,
            'region_precision': 'page', 'output_truncated': finish == 'length'}


def enabled(config):
    choice = str(config.get('WUYU_OCR_ENABLED', 'auto')).lower()
    if choice == 'auto':
        if _remote_transport(config):
            return True
        executable = config.get('WUYU_TESSERACT_PATH') or shutil.which('tesseract')
        return bool(executable and Path(executable).is_file())
    return choice in ('true', '1')


def ocr_image(image, config, cancel_check=None, timeout=None):
    pending = {'available': False, 'status': 'pending', 'lines': []}
    choice = str(config.get('WUYU_OCR_ENABLED', 'auto')).lower()
    if choice in ('false', '0', 'off'):
        return {**pending, 'reason': 'local_ocr_disabled'}
    if choice not in ('auto', 'true', '1'):
        raise PipelineError('ocr_config', 'OCR启用配置只支持auto、true或false。')
    remote = remote_ocr_image(image, config, cancel_check, timeout)
    if remote is not None and remote['lines']:
        return remote
    local = local_ocr_image(image, config, cancel_check, timeout)
    if remote is not None and not local['lines']:
        return remote
    return local


def local_ocr_image(image, config, cancel_check=None, timeout=None):
    pending = {'available': False, 'status': 'pending', 'lines': []}
    choice = str(config.get('WUYU_OCR_ENABLED', 'auto')).lower()
    if choice in ('false', '0', 'off'):
        return {**pending, 'reason': 'local_ocr_disabled'}
    if choice not in ('auto', 'true', '1'):
        raise PipelineError('ocr_config', 'OCR启用配置只支持auto、true或false。')
    executable = config.get('WUYU_TESSERACT_PATH') or shutil.which('tesseract')
    if not executable or not Path(executable).is_file():
        return {**pending, 'reason': 'local_ocr_executable_unavailable'}
    language = str(config.get('WUYU_OCR_LANG', 'chi_sim+eng'))
    if not re.fullmatch(r'[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*', language):
        raise PipelineError('ocr_config', '本地OCR语言配置无效。')
    seconds = min(60, max(.2, float(config.get('WUYU_OCR_TIMEOUT', 20))))
    if timeout is not None:
        seconds = min(seconds, max(.2, timeout))
    if image.width * image.height > 40_000_000 or min(image.size) <= 0:
        raise PipelineError('image_limit', 'OCR输入像素超过限制。')
    check_cancel(cancel_check)
    size = image.size
    if image.mode == 'RGBA' or 'transparency' in image.info:
        from PIL import Image
        rgba = image.convert('RGBA')
        prepared = Image.new('RGB', image.size, 'white'); prepared.paste(rgba, mask=rgba.getchannel('A'))
    else:
        prepared = image.convert('RGB')
    prepared.thumbnail((2500, 2500))
    scale_x, scale_y = size[0] / prepared.width, size[1] / prepared.height
    with tempfile.TemporaryDirectory(prefix='wuyu-local-ocr-') as work:
        work = Path(work)
        input_path, output_path = work / 'input.png', work / 'output.tsv'
        prepared.save(input_path)
        env = dict(os.environ); env['OMP_THREAD_LIMIT'] = '1'
        options = {'stderr': subprocess.DEVNULL, 'env': env}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            options['start_new_session'] = True
        process = None
        try:
            with output_path.open('wb') as output:
                process = subprocess.Popen([str(executable), str(input_path), 'stdout', '-l', language, '--psm', '3', 'tsv'], stdout=output, **options)
                deadline = time.monotonic() + seconds
                while process.poll() is None:
                    check_cancel(cancel_check)
                    if time.monotonic() >= deadline or output_path.stat().st_size > 8 * 1024 * 1024:
                        raise PipelineError('ocr_timeout', '本地OCR超过单页时间或输出规模限制。')
                    time.sleep(.05)
            if process.returncode != 0 or output_path.stat().st_size > 8 * 1024 * 1024:
                return {**pending, 'reason': 'local_ocr_failed_or_language_unavailable'}
            text = output_path.read_text(encoding='utf-8-sig', errors='replace')
        except PipelineError as error:
            if error.code == 'cancelled':
                raise
            return {**pending, 'reason': error.code}
        except (OSError, ValueError):
            return {**pending, 'reason': 'local_ocr_process_failed'}
        finally:
            if process is not None and process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=10)
    lines = {}
    try:
        for count, word in enumerate(csv.DictReader(io.StringIO(text), delimiter='\t')):
            if count >= 100000:
                break
            content = str(word.get('text', '')).strip()
            confidence = float(word.get('conf', -1))
            if not content or not math.isfinite(confidence) or confidence < 0:
                continue
            x, y, w, h = (float(word[k]) for k in ('left', 'top', 'width', 'height'))
            if any(not math.isfinite(value) for value in (x, y, w, h)) or min(x, y, w, h) < 0 or x + w > prepared.width or y + h > prepared.height:
                continue
            key = tuple(word.get(k) for k in ('page_num', 'block_num', 'par_num', 'line_num'))
            if key not in lines:
                if len(lines) >= 1000:
                    break
                lines[key] = {'words': [], 'scores': [], 'left': x, 'top': y, 'right': x + w, 'bottom': y + h}
            line = lines[key]
            line['words'].append(content); line['scores'].append(max(0, min(100, confidence)))
            line['left'], line['top'] = min(line['left'], x), min(line['top'], y)
            line['right'], line['bottom'] = max(line['right'], x + w), max(line['bottom'], y + h)
    except (ValueError, TypeError, csv.Error):
        return {**pending, 'reason': 'local_ocr_invalid_tsv'}
    extracted = [{'text': ' '.join(line['words']), 'confidence': sum(line['scores']) / len(line['scores']) / 100,
                  'region': [line['left'] * scale_x, line['top'] * scale_y,
                             (line['right'] - line['left']) * scale_x, (line['bottom'] - line['top']) * scale_y]}
                 for line in lines.values()]
    return {'available': True, 'status': 'completed' if extracted else 'pending',
            'reason': None if extracted else 'local_ocr_no_recognized_text', 'lines': extracted,
            'engine': 'local_tesseract', 'language': language, 'machine_ocr': True}
