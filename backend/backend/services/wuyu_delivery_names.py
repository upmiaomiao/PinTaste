"""Report titles and download names shared by snapshots and HTTP delivery."""
import re
import unicodedata

LABELS = {'pptx': '图片版', 'image_pptx': '图片版', 'editable_pptx': '可编辑版',
          'hybrid_pptx': '混合可编辑版', 'pdf': '预览版'}
INTAKE_TITLE = re.compile(r'^(根据用户上传的资料规划|Plan a presentation from the user uploaded|先解析材料|请先上传)', re.I)


def _title(value):
    if not isinstance(value, str) or INTAKE_TITLE.search(value.strip()):
        return ''
    value = unicodedata.normalize('NFKC', value)
    value = re.sub(r'\.(?:pptx?|pdf|docx?|xlsx?|md)$', '', value, flags=re.I)
    value = re.split(r'[\n。；;]', value, maxsplit=1)[0]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f\u202a-\u202e\u2066-\u2069]', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    value = value.encode('utf-8')[:120].decode('utf-8', errors='ignore').rstrip(' .')
    if re.fullmatch(r'(?i)(?:[a-f\d-]{24,}|con|prn|aux|nul|com[1-9]|lpt[1-9])', value):
        return ''
    return value


def report_title(context=None, metadata=None):
    """Prefer the export's frozen title, then a real report title or source name."""
    candidates = [(metadata or {}).get('report_title')]
    if isinstance(context, dict):
        brief = context.get('brief') or {}
        candidates.extend([brief.get('title'), brief.get('topic'), context.get('title')])
        outline = context.get('outline') or []
        if isinstance(outline, dict):
            outline = outline.get('pages') or []
        if isinstance(outline, list) and outline and isinstance(outline[0], dict):
            candidates.append(outline[0].get('title'))
        candidates.extend(m.get('filename') for m in context.get('materials', [])
                          if isinstance(m, dict) and m.get('role', 'material') == 'material')
    else:
        candidates.append(context)
    return next((clean for value in candidates if (clean := _title(value))), '无隅演示报告')


def delivery_name(item, topic=None):
    metadata = item.get('metadata') or {}
    # Preview/QA records carry the parent export's format as provenance. That
    # metadata must never turn a PNG or JSON companion into a .pptx download.
    if item.get('kind') and item['kind'] not in LABELS:
        return item.get('filename') or '文件'
    format = metadata.get('format') or item.get('format') or item.get('kind')
    if format not in LABELS:
        return item.get('filename') or '文件'
    suffix = '.pdf' if format == 'pdf' else '.pptx'
    return report_title(topic, metadata) + '_' + LABELS[format] + suffix


def download_disposition(filename, attachment=True):
    """Offer UTF-8 and an ASCII fallback for browsers and mobile clients."""
    from urllib.parse import quote
    extension = re.search(r'\.[a-zA-Z0-9]{1,8}$', filename)
    suffix = extension[0].lower() if extension else '.bin'
    mode = 'attachment' if attachment else 'inline'
    return f'{mode}; filename="wuyu-report{suffix}"; filename*=UTF-8\'\'{quote(filename, safe="")}'
