"""Local-only, per-page native QA with explicit confirmed-image fallbacks."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time

from .wuyu_errors import PipelineError, check_cancel
from .wuyu_generation import is_textless_icon
from .wuyu_trace_export import export_trace_specs, inspect_pptx, validate_trace, write_presentation_notes


def _probe_fingerprint(manifest, source_hash, confirmed, textless):
    dependencies = []
    for element in manifest['elements']:
        if element.get('type') != 'image':continue
        for key in ('image_path', 'raster_fallback_path'):
            if not element.get(key):continue
            path = Path(element[key])
            if not path.is_file():
                raise PipelineError('missing_trace_asset', '描摹图像素材不存在，不能导出占位符。')
            dependencies.append({'field': key, 'path': str(path),
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return hashlib.sha256(json.dumps({'version': 1, 'manifest': manifest, 'approved_image_sha256': source_hash,
        'confirmed_copy': confirmed, 'textless': textless, 'media_dependencies': dependencies},
        ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def export_hybrid_deck(pages, output_path, config, cancel_check=None, *, probe_cache=None):
    from .wuyu_pipeline import assert_confirmed_copy, confirmed_copy_fragments
    from .wuyu_structured import verified_sources, native_data
    from .wuyu_providers import decode_image
    check_cancel(cancel_check)
    started = time.monotonic()
    probe_cache = probe_cache if isinstance(probe_cache, dict) else {}
    cache_hits, cache_misses, probe_seconds = 0, 0, 0.
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    first = None
    traces, native_pages, image_pages, reasons, image_hashes = [], [], [], {}, {}
    note_pages = [{'spec': deepcopy(page.get('spec', {}))} for page in pages]
    with tempfile.TemporaryDirectory(prefix='.hybrid-', dir=target.parent) as folder:
        temporary = Path(folder)
        source_cache = {}
        for number, page in enumerate(pages, 1):
            check_cancel(cancel_check)
            source = Path(page['image_path'])
            source_bytes = source.read_bytes()
            source_hash = hashlib.sha256(source_bytes).hexdigest()
            width, height = decode_image(source_bytes).size
            first = first or (width, height)
            try:
                manifest = deepcopy(page.get('editable_manifest'))
                if not manifest and page.get('manifest_path'):
                    manifest = json.loads(Path(page['manifest_path']).read_text(encoding='utf-8'))
                if not isinstance(manifest, dict):
                    code = page.get('trace_status_reason', 'trace_unavailable')
                    raise PipelineError(code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', code) else 'trace_unavailable', '当前页尚无可用描摹。')
                if (manifest.get('mode') != 'reference_only' or manifest.get('source_image_sha256') != source_hash
                        or any(el.get('asset_origin') == 'verified_project_original'
                               for el in manifest.get('elements', []) if isinstance(el, dict))):
                    raise PipelineError('stale_trace', '描摹未绑定当前确认页图。')
                validate_trace(manifest, width, height, require_data=True)
                if (width, height) != first:
                    raise PipelineError('canvas_mismatch', '描摹画布与整册不一致。')
                assert_confirmed_copy(manifest, page.get('spec', {}))
                if any(el.get('type') in ('table', 'chart') for el in manifest['elements']):
                    actual_sources = verified_sources(page.get('spec', {}), config, cancel_check, source_cache)
                    for element in manifest['elements']:
                        if element.get('type') in ('table', 'chart'):
                            actual = native_data(element, actual_sources)
                            if element.get('source_data_verified') is not True or any(element.get(k) != v for k, v in actual.items()):
                                raise PipelineError('stale_structured_data', '表格或图表的原始数据不匹配。')
                fingerprint = _probe_fingerprint(manifest, source_hash,
                    confirmed_copy_fragments(page.get('spec', {})), is_textless_icon(page.get('spec', {})))
                if probe_cache.get(fingerprint) is True:
                    cache_hits += 1
                else:
                    cache_misses += 1
                    probe_started = time.monotonic()
                    probe = temporary / f'page-{number}.pptx'
                    export_trace_specs([manifest], probe, cancel_check)
                    checked = inspect_pptx(probe, 1, editable=True,
                                           expected_texts=[confirmed_copy_fragments(page.get('spec', {}))],
                                           textless_icon_page_numbers={1} if is_textless_icon(page.get('spec', {})) else set())
                    probe_seconds += time.monotonic() - probe_started
                    if not checked['passed']:
                        raise PipelineError('native_page_qa_failed', '本页原生对象未通过独立检查。')
                    while len(probe_cache) >= 80:del probe_cache[next(iter(probe_cache))]
                    probe_cache[fingerprint] = True
                traces.append(manifest)
                native_pages.append(number)
            except (PipelineError, OSError, ValueError, KeyError, TypeError, IndexError) as error:
                check_cancel(cancel_check)
                code = getattr(error, 'code', 'invalid_trace')
                code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', code) else 'invalid_trace'
                image_pages.append(number)
                reasons[str(number)] = code
                image_hashes[number] = source_hash
                traces.append({'canvas': {'width': first[0], 'height': first[1]}, 'elements': [
                    {'type': 'image', 'bounds': [0, 0, first[0], first[1]], 'image_path': str(source)}]})
                spec = note_pages[number - 1].setdefault('spec', {})
                notes = spec.get('speaker_notes', '')
                if not isinstance(notes, str):
                    raise PipelineError('invalid_speaker_notes', '演讲备注必须为文本。') from None
                # The separate nonvisual note identifies this page even after
                # the PPTX leaves the application; long evidence is bounded.
                from .wuyu_content_contracts import bound_speaker_notes
                spec['speaker_notes'] = bound_speaker_notes(notes)
        candidate = temporary / 'hybrid.pptx'
        export_trace_specs(traces, candidate, cancel_check)
        write_presentation_notes(candidate, note_pages)
        from pptx import Presentation
        deck = Presentation(candidate)
        for number in image_pages:
            slide = deck.slides[number - 1]
            slide.shapes[0].name = f'IMAGE_ONLY_FALLBACK_PAGE_{number}'
            notes = slide.notes_slide.notes_text_frame
            notes.text = notes.text + f'\n\n混合PPT说明：第{number}页为已确认图片，页内文字与图形不可编辑。原因：{reasons[str(number)]}。'
        deck.save(candidate)
        qa = inspect_pptx(candidate, len(pages), editable=bool(native_pages), native_page_numbers=set(native_pages),
                          expected_texts=[confirmed_copy_fragments(page.get('spec', {})) if number in native_pages else []
                                          for number, page in enumerate(pages, 1)], fallback_image_hashes=image_hashes,
                          textless_icon_page_numbers={number for number, page in enumerate(pages, 1) if is_textless_icon(page.get('spec', {}))})
        qa.update(editable_pages=native_pages, image_only_pages=image_pages, fallback_reasons=reasons,
                  fidelity='unverified; per-page structural validation is not visual approval',
                  visual_fidelity='unverified', preview_unavailable=True)
        qa['export_performance'] = {'version': 1, 'total_seconds': round(time.monotonic()-started, 6),
            'native_probe_seconds': round(probe_seconds, 6), 'native_probe_cache_hits': cache_hits,
            'native_probe_cache_misses': cache_misses, 'page_count': len(pages), 'full_deck_qa_executed': True}
        if not qa['passed']:
            raise PipelineError('hybrid_export_qa_failed', '混合PPT结构检查失败。')
        check_cancel(cancel_check)
        candidate.replace(target)
    qa_path = target.with_name(target.name + '.qa.json')
    qa_path.write_text(json.dumps(qa, ensure_ascii=False, indent=2), encoding='utf-8')
    editability = 'mixed_native_and_images' if native_pages and image_pages else 'native_trace_objects' if native_pages else 'image_based'
    return {'path': str(target.resolve()), 'format': 'hybrid_pptx', 'qa': qa, 'qa_path': str(qa_path),
            'editability': editability, 'editable_pages': native_pages, 'image_only_pages': image_pages,
            'fallback_reasons': reasons, 'preview_paths': [], 'reference_image_paths': [p['image_path'] for p in pages],
            'model_mode': 'local_artifact_export', 'provider_called': False}
