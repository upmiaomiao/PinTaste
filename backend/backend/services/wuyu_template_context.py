"""Bounded selection of real template pages; template text never becomes evidence."""
import json


def normalize_template_reference(item, catalog):
    """Resolve an explicit public page number once, without falling back to a cover."""
    if item is None or isinstance(item, str):
        return item
    if not isinstance(item, dict) or set(item) - {'source_id', 'page_id', 'page_number'}:
        raise ValueError('模板参考须为来源 ID 或明确的来源/页面记录。')
    source_id, page_id = item.get('source_id'), item.get('page_id')
    if any(value is not None and (not isinstance(value, str) or not value) for value in (source_id, page_id)):
        raise ValueError('模板来源与页面 ID 须为非空文本。')
    if not source_id and not page_id:
        raise ValueError('模板参考缺少来源或页面 ID。')
    sources = {source['id']: source for source in (catalog or {}).get('sources', [])}
    owners = {page['id']: source['id'] for source in sources.values() for page in source.get('pages', [])}
    if page_id in owners:
        owner = owners[page_id]
        if source_id and source_id != owner:
            raise ValueError('模板页面不属于指定来源。')
        source_id = owner
    elif page_id:
        # Server-bound uploaded references retain their existing private scope
        # checks. They do not use public catalog page-number lookup.
        if not page_id.startswith('material:') or 'page_number' in item:
            raise ValueError('指定模板页面不存在。')
        if source_id and not page_id.startswith(source_id + ':p'):
            raise ValueError('模板页面不属于指定来源。')
        return dict(item)
    if 'page_number' in item:
        number = item['page_number']
        if type(number) is not int or number < 1 or source_id not in sources:
            raise ValueError('模板页号须为指定来源中存在的正整数。')
        matches = [page for position, page in enumerate(sources[source_id].get('pages', []), 1)
                   if page.get('page_number', page.get('number', position)) == number]
        if len(matches) != 1 or page_id and page_id != matches[0]['id']:
            raise ValueError('模板页号不存在、存在歧义或与页面 ID 冲突。')
        page_id = matches[0]['id']
    if page_id:
        return {'source_id': source_id, 'page_id': page_id}
    if source_id not in sources and not source_id.startswith('material:'):
        raise ValueError('指定模板来源不存在。')
    return {'source_id': source_id}


FAMILY_WORDS = {
    'table': ('table', 'matrix', '表格', '计划表', '分工', '责任', '清单', '矩阵'),
    'flow': ('flow', 'flowchart', 'flow_chart', 'step', 'roadmap', '流程', '步骤', '路线', '链条', '路径', '闭环'),
    'comparison': ('comparison', 'compare', '对比', '比较', '优势', '两条', '双向'),
    'map': ('map', 'geograph', '地图', '地理', '空间分布', '区域分布'),
    'chart': ('chart', 'trend', 'plot', 'heatmap', 'scatter', 'radar', 'violin', 'boxplot', '图表', '数据图', '趋势', '结果', '分布', '性能'),
    'hierarchy': ('tree', 'hierarchy', 'architecture', 'mind_map', '层级', '架构', '体系', '树状'),
    'closing': ('closing', 'summary', 'conclusion', '结束', '总结', '展望', '结论'),
}


def layout_family(value):
    text = json.dumps(value, ensure_ascii=False).lower() if not isinstance(value, str) else value.lower()
    scores = {key: sum(len(word) for word in words if word in text) for key, words in FAMILY_WORDS.items()}
    return max(scores, key=scores.get) if any(scores.values()) else 'overview'


def template_page_metadata(chunks):
    pages = {}
    for chunk in chunks:
        number = chunk.get('source', {}).get('page')
        if type(number) is int and number > 0:
            pages.setdefault(number, []).append(str(chunk.get('text', '')))
    result = {}
    for number, texts in pages.items():
        text = '\n'.join(texts)
        title = next((line.strip()[:160] for line in text.splitlines() if line.strip()), '')
        result[number] = {'title': title, 'layout_family': layout_family(text[:2500])}
    return result


def representative_page_numbers(count, metadata=None, limit=12):
    """Keep cover/end, cover distinct expressions, then fill across the deck span."""
    if type(count) is not int or count <= 0:
        return []
    limit = max(2, min(24, int(limit)))
    if count <= limit:
        return list(range(1, count + 1))
    selected = {1, count}
    # Reserve half the budget for broad positional coverage, including late pages.
    for i in range(1, max(2, limit // 2) - 1):
        selected.add(1 + round(i * (count - 1) / (max(2, limit // 2) - 1)))
    by_family = {}
    for number, item in (metadata or {}).items():
        if 1 < number < count:
            by_family.setdefault(item.get('layout_family', 'overview'), []).append(number)
    for family in ('flow', 'table', 'comparison', 'chart', 'map', 'hierarchy', 'closing', 'overview'):
        options = [n for n in by_family.get(family, []) if n not in selected]
        if options and len(selected) < limit:
            selected.add(max(options, key=lambda n: min(abs(n - old) for old in selected)))
    while len(selected) < limit:
        selected.add(max((n for n in range(2, count) if n not in selected),
                         key=lambda n: (min(abs(n - old) for old in selected), -n)))
    return sorted(selected)


def page_ref_id(item):
    return item.get('page_id') or item.get('source_id') if isinstance(item, dict) else item


def _reference_source(ident, sources, owners):
    if ident in sources:
        return ident
    if ident in owners:
        return owners[ident]
    if isinstance(ident, str) and ident.startswith('material:'):
        return ident.split(':p', 1)[0]
    return None


def _page_family(page):
    family = page.get('layout_family')
    if isinstance(family, str) and (family in FAMILY_WORDS or family in {'cover', 'overview'}):
        return family
    return layout_family({'title': page.get('title', ''), 'tags': page.get('tags', {})})


def _page_kind(page):
    tags = page.get('tags') if isinstance(page.get('tags'), dict) else {}
    value = page.get('page_role') or tags.get('page_role') or ''
    role = json.dumps(value, ensure_ascii=False).lower()
    family, title = _page_family(page), str(page.get('title', '')).lower()
    if family == 'cover' or any(word in role for word in ('cover', '封面', '扉页')):
        return 'cover'
    if family == 'closing' or any(word in role for word in ('closing', 'thanks', '结束', '致谢', '封底')):
        return 'closing'
    if any(word in role for word in ('body', 'content', '正文')):
        return 'body'
    if '封面' in title or title == 'cover' or page.get('is_local') and page.get('page_number') == 1:
        return 'cover'
    return 'body'


def choose_page_references(recipe, catalog, root, spec):
    """Choose page compositions inside the user's selected template sources.

    A confirmed sample is a brand/typography anchor, not the composition for
    every body page. Equal-family body references rotate by output page number,
    so repeated content structures need not reuse the same first two pictures.
    Selection stays deterministic and never borrows an unrelated library deck.
    """
    requested = [recipe.get('style_anchor')] + list(recipe.get('references') or [])
    explicit = [page_ref_id(item) for item in requested if item]
    source_map = {source['id']: source for source in (catalog or {}).get('sources', [])}
    owners = {page['id']: source['id'] for source in source_map.values() for page in source.get('pages', [])}
    sources = {source for ident in explicit if (source := _reference_source(ident, source_map, owners))}
    primary = _reference_source(page_ref_id(recipe.get('style_anchor')), source_map, owners)
    represented_covers = {source['id'] for source in source_map.values()
                          for page in source.get('pages', []) if page.get('id') in explicit and _page_kind({**page, 'is_local': source.get('source_type') == 'local'}) == 'cover'}
    duplicate_covers = [item for item in explicit if item in represented_covers]
    # An explicit structural form outranks incidental words in an academic title.
    structure = spec.get('layout', {}).get('structure') if isinstance(spec.get('layout'), dict) else None
    structural_form = structure or spec.get('visual_form') or ''
    structural_family = 'flow' if isinstance(structural_form, str) and structural_form in {'process', 'pipeline', 'process_diagram'} else layout_family(structural_form)
    family = structural_family if structural_family != 'overview' else layout_family({'title': spec.get('title', ''), 'layout': spec.get('layout', {})})
    is_cover = (spec.get('kind') == 'cover' or spec.get('role') == 'cover' or
                not spec.get('kind') and not spec.get('role') and spec.get('number') == 1)
    approved = sorted(recipe.get('approved_sample_refs') or [], key=lambda r: ('cover' not in r['id']) if is_cover else ('first_body' not in r['id']))
    candidates = []
    for source in source_map.values():
        if source['id'] not in sources:
            continue
        for page in source.get('pages', []):
            if page.get('preview_review') == 'layout_issue':
                continue
            image = page.get('image')
            if not image or not root:
                continue
            path = (root / image).resolve()
            if root not in path.parents or not path.is_file():
                continue
            candidates.append({**page, 'source_id': source['id'], 'is_local': source.get('source_type') == 'local'})
    for record in recipe.get('user_template_refs') or []:
        parent = 'material:' + str(record.get('material_id', ''))
        if parent in sources:
            candidates.append({**record, 'page_number': record.get('number', 1), 'source_id': parent, 'is_local': True})
    body = [page for page in candidates if _page_kind(page) != 'cover' and (_page_kind(page) != 'closing' or family == 'closing')]
    matching = [page for page in body if _page_family(page) == family]
    # Keep the selected style source authoritative when it offers the required
    # expression. Other sources are eligible only when explicitly in the recipe.
    primary_matching = [page for page in matching if page['source_id'] == primary]
    pool = primary_matching or matching
    if not pool:
        fallback = [page for page in body if _page_family(page) == 'overview'] or body
        pool = [page for page in fallback if page['source_id'] == primary] or fallback
    pool = sorted(pool, key=lambda page: (page.get('preview_review') != 'recommended_clear_body',
                                          page.get('page_number') if type(page.get('page_number')) is int else 0, str(page.get('id'))))
    number = spec.get('number')
    offset = ((number if type(number) is int and number > 0 else 2) - 2) % len(pool) if len(pool)>2 else 0
    rotated = pool[offset:] + pool[:offset]
    page_specific = [] if is_cover else rotated[:2]
    # Composition references lead; approved examples and explicit anchors retain
    # brand colors, typography and frames without imposing their old page story.
    chosen = [page['id'] for page in page_specific]
    chosen += [approved[0]['id']] if approved else []
    chosen += [item for item in requested if item and page_ref_id(item) not in duplicate_covers]
    metadata = {page['id']: {'source_id': page['source_id'], 'page_number': page.get('page_number'),
                'layout_family': _page_family(page), 'reference_purpose': 'page_composition',
                'selection_reason': 'matching_page_expression' if _page_family(page) == family else 'selected_source_body'} for page in page_specific}
    if approved:
        metadata[approved[0]['id']] = {'reference_purpose': 'global_brand_typography_and_frame',
                                      'selection_reason': 'approved_style_anchor_only'}
    return chosen, metadata, {'layout_family': family, 'candidate_count': len(candidates),
                              'body_reference_pool_count': len(body), 'matching_expression_count': len(pool),
                              'composition_rotation': offset,
                              'page_specific_reference_ids': [page['id'] for page in page_specific],
                              'global_style_reference_ids': [approved[0]['id']] if approved else [],
                              'deduplicated_reference_ids': duplicate_covers}
