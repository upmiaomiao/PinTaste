"""What a deck is for: one fixed vocabulary of presentation types.

The brief stores it as ``deck_type`` with ``deck_type_source``:

* ``stated``   - set explicitly by the user or the agent (update_brief payload). Never overwritten
                  by inference; only another explicit value (or null) changes it.
* ``inferred`` - derived here from the stated topic, goal and requirements whenever the brief
                  changes. ``None`` when nothing in the brief names a type; the code never guesses.

``subscenario`` is left untouched: changing it re-plans reading and switches the academic skill.
Template choice and recommendation read the effective type through ``effective_deck_type``.
Uploaded material contents and file names are never examined.
"""
import re

# (code, label, scenario, markers). Order matters only for ties at the same text position:
# more specific types come first (博士论文答辩 before 论文答辩).
DECK_TYPES = (
    ('doctoral_defense', '博士论文答辩', 'research', ('博士答辩', '博士论文', '博士学位', '博士毕业', 'phd', 'doctoral')),
    ('master_defense', '硕士论文答辩', 'research', ('硕士答辩', '硕士论文', '硕士学位', '硕士毕业', 'master thesis')),
    ('thesis_defense', '毕业论文答辩', 'research', ('毕业答辩', '论文答辩', '学位答辩', '毕业汇报', '本科答辩', 'thesis defense')),
    ('proposal_defense', '开题答辩', 'research', ('开题答辩', '开题汇报', '开题报告', '开题')),
    ('qualifying_exam', '资格考核', 'research', ('资格考试', '资格考核', '博资考', 'qualifying exam')),
    ('academic_report', '学术汇报', 'research', ('文献汇报', '论文汇报', '文献分享', '论文分享', '组会', '学术汇报',
                                                  '学术报告', '学术会议', 'journal club')),
    ('grant', '项目申请', 'research', ('项目申请', '项目申报', '基金申请', '基金申报', '课题申报', '课题申请', '申请书', '国自然')),
    ('award', '成果报奖', 'research', ('报奖', '奖项申报', '成果奖', '奖励申报', '评奖')),
    ('business_plan', '商业计划', 'commercial', ('商业计划', '融资', '路演', '投资人', 'business plan', 'bp')),
    ('company_intro', '企业介绍', 'commercial', ('企业介绍', '公司介绍', '企业宣传', '公司简介', '企业简介', '品牌介绍')),
    ('work_report', '工作汇报', 'commercial', ('工作汇报', '年终总结', '年度总结', '述职', '季度汇报', '工作总结', '月度汇报', '周报', '月报')),
    ('training', '培训课件', 'commercial', ('培训', '课件', '讲座', '教学')),
    ('technical_proposal', '技术方案', 'commercial', ('技术方案', '解决方案', '技术路线', '系统架构', '方案设计', '投标', '标书')),
)
CODES = tuple(code for code, _, _, _ in DECK_TYPES)
LABELS = {code: label for code, label, _, _ in DECK_TYPES}
# Codes the existing academic template families and academic skill already understand.
ACADEMIC_CODES = {'doctoral_defense', 'master_defense', 'thesis_defense', 'proposal_defense',
                  'qualifying_exam', 'academic_report'}
DEGREE_CODES = {'doctoral_defense', 'master_defense', 'thesis_defense'}
OCCASION_CODES = ('proposal_defense', 'qualifying_exam')
_LATIN = re.compile(r'[a-z]')


def _positions(text, marker):
    """Start offsets of a marker; Latin markers must be whole words (``bp`` is not inside ``abps``)."""
    if _LATIN.search(marker):
        return [m.start() for m in re.finditer(r'(?<![a-z0-9])' + re.escape(marker) + r'(?![a-z0-9])', text)]
    found, start = [], text.find(marker)
    while start >= 0:
        found.append(start)
        start = text.find(marker, start + 1)
    return found


def _first(text):
    best, found = None, set()
    for rank, (code, _, _, markers) in enumerate(DECK_TYPES):
        for marker in markers:
            for position in _positions(text, marker):
                found.add(code)
                key = (position, rank)
                if best is None or key < best[0]:
                    best = (key, code)
    if best is None:
        return None
    # The occasion outranks the degree: "博士学位论文开题答辩" is a proposal defense, not a final one.
    if best[1] in DEGREE_CODES:
        for occasion in OCCASION_CODES:
            if occasion in found:
                return occasion
    return best[1]


def infer_deck_type(brief):
    """The type the brief's own words name first; topic/goal outrank the longer requirements."""
    if not isinstance(brief, dict):
        return None
    head = ' '.join(str(brief.get(key) or '') for key in ('topic', 'goal')).lower()
    found = _first(head)
    if found:
        return found
    return _first(str(brief.get('requirements') or '').lower()[:4000])


def valid(code):
    return code is None or code in CODES


def effective_deck_type(brief):
    """Stated type, else the stored inference, else a fresh inference, else a non-default subscenario."""
    if not isinstance(brief, dict):
        return None
    stored = brief.get('deck_type')
    if stored in CODES:
        return stored
    if brief.get('deck_type_source') == 'stated':
        return None  # explicitly cleared
    found = infer_deck_type(brief)
    if found:
        return found
    sub = str(brief.get('subscenario') or '').lower()
    return sub if sub in CODES and sub != 'technical_proposal' else None


def apply_to_brief(brief, payload):
    """Update ``brief`` in place after an update_brief merge. ``payload`` is the caller's patch."""
    if 'deck_type' in payload:
        value = payload['deck_type']
        if value is not None and value not in CODES:
            raise ValueError('Unknown deck_type')
        if value is None:
            brief.pop('deck_type', None)
        else:
            brief['deck_type'] = value
        brief['deck_type_source'] = 'stated'
        return
    if brief.get('deck_type_source') == 'stated':
        return
    found = infer_deck_type(brief)
    if found:
        brief['deck_type'], brief['deck_type_source'] = found, 'inferred'
    else:
        brief.pop('deck_type', None)
        brief.pop('deck_type_source', None)


def label(code):
    return LABELS.get(code)
