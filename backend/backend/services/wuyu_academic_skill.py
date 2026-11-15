"""Scoped, source-addressed academic presentation guidance for the product."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path


SKILL_NAME = 'academic-research-ppt'
SKILL_VERSION = 1
_SKILL_RELATIVE = Path('skills') / SKILL_NAME / 'SKILL.md'
_SKILL_PATH = Path(__file__).resolve().parents[2] / _SKILL_RELATIVE

_ACADEMIC_SCENARIOS = frozenset({'research', 'academic', 'scientific', 'academic_report',
                                '科研', '科研汇报', '学术', '学术汇报'})
_ACADEMIC_PURPOSES = frozenset({
    'thesis_defense', 'master_defense', 'doctoral_defense', 'proposal_defense',
    'academic_report', 'qualifying_exam', 'degree', 'paper', 'paper_report', 'academic',
    'journal_club', 'research_report', 'research_proposal',
    '学位答辩', '毕业答辩', '博士论文答辩', '硕士论文答辩', '论文答辩',
    '开题答辩', '开题报告', '资格考试', '学术报告', '学术汇报', '科研汇报', '论文汇报', '文献汇报', '组会汇报',
})
_BUSINESS_SCENARIOS = frozenset({'commercial', 'business', 'enterprise', 'corporate',
                                '商业', '商业汇报', '企业', '企业汇报'})
_OTHER_PURPOSES = frozenset({'grant', 'award', 'talent', 'business_plan', 'product', 'market',
                           'bid', 'pitch', 'corporate_introduction', 'company_introduction',
                           '报奖', '项目申请', '基金申请', '企业介绍', '商业计划', '商业方案', '投标'})


class AcademicSkillUnavailable(RuntimeError):
    """The applicable product skill was not packaged with its model guidance."""


def _tag(value):
    return value.strip().casefold().replace(' ', '_') if isinstance(value, str) else ''


def academic_skill_applies(brief: Mapping) -> bool:
    """Resolve academic intent only from structured scenario and purpose fields."""
    scenario = _tag(brief.get('scenario'))
    purposes = {_tag(brief.get(key)) for key in ('subscenario', 'purpose')} - {''}
    if scenario in _BUSINESS_SCENARIOS or purposes & _OTHER_PURPOSES or _tag(brief.get('creation_kind')) == 'asset':
        return False
    if purposes & _ACADEMIC_PURPOSES:
        return True
    # Research without a more specific task still receives research guidance;
    # an explicit, unrelated purpose never inherits a thesis workflow.
    return scenario in _ACADEMIC_SCENARIOS and not purposes


def academic_presentation_skill(brief: Mapping) -> dict | None:
    """Return a fresh policy plus exact packaged SKILL.md and its SHA-256."""
    if not academic_skill_applies(brief):
        return None
    try:
        raw = _SKILL_PATH.read_bytes()
        instruction = raw.decode('utf-8')
    except (OSError, UnicodeError) as error:
        raise AcademicSkillUnavailable('ACADEMIC_SKILL_UNAVAILABLE') from error
    if not instruction.startswith('---\nname: ' + SKILL_NAME + '\n'):
        raise AcademicSkillUnavailable('ACADEMIC_SKILL_INVALID')
    return {
        'name': SKILL_NAME, 'version': SKILL_VERSION,
        'source_path': _SKILL_RELATIVE.as_posix(), 'sha256': hashlib.sha256(raw).hexdigest(),
        'instruction_markdown': instruction,
        'content_policy': {
            'narrative': ['research_question_and_boundary', 'specific_challenge', 'method',
                          'verified_results', 'scientific_meaning', 'evidence_backed_innovation'],
            'source_grounding': 'Bind each claim, value, qualifier and figure to current-project evidence; distinguish plans, external knowledge, interpretation and demonstrated findings.',
            'visible_chinese_characters': {'suggested_maximum': 150, 'hard_limit': False,
                'preserve_units_denominators_uncertainty': True, 'full_evidence_in_notes': True},
            'speaker_notes': {'suggested_characters': [30, 50], 'hard_limit': False,
                              'longer_when_evidence_requires': True},
            'page_count': {'use_confirmed_brief': True, 'infer_45_pages_from_15_minutes': False},
            'pre_image_review': ['title', 'claim', 'visible_copy', 'evidence_locations',
                                 'source_figure_references', 'layout_rationale', 'notes'],
        },
        'design_policy': {
            'priority': ['explicit_current_user_requirements', 'confirmed_template_body_pages', 'academic_skill_defaults'],
            'template_fidelity': ['font_hierarchy', 'primary_color', 'frames', 'header_footer', 'spacing', 'figure_text_relationship'],
            'default_canvas': 'white_or_light_gray', 'main_color_count': 1,
            'emphasis': 'Reserve restrained red for scientific keywords or key findings; verified chart categories may use additional consistent semantic colors.',
            'important_regions': 'Use one consistent white/gray frame treatment, a fine border or subtle shadow when useful; never different decorative colors for each region.',
            'composition': 'Choose figures, method flows, aligned comparisons or evidence matrices from the content; do not force three cards/columns or icons on every page.',
            'icons': 'Use only when they explain an actual object, process or relationship; keep a consistent semantic style.',
            'default_typography': {'chinese': '微软雅黑', 'latin_and_digits': 'Arial',
                'title_pt': 30, 'body_pt': [18, 24], 'figure_caption_pt': 14,
                'adjust_to_canvas_and_confirmed_template': True, 'consistent_semantic_roles': True},
            'source_figures': 'Use relevant original figures as image-generation references with original scientific labels and relationships; use grounded text schematics when no suitable image exists.',
        },
    }


def academic_model_context(brief: Mapping, *, design_only=False) -> dict:
    skill=academic_presentation_skill(brief)
    if skill is None:return {}
    fields=('name','version','sha256','design_policy')if design_only else (
        'name','version','sha256','instruction_markdown','content_policy','design_policy')
    return {'academic_presentation_skill':{k:skill[k]for k in fields}}


def academic_reader_context(brief: Mapping) -> dict:
    skill=academic_presentation_skill(brief)
    if skill is None:return {}
    # Source extraction needs the research question and evidence contract;
    # full typography/layout guidance belongs to synthesis and image design.
    return {'academic_reading_focus':{'name':skill['name'],'sha256':skill['sha256'],
        'narrative':skill['content_policy']['narrative'],
        'source_grounding':skill['content_policy']['source_grounding']}}
