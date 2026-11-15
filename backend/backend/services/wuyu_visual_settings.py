"""One effective visual source shared by workflow UI and image requests."""
from copy import deepcopy
import json

FOLLOW_TEMPLATE_STYLE = '跟随已确认模板'


def follows_template(body):
    return ((body.get('template_choice') or {}).get('style_mode') == 'template'
            and body.get('brief', {}).get('style') == FOLLOW_TEMPLATE_STYLE)


def effective_visual_settings(body, confirmed):
    brief, choice = body.get('brief', {}), body.get('template_choice') or {}
    template = bool(confirmed and follows_template(body))
    return {'style_mode': 'template' if template else 'brief',
            'effective_style': FOLLOW_TEMPLATE_STYLE if template else brief.get('style', ''),
            'additional_style_requirements': brief.get('design_notes', '')}


def apply_template_visual_settings(prompt, settings):
    """Keep source/copy policies; replace defaults that conflict with consented style.

    Only fresh template confirmations opt in. Existing completed projects and
    legacy brief styles retain their request semantics until the user confirms.
    """
    if settings.get('style_mode') != 'template':
        return prompt
    value = json.loads(prompt)
    value['effective_visual_settings'] = deepcopy(settings)
    value['design_preferences'] = {}  # Old RAG defaults cannot restore the replaced style.
    task = value.get('task', '')
    task = task.replace(
        'Current feedback and explicit design_requirements override conflicting palettes, typography and decoration in every style reference, including the edit original and approved samples. ',
        'Only current explicit feedback and additional design_notes may override the confirmed template style. Old style presets and historical design defaults are superseded. ')
    for sentence in (
        'Visual language: a white or near-white canvas dominates; ',
        'Prefer whitespace and thin dividers over decorative panels, gradients or photo backgrounds. ',
    ):
        task = task.replace(sentence, '')
    task = task.replace('on a white canvas', 'on a canvas matching the confirmed template')
    task = task.replace('on the white 16:9 canvas', 'on the confirmed template background at 16:9')
    value['task'] = task + (' The confirmed template images are the active visual source. '
        'Inherit their actual background, primary palette, typography hierarchy, frames and header/footer treatment. '
        'Earlier preset colours, fonts and decoration embedded in slide_spec or general defaults are superseded. '
        'Preserve confirmed project content and semantic structure. Only current explicit feedback and additional '
        'design_notes may modify this template style; never infer such permission from an old preset.')
    value.setdefault('visual_style', {})['canvas'] = 'Inherit the confirmed template background; no forced white or dark default.'
    spec = value.get('slide_spec')
    if isinstance(spec, dict):
        for key in ['style', 'visual_style', 'style_description', 'palette', 'typography', 'background', 'theme']:
            spec.pop(key, None)
        if isinstance(spec.get('layout'), dict):
            for key in ['style', 'palette', 'colors', 'background', 'typography', 'theme']:
                spec['layout'].pop(key, None)
    value['visual_priority'] = ['current_explicit_feedback_and_additional_design_notes',
        'confirmed_template_typography_palette_background_and_frame',
        'page_specific_template_composition', 'defaults_only_when_template_has_no_guidance']
    contract = value.setdefault('template_fidelity_contract', {})
    contract['keep_consistent'] = ['background', 'font_hierarchy', 'main_palette', 'header_footer', 'frame_treatment', 'spacing']
    contract['exceptions'] = 'Only current explicit feedback/additional design_notes and truthful source semantics; old style presets are replaced.'
    if 'body_regions' in contract:
        contract['body_regions'] = 'Follow the confirmed template frames, fills, borders and spacing.'
    academic = value.get('academic_presentation_skill')
    if isinstance(academic, dict) and isinstance(academic.get('design_policy'), dict):
        design = academic['design_policy']
        design['priority'] = ['current_explicit_feedback_and_additional_design_notes', 'confirmed_template_body_pages', 'academic_skill_defaults']
        for key in ['default_canvas', 'main_color_count', 'emphasis', 'important_regions', 'default_typography']:
            design.pop(key, None)
    return json.dumps(value, ensure_ascii=False)
