"""Public template names and exactly five varied body references, without research copy."""
import hashlib
import json
import re
from pathlib import Path

BODY_TITLES = {'flow': '流程与步骤', 'comparison': '对比分析', 'chart': '图表组合',
               'table': '表格表达', 'hierarchy': '层级结构', 'map': '空间分布',
               'overview': '图文排版', 'cover': '章节排版', 'closing': '总结排版'}


def public_template_title(source, tags):
    """Use task, palette and a stable short suffix; never a filename, author or institution."""
    purposes = tags.get('purpose', [])
    purpose = next((p for p in ('博士论文答辩', '硕士论文答辩', '开题答辩', '资格考核',
                    '成果报奖', '项目申请', '企业介绍', '商业计划', '技术方案') if p in purposes), '学术汇报')
    palette = next((p for p in tags.get('visual_style', []) if p in {'蓝白', '青绿', '紫白', '暖色'}), '简约')
    suffix = hashlib.sha256(str(source['id']).encode()).hexdigest()[:4].upper()
    return f'{purpose} · {palette} {suffix}'


def five_body_pages(pages):
    """Keep at most five actual body pages; prefer different layouts and positions."""
    # Legacy catalogs sometimes classified agendas and section dividers as body pages.
    def is_body(page):
        title = str(page.get('title', '')).strip()
        return (page.get('is_body_reference') and
                not re.fullmatch(r'[\d\s.、一二三四五六七八九十]+', title) and
                title.casefold() not in {'汇报内容', '报告内容', '汇报提纲', '目录',
                                         'contents', 'agenda', 'thank you', '谢谢'})
    candidates = [p for p in pages if is_body(p)]
    if not candidates:
        return []
    chosen = []
    for family in dict.fromkeys(p.get('layout_family') for p in candidates):
        pool = [p for p in candidates if p.get('layout_family') == family]
        chosen.append(pool[len(pool) // 2])
        if len(chosen) == 5:
            break
    for index in range(len(candidates)):
        if len(chosen) >= min(5, len(candidates)):
            break
        number = round(index * (len(candidates) - 1) / max(1, min(5, len(candidates)) - 1))
        page = candidates[min(number, len(candidates) - 1)]
        if page not in chosen:
            chosen.append(page)
        if len(chosen) >= min(5, len(candidates)):
            break
    return sorted(chosen, key=lambda p: p['page_number'])


def local_design_manifest(library_root):
    """Read a bounded, administrator-built manifest; absence never permits raw local fallback."""
    root = Path(library_root).resolve()
    path = root / 'local/design-previews-v1.json'
    if not path.is_file():
        return {}, b''
    if path.is_symlink() or path.stat().st_size > 2_000_000:
        raise ValueError('Invalid local design preview manifest')
    raw = path.read_bytes()
    value = json.loads(raw)
    if value.get('version') != 1 or not isinstance(value.get('sources'), dict):
        raise ValueError('Invalid local design preview manifest')
    return value['sources'], raw
