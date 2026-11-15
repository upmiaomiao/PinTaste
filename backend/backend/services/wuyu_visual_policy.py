"""Evidence-first visual expression rules shared by planning, imaging and tracing."""
from copy import deepcopy


CHART_RULES = (
    ('time_series', ['line', 'small_multiple_line', 'area'],
     'Ordered dates/time and comparable units; area only for meaningful accumulated magnitude. Preserve missing intervals; never smooth invented observations.'),
    ('category_comparison', ['dot_plot', 'lollipop', 'grouped_column'],
     'Actual category values on a shared scale. Prefer dot/lollipop for many categories; columns remain appropriate when zero baseline is meaningful.'),
    ('before_after', ['dumbbell', 'slope_chart'],
     'Paired values for the same entities and two comparable conditions; do not turn unpaired samples into paired effects.'),
    ('part_to_whole', ['donut', 'stacked_column', 'hundred_percent_stacked'],
     'Verified mutually exclusive parts and an explicit meaningful total; donut only for few nonnegative parts. Do not normalize unrelated metrics into shares.'),
    ('association', ['scatter', 'bubble'],
     'Verified paired x/y observations; bubble also requires a sourced third size variable. No synthetic points or implied causal relationship.'),
    ('distribution', ['histogram', 'box_plot', 'violin'],
     'Raw observations or supplied distribution statistics. Never fabricate a distribution, whiskers, density, uncertainty or sample size from averages/ranges.'),
    ('matrix', ['heatmap'],
     'A genuine row-by-column numeric matrix with units and an honest shared color scale; no invented empty cells.'),
    ('incremental_contribution', ['waterfall'],
     'Explicit signed increments and verified start/end totals; independent category magnitudes are not cumulative contributions.'),
    ('quantified_flow', ['sankey'],
     'Verified directed links with comparable quantities and a meaningful balance; qualitative processes use uniform-width arrows instead.'),
    ('geographic', ['choropleth', 'proportional_symbol_map'],
     'Actual geographic entities/coordinates and corresponding verified values; do not invent locations or boundaries.'),
    ('hierarchical_parts', ['treemap'],
     'Verified parent-child membership and comparable nonnegative part values; otherwise use a qualitative tree.'),
)


def visual_expression_policy():
    return {
        'version': 1,
        'priority': 'Choose an honest visual encoding for the evidence before choosing decoration; no numeric facts may be invented for variety.',
        'content': 'Prefer small consistent pictograms, icons and explanatory schematics to sentence walls. One claim per page; short labels live in the diagram, supporting prose and detail remain in speaker notes. Preserve all confirmed values, units and necessary qualifications.',
        'logos': 'A generic pictogram is not a brand logo. Use only independently supplied project logos; do not copy school emblems, awards, names, dates, page numbers or captions from reference templates.',
        'processes': 'Simple methods use a compact step diagram. Complex processes use numbered stages with clear directed connectors, swimlanes for distinct actors, a cycle only for a real feedback loop, and branches only for evidenced decisions. Keep an uncluttered final state readable as a static image.',
        'chart_selection': [{'data_semantics': semantics, 'forms': list(forms), 'requires': rule}
                            for semantics, forms, rule in CHART_RULES],
        'diversity': 'Do not default every numerical page to bars/columns or vary the chart merely for novelty. Different evidence types use appropriate different encodings; comparable data keep the same encoding and color meaning across pages. Several unsupported conclusions use a comparison table, evidence matrix or annotated schematic without fictional numerical axes.',
        'color': {
            'canvas': '#FFFFFF', 'text': '#172C35',
            'categorical': ['#176B5B', '#3D83C5', '#D7A43B', '#8B75B5', '#D57565', '#63A5A0'],
            'sequential': ['#EAF5F1', '#B9DDD1', '#78B8A5', '#2B8C74', '#155D4F'],
            'diverging': ['#3268A8', '#9FBED9', '#F6F7F7', '#E9B985', '#B76A32'],
            'rule': 'Explicit user/template brand colors override this default palette. Keep category colors stable across the deck; sequential scales encode magnitude, diverging scales require a meaningful midpoint. Use muted neutral context and one restrained highlight; avoid rainbow gradients, fake 3D, low contrast and decorative shadows over data.'},
        'typography': {'chinese': '微软雅黑', 'latin_and_digits': 'Arial',
                       'consistency': 'The same semantic text role and layout variant use the same font size across the deck. Do not shrink individual peer labels arbitrarily; simplify wording or allocate space while preserving facts.'},
        'media': 'Sharp original or vector logos and crisp schematic edges; no blurry screenshot thumbnails. Preserve supplied scientific figure data and labels without invented enhancement.',
        'animation': {'preview': 'PNG is the complete static final state, not a video or GIF',
                      'editable_pptx': 'Complex flow stages use ordered click-to-appear groups when reconstructed as separate native objects. Do not promise animation for a flattened image-only page.'},
    }


def page_visual_contract(spec):
    policy = visual_expression_policy()
    policy['page_context'] = {
        'kind': spec.get('creation_kind', 'deck'),
        'layout': deepcopy(spec.get('layout', {})),
        'has_verified_structured_data': bool(spec.get('source_structured_data')),
        'has_source_figure_references': bool(spec.get('source_asset_ids')),
    }
    if spec.get('creation_kind') == 'asset' and spec.get('asset_kind') == 'icon':
        policy['content'] = 'One reusable unlabelled pictogram only; no letters, digits, captions, chart axes or presentation chrome.'
    return policy


def flow_animation_policy(spec):
    layout = spec.get('layout') or {}
    layout = layout if isinstance(layout, dict) else {}
    structure = str(layout.get('structure', '')).lower()
    visual = spec.get('visualization') or {}
    visual = visual if isinstance(visual, dict) else {}
    form = str(visual.get('form', '')).lower()
    is_flow = any(marker in structure + ' ' + form for marker in ('flow', 'process', 'swimlane', '流程', '技术路线', 'cycle'))
    points = spec.get('points') or []
    complex_flow = is_flow and (visual.get('complexity') == 'complex' or
                               isinstance(points, list) and len(points) >= 4 or 'swimlane' in structure)
    if spec.get('creation_kind') == 'asset' and spec.get('asset_kind') == 'icon':
        complex_flow = False
    return {
        'requested': bool(complex_flow), 'effect': 'on_click_appear', 'maximum_steps': 12,
        'element_field': 'animation_step',
        'rule': 'For this complex process, assign animation_step integer 1..12 to separately rebuilt step nodes, their pictograms and exact labels. The incoming connector belongs to its destination stage. Same step appears together, the next step on the next click. Keep title, source notes, decoration and unrelated graphics unmarked and always visible. Preserve the actual reading/causal order. Never animate a full-page raster or invent intermediate states.'
                if complex_flow else 'Leave animation_step absent; ordinary charts, icons and non-process pages stay static.',
        'static_preview': 'all stages visible; no playback in PNG',
    }
