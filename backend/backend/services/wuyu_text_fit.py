"""Fit native text to its own immutable box using the renderer's installed fonts.

This is an export adaptation, never a TraceSpec/content rewrite. Explicit newlines,
paragraph formatting and geometry stay intact. Measurement uses the required
Arial/YaHei script faces. Only a demonstrably overflowing font size is reduced;
a box which already fits is left untouched.
"""
from functools import lru_cache
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata

from .wuyu_errors import PipelineError
from .wuyu_typography import CJK_FONT, LATIN_FONT
from wuyu_font_cache import load_font as _load_font

_SCALE = 4


def _cjk(char):
    code = ord(char)
    return (0x2E80 <= code <= 0xA4CF or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF or 0xFE30 <= code <= 0xFE4F
            or 0xFF00 <= code <= 0xFFEF or 0x20000 <= code <= 0x3134F)


@lru_cache(maxsize=128)
def resolve_font(family, bold=False, italic=False, cjk=False):
    """Resolve like Fontconfig on Linux, or the Windows installed-font registry.

    Font paths are private implementation details, absent from fit reports.
    File-backed faces use the shared bounded export measurement cache.
    """
    if shutil.which('fc-match'):
        pattern = family + (':weight=bold' if bold else '') + (':slant=italic' if italic else '')
        if cjk:
            pattern += ':lang=zh-cn:charset=4e00'
        try:
            result = subprocess.run(['fc-match', '-f', '%{file}\n%{index}\n%{family}', pattern],
                                    capture_output=True, text=True, timeout=4, check=True)
            fields = result.stdout.splitlines()
            if len(fields) >= 3 and Path(fields[0]).is_file():
                return fields[0], int(fields[1] or 0), fields[2]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if os.name == 'nt':
        import winreg
        # Windows registers YaHei under its English family name even when the
        # DrawingML typeface uses the public Chinese name.
        desired_names = [family, 'Microsoft YaHei'] if family in ('微软雅黑', 'Microsoft YaHei') else [family]
        desired = [re.sub(r'[^\w]', '', name.casefold()) for name in desired_names]
        best = None
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                               r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts') as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    name, filename, _ = winreg.EnumValue(key, index)
                    normalized = re.sub(r'[^\w]', '', name.casefold().replace('(truetype)', ''))
                    if not any(normalized.startswith(name) for name in desired):
                        continue
                    is_bold = 'bold' in normalized
                    is_italic = 'italic' in normalized or 'oblique' in normalized
                    score = int(is_bold != bold) * 2 + int(is_italic != italic) * 2 + len(normalized) / 1000
                    path = Path(filename)
                    if not path.is_absolute():
                        path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts' / path
                    if path.is_file() and (best is None or score < best[0]):
                        best = (score, str(path), 0, name.replace(' (TrueType)', ''))
            if best and (not cjk or 'noto' in family.casefold() or 'yahei' in family.casefold() or family == '微软雅黑'):
                return best[1:]
        except OSError:
            pass
        # Existing project font is a genuine CJK font, not a PIL bitmap fallback.
        if cjk or 'noto' in family.casefold():
            bundled = Path(__file__).resolve().parents[1] / 'fonts' / 'NotoSansSC-Regular.ttf'
            if bundled.is_file():
                return str(bundled), 0, 'Noto Sans SC'
        if best:
            return best[1:]
    return None


def _font(resolved, size_pt):
    path, index, _ = resolved
    stat = Path(path).stat()
    return _load_font(path, index, max(1, round(size_pt * 96 / 72 * _SCALE)),
                      stat.st_mtime_ns, stat.st_size)


def _runs(text):
    cjk_context = any(_cjk(char) for char in text)
    def kind(char):
        if char.isdecimal() or unicodedata.name(char, '').startswith('FULLWIDTH LATIN'):
            return False
        # Shared mathematical/punctuation glyphs stay with an adjacent Asian
        # script in LO (e.g. <= U+2264 in the actual mixed-script metric label).
        return _cjk(char) or (cjk_context and unicodedata.east_asian_width(char) == 'A'
                              and not char.isalnum() and not char.isspace())
    start = 0
    for index in range(1, len(text)):
        if kind(text[index]) != kind(text[index - 1]):
            yield text[start:index], kind(text[index - 1])
            start = index
    if text:
        yield text[start:], kind(text[-1])


def _line_metrics(text, fonts):
    advance, left, right, ink_top, ink_bottom, line_height = 0., 0., 0., math.inf, -math.inf, 0.
    previous = None
    for run, cjk in _runs(text or ' '):
        font = fonts.get(cjk) or next(iter(fonts.values()))
        if previous is not None and previous[1] != cjk:
            latin = previous[0][-1] if cjk else run[0]
            other = run[0] if cjk else previous[0][-1]
            if latin.isascii() and latin.isalnum() and not other.isspace():
                # DrawingML/LO Asian-Western automatic spacing is a quarter em.
                # Real PDF preflight showed this twice around the digit in
                # "题居首或距首位≤1分"; glyph advances alone undercounted 15px.
                advance += font.size / _SCALE / 4
        box = font.getbbox(run)
        left = min(left, advance + box[0] / _SCALE)
        right = max(right, advance + box[2] / _SCALE)
        ink_top = min(ink_top, box[1] / _SCALE)
        ink_bottom = max(ink_bottom, box[3] / _SCALE)
        advance += font.getlength(run) / _SCALE
        line_height = max(line_height, sum(font.getmetrics()) / _SCALE)
        previous = run, cjk
    return max(right, advance) - left, max(0., ink_bottom - ink_top), line_height


def _wrapped_lines(text, available_width, fonts):
    if not text:
        return ['']
    result, line = [], ''
    # Keep Latin words together like DrawingML; CJK can wrap between glyphs.
    for token in re.findall(r'[A-Za-z0-9_\u00c0-\u024f]+|[^\S\n]+|.', text):
        if line and _line_metrics(line + token, fonts)[0] > available_width:
            result.append(line.rstrip())
            line = '' if token.isspace() else token
        else:
            line += token
        if _line_metrics(line, fonts)[0] > available_width:
            part = ''
            for char in line:
                if part and _line_metrics(part + char, fonts)[0] > available_width:
                    result.append(part)
                    part = char
                else:
                    part += char
            line = part
    result.append(line.rstrip())
    return result


def measure_text(el, size_pt, resolved):
    """Return glyph extents in canvas pixels, preserving explicit paragraph breaks."""
    fonts = {kind: _font(value, size_pt) for kind, value in resolved.items()}
    margins = el.get('margins', [0, 0, 0, 0])
    available_width = el['bounds'][2] - margins[0] - margins[2]
    max_width, height = 0., 0.
    formats = el.get('paragraph_formats', [])
    previous_advance = None
    for number, paragraph in enumerate(el['text'].split('\n')):
        fmt = formats[number] if number < len(formats) else {}
        height += float(fmt.get('space_before', 0)) * 96 / 72
        lines = _wrapped_lines(paragraph, available_width, fonts) if el.get('wrap', False) else [paragraph]
        for line in lines:
            width, ink_height, natural_advance = _line_metrics(line, fonts)
            max_width = max(max_width, width)
            if previous_advance is None:
                height += ink_height
            else:
                height += previous_advance
            # Multiple line spacing is relative to the resolved font's natural line cell.
            previous_advance = natural_advance * float(fmt.get('line_spacing', 1))
        height += float(fmt.get('space_after', 0)) * 96 / 72
    return max_width, height


def fit_native_text(el):
    """Return an export-only font override and a safe measurement record.

    The original font dict and trace are not mutated. Existing >=6pt text
    keeps its 6pt fit floor; a valid smaller original is never enlarged to 6pt.
    Text which cannot fit at its floor is rejected, never truncated.
    """
    original = dict(el.get('font', {}))
    size = float(original.get('size', 18)) * (.75 if original.get('size_unit', 'pt') == 'px' else 1)
    family = {'latin': LATIN_FONT, 'east_asian': CJK_FONT}
    kinds = {kind for _, kind in _runs(el['text'])} or {False}
    resolved = {kind: resolve_font(CJK_FONT if kind else LATIN_FONT, original.get('bold', False), original.get('italic', False), kind)
                for kind in kinds}
    if not all(resolved.values()):
        return original, {'status': 'unverified_font_unavailable', 'font_family': family,
                          'text_sha256': hashlib.sha256(el['text'].encode()).hexdigest()}
    margins = el.get('margins', [0, 0, 0, 0])
    available = [el['bounds'][2] - margins[0] - margins[2],
                 el['bounds'][3] - margins[1] - margins[3]]
    measured = measure_text(el, size, resolved)
    # Do not touch already-fitting boxes. The small inset is only used AFTER a
    # genuine overflow is established, to cover FreeType/LO fractional rounding.
    if measured[0] <= available[0] and measured[1] <= available[1]:
        return original, None
    target = [max(.1, value - 1.5) for value in available]
    minimum = min(6., size)
    low, high = minimum, size
    if any(value > limit for value, limit in zip(measure_text(el, low, resolved), target)):
        raise PipelineError('native_text_overflow', '原生文字无法在现有文本框内完整显示，请缩短文字或调整版式。')
    for _ in range(16):
        middle = (low + high) / 2
        extent = measure_text(el, middle, resolved)
        if all(value <= limit for value, limit in zip(extent, target)):
            low = middle
        else:
            high = middle
    # OOXML hundredth-point precision: round DOWN, then recheck actual metrics.
    fitted = math.floor(low * 100) / 100
    override = {**original, 'size': fitted, 'size_unit': 'pt'}
    final = measure_text(el, fitted, resolved)
    return override, {'status': 'shrunk_to_own_box', 'original_size_pt': size, 'size_pt': fitted,
                      'available_px': available, 'original_extent_px': list(measured),
                      'fitted_extent_px': list(final), 'font_family': family,
                      'resolved_families': sorted({value[2] for value in resolved.values()}),
                      'text_sha256': hashlib.sha256(el['text'].encode()).hexdigest(),
                      'explicit_lines': len(el['text'].split('\n')), 'wrap': el.get('wrap', False),
                      'geometry_unchanged': True, 'content_unchanged': True}
