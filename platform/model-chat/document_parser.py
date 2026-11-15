"""Structured, complete extraction for document retrieval; bounded and explicit."""
import io
import json
import re
from pathlib import Path

MAX_CHARACTERS = 2_000_000
MAX_PAGES = 2000
VERSION = 'structured-rag-v1'


def extract(name, data):
    ext = Path(name).suffix.lower()
    blocks, warnings = [], []
    character_count = 0
    total_pages = 0

    def add(text, *, page=None, section='', visual=False):
        nonlocal character_count
        text = text.replace('\x00', '').strip()
        blocks.append({'text': text, 'page': page, 'section': section[:160], 'visual': visual})
        character_count += len(text)
        if character_count > MAX_CHARACTERS:
            raise ValueError('文档超过 200 万字符的解析容量，请拆成多个附件；本次没有截取前文代替全文。')

    if ext == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError('请先解除 PDF 密码保护。')
        total_pages = len(reader.pages)
        if total_pages > MAX_PAGES:
            raise ValueError('PDF 超过 2,000 页，请拆分后上传。')
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ''
            resources = page.get('/Resources')
            if resources is not None:
                resources = resources.get_object()
            visual = bool((resources or {}).get('/XObject')) or not text.strip()
            add(text, page=number, visual=visual)
        if any(not b['text'] for b in blocks):
            warnings.append('部分页面没有文本层，将参与视觉检索；选中页面会尝试 OCR，无法识别的内容会明确提示。')
    elif ext == '.docx':
        from docx import Document
        document = Document(io.BytesIO(data))
        heading = ''
        # XML order keeps paragraphs and tables together in their source context.
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        for element in document.element.body.iterchildren():
            if element.tag.endswith('}p'):
                paragraph = Paragraph(element, document)
                if paragraph.style and paragraph.style.name.startswith(('Heading', '标题')):
                    heading = paragraph.text
                if paragraph.text.strip():
                    add(paragraph.text, section=heading)
            elif element.tag.endswith('}tbl'):
                table = Table(element, document)
                rows = [' | '.join(cell.text.replace('\n', ' ') for cell in row.cells) for row in table.rows]
                add('\n'.join(rows), section=heading)
    elif ext == '.pptx':
        from pptx import Presentation
        slides = Presentation(io.BytesIO(data)).slides
        if len(slides) > MAX_PAGES:
            raise ValueError('PPT 超过 2,000 页，请拆分后上传。')
        total_pages = len(slides)
        for number, slide in enumerate(slides, 1):
            title = slide.shapes.title.text if slide.shapes.title else ''
            rows = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    rows.append(shape.text)
                elif shape.has_table:
                    rows.extend(' | '.join(cell.text for cell in row.cells) for row in shape.table.rows)
            add('\n'.join(rows), page=number, section=title)
        warnings.append('PPT 的图形和图片暂未转为可检索页面；已索引文本与表格。')
    elif ext in {'.xlsx', '.xls'}:
        if ext == '.xlsx':
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
            sheets = [(sheet.title, sheet.iter_rows(values_only=True)) for sheet in workbook]
        else:
            import xlrd
            workbook = xlrd.open_workbook(file_contents=data, on_demand=True)
            sheets = [(sheet.name, (sheet.row_values(i) for i in range(sheet.nrows))) for sheet in workbook.sheets()]
        try:
            for sheet_name, rows in sheets:
                header = ''
                for number, row in enumerate(rows, 1):
                    if number > 100000 or len(row) > 500:
                        raise ValueError('表格超过单表 100,000 行或 500 列的处理容量，请拆分。')
                    text = ' | '.join('' if value is None else str(value) for value in row)
                    if not text.replace('|', '').strip():
                        continue
                    if not header:
                        header = text
                    add(('表头：' + header + '\n' if number > 1 else '') + f'第 {number} 行：' + text, section=sheet_name)
        finally:
            workbook.close() if ext == '.xlsx' else workbook.release_resources()
        warnings.append('表格使用缓存值，公式未重新计算。')
    else:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = data.decode('gb18030')
        if '\x00' in text:
            raise ValueError('文件不是可读取的文本。')
        if len(text) > MAX_CHARACTERS:
            raise ValueError('文档超过 200 万字符，请拆分后上传。')
        heading = ''
        for paragraph in re.split(r'\n\s*\n', text):
            first = paragraph.split('\n', 1)[0]
            if re.match(r'^#{1,6}\s|^第[一二三四五六七八九十\d]+[章节]', first):
                heading = first.lstrip('# ').strip()
            if paragraph.strip():
                add(paragraph, section=heading)
    text = '\n\n'.join((f'第 {b["page"]} 页\n' if b['page'] else '') + b['text'] for b in blocks)
    if not blocks:
        raise ValueError('文档没有可读内容。')
    return {'kind': 'document', 'text': text, 'blocks': blocks, 'warning': ' '.join(warnings),
        'parser_version': VERSION, 'parsed_pages': total_pages, 'total_pages': total_pages,
        'visual_pages': sum(b['visual'] for b in blocks), 'source_complete': True}
