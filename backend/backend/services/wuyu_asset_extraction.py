"""Extract original media bytes, never rendered/redrawn scientific data."""
import hashlib
from io import BytesIO
from pathlib import Path
import posixpath
import re
import zipfile
from PIL import Image
from .wuyu_errors import PipelineError


def office_image_locations(archive, extension, checkpoint):
    """Bind embedded media to the surrounding original paragraph/slide/sheet."""
    from .wuyu_materials import read_xml, NS, word_text
    locations = {}
    def relationships(part):
        relpart = posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')
        if relpart not in archive.namelist():return {}
        return {r.get('Id'): posixpath.normpath(posixpath.join(posixpath.dirname(part), r.get('Target', ''))).lstrip('/')
                for r in read_xml(archive, relpart) if r.get('TargetMode', '').lower() != 'external'}
    if extension == '.docx':
        parts = ['word/document.xml'] + sorted(n for n in archive.namelist()
            if re.fullmatch(r'word/(?:header\d+|footer\d+|footnotes|endnotes|comments)\.xml', n))
        paragraph_number, table_number = 0, 0
        for part in parts:
            if part not in archive.namelist():continue
            checkpoint();root = read_xml(archive, part);rels = relationships(part)
            # Keep the text extractor's top-level paragraph IDs. Table contents
            # have table/row/cell locators instead of fabricated paragraph IDs.
            # Entries retain XML order, including repeated uses of one image.
            paragraphs, entries = [], []
            def entry(paragraph, source, group):
                item = {'node': paragraph, 'source': source, 'group': group,
                        'index': len(group)}
                group.append(item);entries.append(item)
            def visit(node):
                nonlocal paragraph_number, table_number
                checkpoint()
                if node.tag == '{' + NS['w'] + '}p':
                    paragraph_number += 1
                    entry(node, {'part': part, 'paragraph': paragraph_number}, paragraphs)
                elif node.tag == '{' + NS['w'] + '}tbl':
                    table_number += 1
                    for row_number, row in enumerate(node.findall('w:tr', NS), 1):
                        for cell_number, cell in enumerate(row.findall('w:tc', NS), 1):
                            cell_paragraphs = []
                            for index, paragraph in enumerate(cell.findall('.//w:p', NS), 1):
                                entry(paragraph, {'part': part, 'table': table_number,
                                      'row': row_number, 'cell': cell_number,
                                      'paragraph_in_cell': index}, cell_paragraphs)
                else:
                    for child in node:visit(child)
            visit(root)
            group_texts = {}
            for item in entries:
                key = id(item['group'])
                if key not in group_texts:
                    group_texts[key] = [word_text(value['node']).strip() for value in item['group']]
            for item in entries:
                checkpoint();paragraph = item['node'];index = item['index']
                texts = group_texts[id(item['group'])]
                following = [text for text in texts[index:index + 4] if text]
                preceding = [text for text in texts[max(0,index - 2):index] if text]
                caption = next((text for text in following if re.match(r'^(?:Fig(?:ure)?\.?\s*[S\d]|图\s*[S\d一二三四五六七八九])', text, re.I)), '')
                label = re.match(r'^(?:Fig(?:ure)?\.?\s*S?\d+|图\s*[S\d一二三四五六七八九]+)',caption,re.I)
                locator = item['source']
                media = []
                for node in paragraph.iter():
                    for key in ('{' + NS['r'] + '}embed','{' + NS['r'] + '}id'):
                        target = rels.get(node.get(key))
                        if target and '/media/' in target and '..' not in Path(target).parts:media.append(target)
                for target in dict.fromkeys(media):
                    locations.setdefault(target,[]).append({'source':locator,'caption':caption,'label':label.group()if label else '',
                        'nearby_text':'\n'.join(preceding+following)[:6000], 'related_parts':list(dict.fromkeys(media))})
    elif extension == '.pptx' and 'ppt/presentation.xml' in archive.namelist():
        rels = relationships('ppt/presentation.xml')
        root = read_xml(archive,'ppt/presentation.xml')
        for page, slide in enumerate(root.findall('p:sldIdLst/p:sldId',NS),1):
            checkpoint();part = rels.get(slide.get('{' + NS['r'] + '}id'))
            if not part:continue
            xml = read_xml(archive,part);text = '\n'.join(t.text or ''for t in xml.findall('.//a:t',NS))
            targets = relationships(part)
            for node in xml.iter():
                target = targets.get(node.get('{' + NS['r'] + '}embed'))
                if target and '/media/' in target:locations.setdefault(target,[]).append({'source':{'part':part,'page':page},'nearby_text':text[:6000]})
    elif extension == '.xlsx' and 'xl/workbook.xml' in archive.namelist():
        rels=relationships('xl/workbook.xml');root=read_xml(archive,'xl/workbook.xml')
        for sheet in root.iter():
            part=rels.get(sheet.get('{' + NS['r'] + '}id'))
            if not part or part not in archive.namelist():continue
            for drawing in relationships(part).values():
                if not drawing.startswith('xl/drawings/') or drawing not in archive.namelist():continue
                for target in relationships(drawing).values():
                    if '/media/' in target:locations.setdefault(target,[]).append({'source':{'part':part,'sheet':sheet.get('name')},'nearby_text':''})
    return locations


def extract_source_assets(path, config, source_sha, filename, checkpoint, output_dir=None):
    p = Path(path).resolve()
    roots = config.get('WUYU_ALLOWED_MATERIAL_ROOTS') or [config.get('UPLOAD_FOLDER', '')]
    if isinstance(roots, str):
        roots = [roots]
    roots = [Path(root).resolve() for root in roots if root]
    out = Path(output_dir or p.parent / 'extracted_assets' / source_sha[:16]).resolve()
    if not roots or not any(p.is_relative_to(root) and out.is_relative_to(root) for root in roots):
        raise PipelineError('unsafe_material_path', 'Source asset output must be inside controlled material storage')
    max_assets = max(1, min(int(config.get('WUYU_MAX_SOURCE_ASSETS', 200)), 600))
    max_bytes = max(1, min(int(config.get('WUYU_MAX_SOURCE_ASSET_BYTES', 64 * 1024 * 1024)), 200 * 1024 * 1024))
    assets, warnings, seen = [], [], set()
    consumed = 0

    def add(raw, page=None, region=None, caption='', label='', part=None, embedded=True, method='pdf_embedded_original', locations=None):
        nonlocal consumed
        checkpoint()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            existing=next(a for a in assets if a['sha256']==digest)
            if part and part not in existing.setdefault('source_parts',[]):existing['source_parts'].append(part)
            return
        try:
            with Image.open(BytesIO(raw)) as image:
                width, height = image.size
                fmt = image.format.lower()
                image.verify()
        except Exception:
            return
        if width * height > 40_000_000:
            if 'source_asset_image_pixel_limit' not in warnings:
                warnings.append('source_asset_image_pixel_limit')
            return
        if embedded and (max(width, height) < 500 or width * height < 120000):
            return
        if len(assets) >= max_assets or consumed + len(raw) > max_bytes:
            if 'source_asset_limit_reached' not in warnings:
                warnings.append('source_asset_limit_reached')
            return
        if fmt not in {'png', 'jpeg', 'webp', 'gif', 'tiff', 'bmp'}:
            return
        extension = 'jpg' if fmt == 'jpeg' else fmt
        out.mkdir(parents=True, exist_ok=True)
        target = out / f'{digest}.{extension}'
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            target.write_bytes(raw)
        source = {'filename': filename, 'sha256': source_sha}
        if page is not None:
            source['page'] = page
        if region:
            source['region'] = {'bounds': list(region), 'unit': 'pdf_points'}
        if part:
            source['part'] = part
        assets.append({'id': f'{source_sha[:16]}:p{page or 0}:{digest[:16]}', 'path': str(target),
            'sha256': digest, 'mime_type': 'image/jpeg' if fmt == 'jpeg' else 'image/' + fmt,
            'width': width, 'height': height, 'bytes': len(raw), 'kind': 'figure' if label else 'image',
            'figure_label': label, 'caption': caption[:3000], 'extraction_method': method,
            'original_bytes_preserved': True, 'source': source, 'evidence_kind': 'project_material',
            'trust': 'untrusted_source_content'})
        if locations:
            assets[-1]['locations'] = locations
            assets[-1]['source'].update({k:v for k,v in locations[0]['source'].items()if k!='part'})
            assets[-1]['nearby_text'] = locations[0].get('nearby_text','')
        consumed += len(raw)
        seen.add(digest)

    if p.suffix.lower() == '.pdf':
        try:
            import pymupdf
        except ImportError:
            return [], ['pdf_original_figures_unavailable_pymupdf']
        with pymupdf.open(str(p)) as doc:
            for number, page in enumerate(doc, 1):
                checkpoint()
                if number > int(config.get('WUYU_MAX_MATERIAL_PAGES',2000)):
                    warnings.append('source_asset_pdf_page_limit')
                    break
                captions = []
                original_count=len(assets)
                for block in page.get_text('blocks'):
                    text = str(block[4]).strip()
                    match = re.match(r'^(Fig(?:ure)?\.?\s*S?\d+|图\s*[S\d一二三四五六七八九]+)', text, re.I)
                    if match:
                        captions.append((block, text, match.group(1)))
                for image in page.get_images(full=True):
                    checkpoint()
                    if image[2] * image[3] > 40_000_000 or max(image[2], image[3]) < 500 or image[2] * image[3] < 120000:
                        continue
                    xref = image[0]
                    raw = doc.extract_image(xref)['image']
                    rects = page.get_image_rects(xref)
                    rect = rects[0] if rects else None
                    candidates = captions
                    if rect and candidates:
                        candidates = sorted(candidates, key=lambda row: abs(row[0][1] - rect.y1) +
                            (0 if row[0][2] >= rect.x0 and row[0][0] <= rect.x1 else 1000))
                    caption, label = (candidates[0][1], candidates[0][2]) if candidates else ('', '')
                    if number == 1 and 'graphicalabstract' in re.sub(r'\s+', '', page.get_text()).lower():
                        caption, label = '', 'Graphical abstract'
                    region = [rect.x0, rect.y0, rect.width, rect.height] if rect else None
                    add(raw, number, region, caption, label)
                if len(assets)==original_count and (captions or not page.get_text().strip() or len(page.get_drawings())>20):
                    # A vector-only flowchart/scanned page has no embedded
                    # raster to extract. Render the actual original PDF page;
                    # never redraw its scientific content or invent a figure.
                    scale=min(2,3072/max(page.rect.width,page.rect.height))
                    pix=page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False)
                    caption,label=(captions[0][1],captions[0][2])if captions else('','')
                    add(pix.tobytes('png'),number,[0,0,page.rect.width,page.rect.height],caption,label,
                        embedded=False,method='original_pdf_page_render')
                    if len(assets)>original_count:assets[-1]['original_bytes_preserved']=False
    elif p.suffix.lower() in {'.docx', '.pptx', '.xlsx'}:
        with zipfile.ZipFile(p) as archive:
            locations = office_image_locations(archive, p.suffix.lower(), checkpoint)
            for member in archive.infolist():
                checkpoint()
                if re.fullmatch(r'(?:word|ppt|xl)/media/[^/]+\.(?:png|jpe?g|gif|tiff?|bmp|webp)', member.filename, re.I):
                    if member.file_size <= max_bytes:
                        links = locations.get(member.filename,[])
                        primary = links[0] if links else {}
                        add(archive.read(member), part=member.filename, method='office_embedded_original',
                            embedded=not bool(links), locations=links, page=primary.get('source',{}).get('page'),
                            caption=primary.get('caption',''),label=primary.get('label',''))
    elif p.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.tif', '.tiff', '.bmp'}:
        add(p.read_bytes(), embedded=False, method='uploaded_original')
    return assets, warnings
