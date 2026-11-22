"""Bounded local evidence extraction used by FileParserService and WuyuPipeline.

Does not upload documents, run macros or execute source instructions. OCR is local only.
"""
import csv
import hashlib
import io
import json
import re
import stat
import time
from pathlib import Path, PurePosixPath
import posixpath
import zipfile
import xml.etree.ElementTree as ET
from PIL import Image
from .wuyu_errors import PipelineError, check_cancel

NS = {'p': 'http://schemas.openxmlformats.org/presentationml/2006/main',
      'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
      'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
      'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}


def word_text(node):
    """Keep math, tabs and explicit line breaks in their original XML order."""
    text_tags = {'{' + NS['w'] + '}t', '{http://schemas.openxmlformats.org/officeDocument/2006/math}t'}
    return ''.join((item.text or '') if item.tag in text_tags else '\t' if item.tag == '{' + NS['w'] + '}tab'
                   else '\n' if item.tag in {'{' + NS['w'] + '}br', '{' + NS['w'] + '}cr'} else ''
                   for item in node.iter())


def allowed_file(path, roots):
    p = Path(path).resolve()
    resolved = [Path(r).resolve() for r in roots if r]
    if not resolved or not any(p == r or r in p.parents for r in resolved):
        raise PipelineError('unsafe_path', '材料路径不在配置的项目上传目录内。')
    if not p.is_file():
        raise PipelineError('material_not_found', '材料文件不存在。')
    return p


def _pdf_encrypted(document):
    return bool(document.is_encrypted or document.needs_pass or
                (document.metadata or {}).get('encryption'))


def read_verified_pdf_page(path, config, expected_sha256, page_number, cancel_check=None):
    """Read one cited, hash-verified original page for strict evidence checks.

    This never modifies stored evidence or calls a provider. Failed extraction
    does not supply a permissive fallback for a quantitative claim.
    """
    started = time.monotonic()
    max_seconds = min(30, max(1, float(config.get('WUYU_PDF_VERIFY_MAX_SECONDS', 15))))
    max_chars = min(200000, max(1, int(config.get('WUYU_PDF_VERIFY_MAX_CHARS', 120000))))
    cancellation_error = None
    def checkpoint():
        nonlocal cancellation_error
        if cancellation_error is not None:
            raise cancellation_error
        try:
            check_cancel(cancel_check)
        except Exception as error:
            cancellation_error = error
            raise
        if time.monotonic() - started > max_seconds:
            raise PipelineError('material_timeout', '原PDF页核对超过本地处理时间上限。')
    checkpoint()
    max_pages = max(1, min(int(config.get('WUYU_MAX_MATERIAL_PAGES', 2000)), 10000))
    if not isinstance(page_number, int) or isinstance(page_number, bool) or not 1 <= page_number <= max_pages:
        raise PipelineError('invalid_pdf_page', '原PDF核对页码超出明确配置的页数边界。')
    if not isinstance(expected_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_sha256):
        raise PipelineError('material_hash_mismatch', '原PDF缺少可核对的来源校验和。')
    roots = config.get('WUYU_ALLOWED_MATERIAL_ROOTS') or [config.get('UPLOAD_FOLDER')]
    roots = [roots] if isinstance(roots, str) else roots
    original = allowed_file(path, roots)
    if original.suffix.lower() != '.pdf':
        raise PipelineError('invalid_pdf', '原页复核只接受PDF来源。')
    if original.stat().st_size > min(200 * 1024 * 1024, int(config.get('WUYU_MAX_MATERIAL_BYTES', 200 * 1024 * 1024))):  # op189
        raise PipelineError('material_limit', '原PDF超过本地核对大小限制。')
    digest = hashlib.sha256()
    with original.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            checkpoint(); digest.update(block)
    if digest.hexdigest() != expected_sha256:
        raise PipelineError('material_hash_mismatch', '原PDF与已记录的来源校验和不一致。')
    try:
        import pymupdf
    except ImportError:
        raise PipelineError('pdf_native_extractor_unavailable', '原PDF页核对需要服务器上的PyMuPDF。') from None
    try:
        with pymupdf.open(original) as document:
            checkpoint()
            if _pdf_encrypted(document):
                raise PipelineError('encrypted_material', '加密PDF需先解密。')
            if page_number > len(document):
                raise PipelineError('invalid_pdf_page', '核对页码超出原PDF实际页数。')
            text = document[page_number - 1].get_text('text') or ''
            checkpoint()
            if len(text) > max_chars:
                raise PipelineError('pdf_page_text_limit', '原PDF单页文字超过安全核对上限。')
    except PipelineError:
        raise
    except Exception:
        if cancellation_error is not None:
            raise cancellation_error
        raise PipelineError('invalid_pdf', '原PDF页无法可靠提取，未放行数字主张。') from None
    return {'text': text, 'page': page_number, 'page_text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'source_sha256': expected_sha256, 'extractor': 'pymupdf'}


def check_zip(z, max_uncompressed=512 * 1024 * 1024):
    infos = z.infolist()
    if len(infos) > 10000 or sum(i.file_size for i in infos) > max_uncompressed:
        raise PipelineError('archive_limit', 'Office文件解压规模超过限制。')
    seen = set()
    for item in infos:
        name = item.filename.replace('\\', '/')
        canonical = posixpath.normpath(name)
        if '\x00' in name or name.startswith('/') or '..' in PurePosixPath(name).parts or re.match(r'^[A-Za-z]:', name) or canonical in seen or stat.S_ISLNK(item.external_attr >> 16):
            raise PipelineError('unsafe_archive', 'Office文件包含不安全的内部路径。')
        seen.add(canonical)
        if item.flag_bits & 1 or item.file_size > 128 * 1024 * 1024 or (item.file_size > 1024 * 1024 and item.file_size / max(1, item.compress_size) > 1000):
            raise PipelineError('archive_limit', 'Office文件的压缩条目超过限制。')


def read_xml(z, part):
    """Never expand entities or load unbounded XML parts from Office containers."""
    if z.getinfo(part).file_size > 32 * 1024 * 1024:
        raise PipelineError('archive_limit', 'Office单个XML部件超过32MB解析上限。')
    data = z.read(part)
    if re.search(br'<!\s*(?:DOCTYPE|ENTITY)\b', data, re.I):
        raise PipelineError('unsafe_archive', 'Office XML含有禁止的DTD或实体声明。')
    return ET.fromstring(data)


def office_preview_safety(path, cancel_check=None):
    """Do not hand executable/externally loaded package parts to LibreOffice."""
    started = time.monotonic()
    with zipfile.ZipFile(path) as z:
        check_zip(z)
        for name in z.namelist():
            check_cancel(cancel_check)
            if time.monotonic() - started > 15:
                return 'office_safety_scan_timeout'
            lower = name.lower()
            if lower.endswith('vbaproject.bin') or '/embeddings/' in lower or lower.endswith('/oleobject.bin'):
                return 'office_active_embedded_content'
            if lower.endswith('.rels'):
                root = read_xml(z, name)
                if any(rel.get('TargetMode', '').lower() == 'external' and not rel.get('Type', '').endswith('/hyperlink') for rel in root):
                    return 'office_external_resource_relationship'
            elif lower.endswith('.xml'):
                read_xml(z, name)
    return None


def parse_local_material(path, config, filename=None, cancel_check=None, output_dir=None):
    from .wuyu_ocr import enabled as ocr_enabled, ocr_image, _remote_transport
    started = time.monotonic()
    max_seconds = min(600, max(1, float(config.get('WUYU_MAX_MATERIAL_SECONDS', 180))))
    # op 175: the official OCR route takes a few seconds per page, so its defaults
    # cover more scanned pages; explicit settings and the hard caps still apply.
    remote_ocr = bool(_remote_transport(config))
    ocr_deadline = started + min(max_seconds - .5, max(1, min(120, float(config.get('WUYU_OCR_TOTAL_TIMEOUT', 120 if remote_ocr else 45)))))
    ocr_max_pages = max(1, min(20, int(config.get('WUYU_OCR_MAX_PAGES', 20 if remote_ocr else 5))))
    ocr_pages, ocr_report = 0, []
    cancellation_error = None
    def checkpoint():
        nonlocal cancellation_error
        if cancellation_error is not None:
            raise cancellation_error
        try:
            check_cancel(cancel_check)
        except Exception as error:
            cancellation_error = error
            raise
        if time.monotonic() - started > max_seconds:
            raise PipelineError('material_timeout', '材料本地解析超过处理时间上限。')
    checkpoint()
    roots = config.get('WUYU_ALLOWED_MATERIAL_ROOTS') or [config.get('UPLOAD_FOLDER')]
    if isinstance(roots, str):
        roots = [roots]
    p = allowed_file(path, roots)
    max_bytes = int(config.get('WUYU_MAX_MATERIAL_BYTES', 200 * 1024 * 1024))  # op189
    if p.stat().st_size > max_bytes:
        raise PipelineError('material_limit', '材料超过本地解析大小限制。')
    hasher = hashlib.sha256()
    with p.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            checkpoint()
            hasher.update(block)
    digest = hasher.hexdigest()
    name = Path(filename or p.name).name
    chunks, warnings, structured = [], [], {}
    limit_chars = int(config.get('WUYU_MAX_MATERIAL_CHARS', 12_000_000))
    max_chunks = max(1, min(int(config.get('WUYU_MAX_MATERIAL_CHUNKS', 100000)), 100000))
    max_pages = max(1, min(int(config.get('WUYU_MAX_MATERIAL_PAGES', 2000)), 10000))
    max_rows = max(1, min(int(config.get('WUYU_MAX_MATERIAL_ROWS', 100000)), 1000000))
    limits_hit, document_counts = set(), {}
    used = 0
    truncated = False

    def at_limit():
        nonlocal truncated
        checkpoint()
        full = used >= limit_chars or len(chunks) >= max_chunks
        if used >= limit_chars: limits_hit.add('max_chars')
        if len(chunks) >= max_chunks: limits_hit.add('max_chunks')
        truncated |= full
        return full

    def capture_table(rows, sheet=None, partial=False, locator=None):
        from .wuyu_structured import table_record
        if len(structured) >= 256:
            warnings.append({'code': 'structured_tables_truncated', 'message': '结构化表目录达到256张；完整表格文字仍保留在原文中。'})
            return
        record = table_record(rows, p, name, digest, sheet=sheet, truncated=partial, locator=locator)
        if record:
            structured[record['data_id']] = record

    def add(text, extraction_method='native_text', **locator):
        nonlocal used, truncated
        checkpoint()
        text = str(text or '').strip()
        if not text:
            return
        if used >= limit_chars or len(chunks) >= max_chunks:
            truncated = True
            limits_hit.add('max_chars' if used >= limit_chars else 'max_chunks')
            return
        available = min(limit_chars - used, (max_chunks - len(chunks)) * 12000)
        if len(text) > available:
            truncated = True
            limits_hit.add('max_chars' if limit_chars - used <= (max_chunks - len(chunks)) * 12000 else 'max_chunks')
        text = text[:available]
        used += len(text)
        for offset in range(0, len(text), 12000):
            chunks.append({'id': f'{digest[:16]}:{len(chunks) + 1}', 'text': text[offset:offset + 12000],
                           'source': {'filename': name, 'sha256': digest, **locator},
                           'extraction_method': extraction_method,
                           'trust': 'untrusted_source_content', 'evidence_kind': 'project_material'})

    def add_ocr(image, page=None, pdf_scale=None):
        nonlocal ocr_pages
        remaining = ocr_deadline - time.monotonic()
        if ocr_pages >= ocr_max_pages or remaining < .2:
            ocr_report.append({'page': page, 'status': 'pending', 'reason': 'ocr_page_or_total_budget_limit'})
            return False
        ocr_pages += 1
        result = ocr_image(image, config, checkpoint, timeout=remaining)
        ocr_report.append({key: value for key, value in result.items() if key != 'lines'} | {'page': page})
        for line in result['lines']:
            if at_limit():
                break
            region = line['region']
            if pdf_scale:
                region = [coordinate / pdf_scale for coordinate in region]
            add(line['text'], extraction_method='machine_ocr', page=page,
                region={'bounds': region, 'unit': 'pdf_points' if pdf_scale else 'image_pixels'},
                ocr_confidence=line['confidence'], ocr_engine=result.get('engine', 'local_tesseract'))
        return bool(result['lines'])

    ext = p.suffix.lower()
    ocr = 'not_required'
    if ext in ('.txt', '.md', '.markdown', '.csv', '.tsv'):
        data = p.read_bytes()
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                text = data.decode('gb18030')
            except UnicodeDecodeError:
                raise PipelineError('invalid_encoding', '文本编码无法识别。') from None
        if ext in ('.csv', '.tsv'):
            rows = csv.reader(io.StringIO(text), delimiter='\t' if ext == '.tsv' else ',')
            table_rows, table_partial = [], False
            try:
                for i, row in enumerate(rows, 1):
                    if at_limit() or i > max_rows:
                        truncated = True
                        if i > max_rows: limits_hit.add('max_rows')
                        break
                    add(json.dumps(row, ensure_ascii=False), extraction_method='native_table_row', row=i, columns=len(row))
                    if len(table_rows) < 100:
                        table_rows.append(row[:30])
                        table_partial |= len(row) > 30
                    else:
                        table_partial = True
            except csv.Error:
                if cancellation_error is not None:
                    raise cancellation_error
                raise PipelineError('invalid_csv', 'CSV字段结构或单字段大小超过本地解析支持范围。') from None
            capture_table(table_rows, partial=table_partial or truncated)
        else:
            for i, line in enumerate(text.splitlines(), 1):
                if at_limit():
                    break
                add(line, line=i)
    elif ext in ('.pptx', '.docx', '.xlsx'):
        try:
            with zipfile.ZipFile(p) as z:
                check_zip(z, int(config.get('WUYU_MAX_ARCHIVE_BYTES', 512 * 1024 * 1024)))
                if ext == '.pptx':
                    pres = read_xml(z, 'ppt/presentation.xml')
                    rr = read_xml(z, 'ppt/_rels/presentation.xml.rels')
                    rels = {r.get('Id'): r for r in rr}
                    for page, s in enumerate(pres.findall('p:sldIdLst/p:sldId', NS), 1):
                        if at_limit() or page > max_pages:
                            truncated = True
                            if page > max_pages: limits_hit.add('max_pages')
                            break
                        r = rels[s.get('{' + NS['r'] + '}id')]
                        part = posixpath.normpath(posixpath.join('ppt', r.get('Target', ''))).lstrip('/')
                        if r.get('TargetMode', '').lower() == 'external' or not part.startswith('ppt/'):
                            raise PipelineError('unsafe_archive', '幻灯片关系必须指向包内PPT部件。')
                        x = read_xml(z, part)
                        text = '\n'.join(''.join(t.text or '' for t in para.findall('.//a:t', NS)) for para in x.findall('.//a:p', NS))
                        add(text, page=page, part=part)
                        for table_number,table in enumerate(x.findall('.//a:tbl',NS),1):
                            rows=[['\n'.join(''.join(t.text or ''for t in paragraph.findall('.//a:t',NS))
                                              for paragraph in cell.findall('.//a:p',NS))for cell in row.findall('a:tc',NS)]
                                  for row in table.findall('a:tr',NS)]
                            capture_table(rows,locator={'page':page,'table':table_number,'part':part})
                        relpart=posixpath.join(posixpath.dirname(part),'_rels',posixpath.basename(part)+'.rels')
                        if relpart in z.namelist():
                            for rel in read_xml(z,relpart):
                                if rel.get('Type','').endswith('/notesSlide')and rel.get('TargetMode','').lower()!='external':
                                    note_part=posixpath.normpath(posixpath.join(posixpath.dirname(part),rel.get('Target',''))).lstrip('/')
                                    if not note_part.startswith('ppt/notesSlides/'):raise PipelineError('unsafe_archive','备注必须来自包内PPT部件。')
                                    notes=read_xml(z,note_part)
                                    add('\n'.join(''.join(t.text or ''for t in paragraph.findall('.//a:t',NS))
                                                  for paragraph in notes.findall('.//a:p',NS)),page=page,part=note_part,notes=True)
                        if x.findall('.//a:blip', NS):
                            ocr = 'partial_pending'
                            warnings.append({'code': 'embedded_images_not_ocr', 'page': page, 'message': '仅原生文字进入证据，页内图片文字尚未OCR。'})
                        if not text.strip():
                            warnings.append({'code': 'image_text_not_extracted', 'page': page})
                elif ext == '.docx':
                    parts = ['word/document.xml'] + sorted(name for name in z.namelist() if re.fullmatch(
                        r'word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml', name))
                    paragraphs, tables = 0, 0
                    def visit(node, part):
                        nonlocal paragraphs, tables
                        if at_limit(): return
                        if node.tag == '{' + NS['w'] + '}p':
                            paragraphs += 1
                            add(word_text(node), paragraph=paragraphs, part=part)
                        elif node.tag == '{' + NS['w'] + '}tbl':
                            tables += 1
                            table_rows = []
                            for row, tr in enumerate(node.findall('w:tr', NS), 1):
                                if at_limit(): break
                                cells = ['\n'.join(word_text(paragraph) for paragraph in cell.findall('.//w:p', NS))
                                         for cell in tr.findall('w:tc', NS)]
                                add(json.dumps(cells, ensure_ascii=False), extraction_method='native_table_row', table=tables, row=row, part=part)
                                table_rows.append(cells)
                            capture_table(table_rows, locator={'table': tables, 'part': part}, partial=truncated)
                        else:
                            for child in node: visit(child, part)
                    for part in parts:
                        x = read_xml(z, part)
                        visit(x, part)
                    document_counts.update(parsed_paragraphs=paragraphs, parsed_tables=tables)
                    warnings.append({'code': 'docx_pagination_unavailable', 'message': '来源使用段落号，未声称为Word渲染页号。'})
                else:
                    for part in z.namelist():
                        checkpoint()
                        if part.lower().endswith('.xml'):
                            read_xml(z, part)
                    from openpyxl import load_workbook
                    book = load_workbook(p, read_only=True, data_only=True, keep_links=False)
                    try:
                        for sheet in book.worksheets:
                            if at_limit():
                                break
                            table_rows, table_partial = [], False
                            if sheet.max_row and sheet.max_row > max_rows:
                                truncated = True; limits_hit.add('max_rows')
                            for row, values in enumerate(sheet.iter_rows(max_row=min(sheet.max_row or 1, max_rows), values_only=True), 1):
                                if at_limit():
                                    break
                                reader_values = [value if value is None or isinstance(value, (str, int, float, bool))
                                                 else value.isoformat() if hasattr(value, 'isoformat') else str(value) for value in values]
                                add(json.dumps(reader_values, ensure_ascii=False, default=str), extraction_method='native_table_row', sheet=sheet.title, row=row, columns=len(values))
                                if len(table_rows) < 100:
                                    table_rows.append(list(values[:30]))
                                    table_partial |= any(v is not None for v in values[30:])
                                else:
                                    table_partial = True
                            # read_only iter_rows pads to max_col. Remove trailing
                            # empty columns so data IDs describe actual cells.
                            while table_rows and table_rows[0] and all(row[-1] is None or row[-1] == '' for row in table_rows):
                                for row in table_rows:
                                    row.pop()
                            capture_table(table_rows, sheet=sheet.title, partial=table_partial or truncated)
                        warnings.append({'code': 'xlsx_cached_values_only', 'message': '读取已有缓存值；不执行公式或宏，未计算的公式可能为空。'})
                    finally:
                        book.close()
                    formulas=load_workbook(p,read_only=True,data_only=False,keep_links=False)
                    try:
                        for sheet in formulas.worksheets:
                            for row in sheet.iter_rows(max_row=min(sheet.max_row or 1,max_rows)):
                                if at_limit():break
                                for cell in row:
                                    if cell.data_type=='f':
                                        add(str(cell.value),extraction_method='native_formula_not_executed',sheet=sheet.title,
                                            cell=cell.coordinate,row=cell.row,column=cell.column)
                    finally:formulas.close()
        except (zipfile.BadZipFile, ET.ParseError, KeyError):
            if cancellation_error is not None:
                raise cancellation_error
            raise PipelineError('invalid_office', 'Office文件内容损坏或结构不受支持。') from None
    elif ext == '.pdf':
        document, legacy_reader = None, None
        def legacy_pdf():
            nonlocal legacy_reader
            if legacy_reader is None:
                from PyPDF2 import PdfReader
                legacy_reader = PdfReader(p)
                if legacy_reader.is_encrypted:
                    raise PipelineError('encrypted_material', '加密PDF需先解密。')
            return legacy_reader
        try:
            try:
                import pymupdf
                document = pymupdf.open(p)
            except Exception:
                # PyPDF2 remains a visible compatibility fallback, never the
                # preferred extractor for mathematical comparison symbols.
                warnings.append({'code': 'pdf_text_extractor_fallback', 'extractor': 'pypdf2',
                                 'message': 'PyMuPDF原生提取不可用，已使用PyPDF2兼容提取；数学符号需核对原页。'})
            if document is not None and _pdf_encrypted(document):
                raise PipelineError('encrypted_material', '加密PDF需先解密。')
            total_pages = len(document) if document is not None else len(legacy_pdf().pages)
            document_counts['document_pages'] = total_pages
            truncated |= total_pages > max_pages
            if total_pages > max_pages: limits_hit.add('max_pages')
            for page in range(1, min(total_pages, max_pages) + 1):
                if at_limit():
                    break
                extractor = 'pymupdf' if document is not None else 'pypdf2'
                if document is not None:
                    try:
                        text = document[page - 1].get_text('text') or ''
                    except Exception:
                        checkpoint()
                        text = legacy_pdf().pages[page - 1].extract_text() or ''
                        extractor = 'pypdf2'
                        warnings.append({'code': 'pdf_text_extractor_fallback', 'extractor': extractor, 'page': page,
                                         'message': '该页已使用PyPDF2兼容提取；数学符号需核对原页。'})
                else:
                    text = legacy_pdf().pages[page - 1].extract_text() or ''
                add(text, page=page, text_extractor=extractor)
                if not text.strip():
                    ocr = 'pending'
                    recognized = False
                    if ocr_enabled(config) and ocr_pages < ocr_max_pages and time.monotonic() < ocr_deadline:
                        try:
                            import pymupdf
                            with pymupdf.open(p) as rendered_pdf:
                                original = rendered_pdf[page - 1]
                                scale = min(2, 2500 / max(original.rect.width, original.rect.height))
                                if scale <= 0:
                                    raise ValueError('invalid PDF page size')
                                pixmap = original.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                                with Image.open(io.BytesIO(pixmap.tobytes('png'))) as scanned:
                                    recognized = add_ocr(scanned, page=page, pdf_scale=scale)
                        except PipelineError:
                            raise
                        except Exception:
                            if cancellation_error is not None:
                                raise cancellation_error
                            ocr_report.append({'page': page, 'status': 'pending', 'reason': 'scanned_pdf_render_unavailable'})
                    else:
                        ocr_report.append({'page': page, 'status': 'pending',
                                           'reason': 'ocr_page_or_total_budget_limit' if ocr_enabled(config) else 'local_ocr_disabled_or_executable_unavailable'})
                    if not recognized:
                        warnings.append({'code': 'ocr_pending', 'page': page, 'message': '该扫描页未获得OCR文字；请检查本地引擎、语言包或处理预算。'})
        except PipelineError:
            raise
        except Exception:
            if cancellation_error is not None:
                raise cancellation_error
            raise PipelineError('invalid_pdf', 'PDF无法本地解析。') from None
        finally:
            if document is not None:
                document.close()
    elif ext in ('.png', '.jpg', '.jpeg', '.webp', '.tif', '.tiff'):
        try:
            with Image.open(p) as im:
                im.verify()
            with Image.open(p) as im:
                dimensions = {'width': im.width, 'height': im.height}
                if im.width * im.height > 40_000_000:
                    raise PipelineError('image_limit', '图片像素超过限制。')
        except PipelineError:
            raise
        except Exception:
            raise PipelineError('invalid_image', '图片文件无法解码。') from None
        with Image.open(p) as im:
            recognized = add_ocr(im, page=1)
        ocr = 'completed' if recognized else 'pending'
        if not recognized:
            warnings.append({'code': 'ocr_pending', 'message': '图片材料已校验，尚未取得本地OCR文字。', **dimensions})
    else:
        raise PipelineError('unsupported_material', '支持PPTX、DOCX、PDF、TXT、MD、CSV、TSV、XLSX及常见图片。')
    if truncated:
        warnings.append({'code': 'material_truncated', 'message': '材料超过解析页数、行数或文字上限，仅部分内容进入证据。'})
    checkpoint()
    if ext == '.pdf' and any(c.get('extraction_method') == 'machine_ocr' for c in chunks):
        ocr = 'partial_pending' if any(w.get('code') == 'ocr_pending' for w in warnings) else 'completed'
    from .wuyu_asset_extraction import extract_source_assets
    source_assets, asset_warnings = extract_source_assets(p, config, digest, name, checkpoint, output_dir)
    warnings.extend({'code': code, 'message': '部分原始图像未提取，请检查材料来源与提取上限。'} for code in asset_warnings)
    if ext in {'.docx', '.pptx', '.xlsx'}:
        with zipfile.ZipFile(p) as archive:
            embedded_count = sum(1 for info in archive.infolist() if not info.is_dir() and
                                 info.filename.startswith(('word/media/', 'ppt/media/', 'xl/media/')))
        if embedded_count:
            ocr = 'partial_pending'
            warnings.append({'code': 'embedded_images_not_ocr', 'message': '已保留可用Office内嵌原图；图内文字尚未OCR，不能据此声称已读取全部图片内容。'})
        represented={a.get('source',{}).get('part')for a in source_assets}
        represented.update(part for a in source_assets for part in a.get('source_parts',[]))
        represented.update(part for a in source_assets for location in a.get('locations',[])for part in location.get('related_parts',[]))
        with zipfile.ZipFile(p)as archive:
            unrepresented=[i.filename for i in archive.infolist()if not i.is_dir()and i.filename.startswith(('word/media/','ppt/media/','xl/media/'))and i.filename not in represented]
        if unrepresented:
            warnings.append({'code': 'office_media_partially_extracted', 'media_parts': embedded_count,
                             'extracted_assets': len(source_assets),'unrepresented_parts':len(unrepresented),
                             'message': '部分内嵌媒体格式不支持或超过提取上限，未作为可用原图。'})
    return {'path': str(p), 'filename': name, 'sha256': digest, 'chunks': chunks, 'warnings': warnings,
            'source_assets': source_assets,
            'source_structured_data': structured,
            'ocr_report': ocr_report,
            'processing_limits': {'max_seconds': max_seconds, 'max_chunks': max_chunks, 'max_chars': limit_chars,
                'max_pages': max_pages, 'max_rows': max_rows, 'used_chars': used, 'truncated': truncated,
                'reader_schema_version': 3, 'limits_hit': sorted(limits_hit), **document_counts},
            'ocr_status': ocr, 'instruction_handling': 'source instructions are data, never executed'}
