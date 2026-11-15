"""Deterministic task-aware design matching; template placeholders never become evidence."""
import re

PURPOSES = {
    '博士论文答辩': ('博士', 'phd', 'doctoral'), '硕士论文答辩': ('硕士', 'master'),
    '开题答辩': ('开题', 'proposal_defense'), '资格考核': ('博资', '资格', 'qualification'),
    '成果报奖': ('报奖', '奖项', 'award'), '项目申请': ('项目申请', '项目申报', '基金', 'grant', 'project_application'),
    '企业介绍': ('企业介绍', '公司介绍', 'company'), '商业计划': ('商业计划', '融资', 'business_plan'),
    '技术方案': ('技术方案', 'technical_proposal'), '学术汇报': ('学术', '科研', '论文', 'research', 'academic'),
}

ACADEMIC_FAMILIES = ('user_library-478d24a90895d946', 'user_library-8a1aa7dd468c5d68',
                     'user_library-78549ac0c9996386', 'user_library-05366c65ec9cd6ba')
ACADEMIC_TASKS = {'thesis_defense': 0, 'master_defense': 0, 'doctoral_defense': 0,
                 'degree': 0, 'qualifying_exam': 1, 'academic_report': 2, 'paper': 2,
                 'paper_report': 2, 'academic': 2, 'proposal_defense': 3}


def _academic_preference(brief):
    """Honor the owner's four real academic decks and explicit stated task.

    Filenames and uploaded document contents are deliberately not examined.
    A technical proposal is not classified as a thesis merely because its
    reference material happens to mention a doctoral paper.
    """
    task = str(brief.get('subscenario', '')).lower()
    if task in ACADEMIC_TASKS:
        return ACADEMIC_FAMILIES[ACADEMIC_TASKS[task]]
    goal = str(brief.get('goal', '')).lower()
    if (str(brief.get('scenario', '')).lower() not in {'research', 'scientific'}
            or task in {'grant', 'award', 'talent', 'business_plan', 'product', 'market', 'bid'}
            or any(word in goal for word in ('项目申请', '项目申报', '基金申请', '基金申报', '报奖',
                                             '企业介绍', '企业汇报', '商业计划', '商业汇报', '客户方案', '投标'))):
        return None
    for text in (goal, str(brief.get('topic', '')).lower()):
        markers = ((1, ('资格考试', '资格考核', '博资考', 'qualifying exam')),
                   (3, ('开题答辩', '开题汇报', '开题报告')),
                   (0, ('毕业答辩', '论文答辩', '学位答辩', '毕业汇报')),
                   (2, ('文献汇报', '论文汇报', '文献分享', '论文分享', '组会', 'journal club')))
        matches = [(text.index(word), number) for number, words in markers for word in words if word in text]
        if matches:
            return ACADEMIC_FAMILIES[min(matches)[1]]
    return None


def rank_templates(records, brief):
    """Return records plus plain-language reasons; prefer task fit and varied layout families."""
    context = ' '.join(str(brief.get(key, '')) for key in
                       ('scenario', 'subscenario', 'topic', 'goal', 'requirements')).lower()
    purposes = {purpose for purpose, markers in PURPOSES.items() if any(marker in context for marker in markers)}
    if not purposes:
        purposes = {'学术汇报'} if brief.get('scenario') in {'scientific', 'research'} else {'技术方案'}
    academic_preference = _academic_preference(brief)
    style = str(brief.get('style', ''))
    tokens = {term for term in re.split(r'[\s，、,；;/]+', style) if 1 < len(term) <= 16}
    category = next((name for name in ['工艺流程图','技术路线','摘要图','论文方法图'] if name in context), None)
    if category is None:
        category = {'flow':'工艺流程图','roadmap':'技术路线'}.get(brief.get('diagram_preset'))
    scored = []
    for record in records:
        tags = record.get('tags', {})
        matched = purposes.intersection(tags.get('purpose', []))
        styles = set(tags.get('visual_style', [])).intersection(tokens)
        layouts = set(tags.get('layout_structure', []))
        score = 20 * len(matched) + 4 * len(styles) + min(4, len(layouts))
        score += min(6, int(record.get('body_reference_count', 0)))
        if category in tags.get('template_category', []): score += 150
        if academic_preference and record['id'] in ACADEMIC_FAMILIES:
            score += 120 if record['id'] == academic_preference else 100
        reason = ('适合' + '、'.join(sorted(matched)) if matched else '适合当前内容的通用表达')
        if academic_preference and record['id'] in ACADEMIC_FAMILIES:
            reason = '优先参考已指定的真实学术模板' + ('，与本次任务类型相符' if record['id'] == academic_preference else '')
        if category in tags.get('template_category', []): reason = '符合'+category+'分类；'+reason
        if layouts:
            reason += '，包含' + '、'.join(sorted(layouts)[:3]) + '等版式'
        scored.append((score, str(record['id']), record, reason))
    return [(record, reason) for _, _, record, reason in sorted(scored, key=lambda row: (-row[0], row[1]))]
