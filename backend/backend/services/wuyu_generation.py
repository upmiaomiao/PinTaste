"""Validated creation scope shared by planner and persisted workflow boundaries."""


def creation_options(brief):
    kind = brief.get('creation_kind', 'deck')
    output = brief.get('output_preference', 'editable')
    profile = brief.get('image_profile', 'default')
    asset = brief.get('asset_kind', 'diagram')
    if not isinstance(kind, str) or kind not in ('deck', 'pages', 'asset'):
        raise ValueError('creation_kind must be deck, pages or asset')
    if not isinstance(output, str) or output not in ('image', 'editable'):
        raise ValueError('output_preference must be image or editable')
    if not isinstance(profile, str) or profile not in ('default', 'gpt', 'gemini'):
        raise ValueError('image_profile must be default, gpt or gemini')
    if not isinstance(asset, str) or asset not in ('diagram', 'illustration', 'icon'):
        raise ValueError('asset_kind must be diagram, illustration or icon')
    preset = brief.get('diagram_preset')
    if preset is not None:
        from .wuyu_diagram_presets import known
        if not isinstance(preset, str) or not known(preset):
            raise ValueError('diagram_preset must be one of flow, architecture, concept, roadmap, comparison')
        if kind != 'asset' or asset != 'diagram':
            raise ValueError('diagram_preset only applies to a professional diagram (creation_kind asset, asset_kind diagram)')
    count = brief.get('page_count')
    low, high = (2, 80) if kind == 'deck' else (1, 8) if kind == 'pages' else (1, 1)
    if type(count) is not int or not low <= count <= high:
        raise ValueError(f'page_count must be from {low} to {high} for {kind}')
    if brief.get('aspect_ratio', '16:9') != '16:9':
        raise ValueError('Only 16:9 is currently supported')
    from .wuyu_diagram_presets import resolve
    chosen = resolve(brief) if kind == 'asset' and asset == 'diagram' else None
    return {'creation_kind': kind, 'output_preference': output,
            'image_profile': profile, 'asset_kind': asset, **({'diagram_preset': chosen} if chosen else {})}


def is_textless_icon(spec):
    """Only the server-bound asset/icon scope makes visible text inapplicable."""
    return isinstance(spec, dict) and spec.get('creation_kind') == 'asset' and spec.get('asset_kind') == 'icon'
