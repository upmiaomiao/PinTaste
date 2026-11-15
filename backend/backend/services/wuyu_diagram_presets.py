"""Professional diagram presets: the "template library" for single-figure (asset/diagram) work.

A deck template is a set of slide pages; a professional figure needs something different — a visual
GRAMMAR: how a flow, an architecture or a concept sketch is laid out, labelled and connected. Each
preset below is that grammar, written as direction for the image model and as layout guidance for
the planner. Colours and brand come from the user's template / design notes, not from here.

Choosing is optional. Without a preset the product behaves as before; with one, the figure follows
the grammar. A preset can be named by the user (`diagram_preset`) or inferred from plain words in the
request (流程 / 架构 / 路线 ...) — inference only fires for a clear cue, never guesses.
"""
import re

PRESETS = {
    'flow': {
        'label': '流程图',
        'when': '工艺流程、业务流程、操作步骤、处理链条',
        'layout': 'ordered_flow',
        'rules': ('Ordered process figure. Read direction is one of left-to-right or top-to-bottom, never mixed. '
                  'Stages are equal-weight rounded rectangles in one row (or snake at most once for 8+ stages); '
                  'each stage has a short verb-first label and a numbered chip. Arrows are uniform single-line '
                  'connectors that never cross; branch and merge points are small diamonds with the condition written '
                  'on the outgoing arrow. Group stages that belong to one phase with a light background band and a '
                  'band title. Show inputs on the far left and outputs on the far right. Keep every label under 12 '
                  'Chinese characters; move detail into a one-line caption under the stage.')},
    'architecture': {
        'label': '架构图',
        'when': '系统架构、技术架构、平台组成、软硬件层次',
        'layout': 'layered_architecture',
        'rules': ('Layered architecture figure. Stack horizontal layers top-to-bottom from user-facing to infrastructure, '
                  'each layer a full-width band with a left-edge layer name. Inside a layer, components are equal-size '
                  'boxes aligned on one grid; related components share a labelled dashed frame. Vertical connectors show '
                  'calls between layers, with the interface or protocol written on the connector when known. Cross-cutting '
                  'concerns (security, monitoring, data) are tall bars at the side spanning several layers. External '
                  'systems sit outside the main frame. One accent colour marks the core component; everything else is '
                  'neutral. No decorative icons that carry no meaning.')},
    'concept': {
        'label': '概念/原理示意图',
        'when': '抽象概念、原理机制、关系示意、一页讲清一个想法',
        'layout': 'concept_map',
        'rules': ('Concept illustration. One dominant central idea (largest shape, strongest colour) with at most six '
                  'supporting elements arranged radially or in a clear triad, each tied to the centre by a labelled '
                  'relationship line (verbs such as 驱动 / 约束 / 转化). Use simple geometric pictograms for meaning, '
                  'not clip-art. Whitespace is part of the design: leave at least 20% of the canvas empty. One short '
                  'takeaway sentence at the bottom. Do not add data, numbers or claims that are not in the request.')},
    'roadmap': {
        'label': '技术路线图',
        'when': '技术路线、研究路线、阶段规划、里程碑',
        'layout': 'phased_roadmap',
        'rules': ('Technical roadmap figure. A single horizontal time or phase axis across the canvas; 3 to 5 phase '
                  'columns, each with a phase name, the key task(s) as short bullet chips, and a deliverable or '
                  'milestone marker (flag or diamond) at the bottom. Parallel workstreams are swim rows labelled at the '
                  'left; dependencies are thin arrows between chips. The final column states the expected outcome in a '
                  'highlighted box. Dates or durations appear only if supplied.')},
    'comparison': {
        'label': '对比/矩阵图',
        'when': '方案对比、优缺点、指标对照、二维定位',
        'layout': 'comparison_matrix',
        'rules': ('Comparison figure. Either a table-like matrix (options as columns, criteria as rows, equal cell '
                  'sizes, header row and first column visually distinct) or a 2x2 positioning grid with labelled axes '
                  'and one highlighted quadrant. Use consistent symbols or short phrases per cell, never long '
                  'sentences. Mark the recommended option with a single accent outline and a one-line reason. '
                  'Do not invent scores or numbers that are not supplied.')},
}

# Plain-word cues. A cue must be unambiguous; "系统" alone is too broad, "系统架构" is not.
_CUES = (
    ('architecture', re.compile(r'架构|技术栈|系统组成|平台组成|分层')),
    ('roadmap', re.compile(r'技术路线|研究路线|路线图|里程碑|阶段规划')),
    ('flow', re.compile(r'流程图|工艺流程|业务流程|操作流程|流程')),
    ('comparison', re.compile(r'对比|比较|优缺点|矩阵|四象限|2\s*[x×*]\s*2')),
    ('concept', re.compile(r'原理|机制示意|概念图|示意图|抽象')),
)


def known(preset_id):
    return preset_id in PRESETS


def infer_preset(brief):
    """A preset id when the request clearly names one kind of figure, else None."""
    text = ' '.join(str(brief.get(key) or '') for key in ('topic', 'goal', 'requirements'))
    for preset_id, pattern in _CUES:
        if pattern.search(text):
            return preset_id
    return None


def resolve(brief):
    """The preset to use for this brief: an explicit valid choice, else a clear cue, else None."""
    chosen = brief.get('diagram_preset')
    if isinstance(chosen, str) and chosen in PRESETS:
        return chosen
    if brief.get('creation_kind') == 'asset' and brief.get('asset_kind', 'diagram') == 'diagram':
        return infer_preset(brief)
    return None


def direction(preset_id):
    """What the planner and the image model receive. Empty when there is no such preset."""
    preset = PRESETS.get(preset_id)
    if not preset:
        return {}
    return {'id': preset_id, 'label': preset['label'], 'layout_structure': preset['layout'], 'rules': preset['rules']}


def catalog():
    """Public list for pickers: id, Chinese label, and when to use it. No prompt text."""
    return [{'id': key, 'label': value['label'], 'when': value['when']} for key, value in PRESETS.items()]
