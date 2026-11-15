"""Native click-by-click Appear builds for explicitly annotated process objects.

Uses PresentationML timing nodes; adds no media, macros, links or playback code.
The exported timing structure is testable without claiming a PowerPoint playback
test. References: Microsoft Learn Timing, SequenceTimeNode, SetBehavior and
BuildParagraph API documentation (ISO/IEC 29500 PresentationML §19.5).
"""
from collections.abc import Mapping

from lxml import etree

from .wuyu_errors import PipelineError

_P = 'http://schemas.openxmlformats.org/presentationml/2006/main'


def _node(tag, parent=None, **attributes):
    node = etree.Element('{' + _P + '}' + tag)
    for key, value in attributes.items():
        node.set(key, str(value))
    if parent is not None:
        parent.append(node)
    return node


def _invalid(message):
    raise PipelineError('invalid_flow_animation', message)


def _shape_map(shapes):
    result = {}
    for shape in shapes:
        result[shape.shape_id] = shape
        if shape.shape_type == 6:
            result.update(_shape_map(shape.shapes))
    return result


def apply_flow_animation(slide, elements, shape_ids):
    """Append native timing and return a safe report without source content.

    ``shape_ids`` is an aligned list/tuple or ``{element_index: actual_shape_id}``.
    Only integer ``animation_step`` values 1..12 are supported. Objects in one
    step appear together; the next distinct step requires the next slide click.
    Unmarked objects are untouched. Zero/one distinct step creates no animation.
    Existing timing is never overwritten and every target must exist on slide.
    """
    if not isinstance(elements, list) or len(elements) > 500:
        _invalid('流程动画对象必须为不超过500个元素的列表。')
    tagged = []
    for index, element in enumerate(elements):
        if not isinstance(element, dict):
            _invalid('流程动画对象必须为有效的描摹对象。')
        if 'animation_step' not in element:
            continue
        step = element['animation_step']
        if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= 12:
            _invalid('流程动画步骤必须为1至12的整数。')
        tagged.append((index, step))
    report = {'version': 1, 'enabled': False, 'effect': 'appear',
              'trigger': 'click', 'steps': [], 'animated_shapes': 0,
              'playback_verified': False, 'native_content_changed': False}
    if len({step for _, step in tagged}) < 2:
        return {**report, 'reason': 'fewer_than_two_explicit_steps'}
    if isinstance(shape_ids, Mapping):
        indexed = shape_ids
    elif isinstance(shape_ids, (tuple, list)) and len(shape_ids) == len(elements):
        indexed = dict(enumerate(shape_ids))
    else:
        _invalid('流程动画必须绑定每个对象的真实PPT形状ID。')
    known = _shape_map(slide.shapes)
    groups, assignments = {}, {}
    presentation = slide.part.package.presentation_part.presentation
    page_area = presentation.slide_width * presentation.slide_height
    for index, step in tagged:
        shape_id = indexed.get(index)
        if isinstance(shape_id, bool) or not isinstance(shape_id, int) or shape_id not in known:
            _invalid('流程动画引用的PPT对象不存在。')
        if shape_id in assignments and assignments[shape_id] != step:
            _invalid('同一个PPT对象不能归属于多个出现步骤。')
        shape = known[shape_id]
        if shape.shape_type == 13 and shape.width * shape.height >= .85 * page_area:
            _invalid('不能把整页平面图片作为多步骤流程动画。')
        assignments[shape_id] = step
        group = groups.setdefault(step, [])
        if shape_id not in group:
            group.append(shape_id)
    if slide._element.find('{' + _P + '}timing') is not None:
        _invalid('页面已有动画，不能静默覆盖原有动画。')
    # Build off-slide first; a validation failure never partially mutates a slide.
    timing = _node('timing')
    next_id = 0
    def time_node(parent, **attributes):
        nonlocal next_id
        next_id += 1
        return _node('cTn', parent, id=next_id, **attributes)
    def start(parent, delay):
        _node('cond', _node('stCondLst', parent), delay=delay)
    top = time_node(_node('par', _node('tnLst', timing)),
                    dur='indefinite', restart='never', nodeType='tmRoot')
    sequence = _node('seq', _node('childTnLst', top), concurrent='1', nextAc='seek')
    main = time_node(sequence, dur='indefinite', nodeType='mainSeq')
    main_children = _node('childTnLst', main)
    builds = _node('bldLst', timing)
    for group_id, (step, target_ids) in enumerate(sorted(groups.items())):
        # A main sequence advances one indefinite group for each onNext event.
        click_group = time_node(_node('par', main_children), fill='hold')
        start(click_group, 'indefinite')
        simultaneous = time_node(_node('par', _node('childTnLst', click_group)), fill='hold')
        start(simultaneous, 0)
        effect = time_node(_node('par', _node('childTnLst', simultaneous)),
                           presetID='1', presetClass='entr', presetSubtype='0',
                           fill='hold', grpId=group_id, nodeType='clickEffect')
        start(effect, 0)
        effects = _node('childTnLst', effect)
        for shape_id in target_ids:
            setter = _node('set', effects)
            behavior = _node('cBhvr', setter)
            behavior_time = time_node(behavior, dur='1', fill='hold')
            start(behavior_time, 0)
            _node('spTgt', _node('tgtEl', behavior), spid=shape_id)
            _node('attrName', _node('attrNameLst', behavior)).text = 'style.visibility'
            _node('strVal', _node('to', setter), val='visible')
            if known[shape_id].has_table or known[shape_id].has_chart:
                _node('bldAsOne', _node('bldGraphic', builds, spid=shape_id, grpId=group_id))
            else:
                _node('bldP', builds, spid=shape_id, grpId=group_id)
        report['steps'].append({'step': step, 'shape_ids': target_ids})
    for tag, event in (('prevCondLst', 'onPrev'), ('nextCondLst', 'onNext')):
        condition = _node('cond', _node(tag, sequence), evt=event, delay=0)
        _node('sldTgt', _node('tgtEl', condition))
    # CT_Slide order: cSld, clrMapOvr?, transition?, timing?, extLst?.
    extension = slide._element.find('{' + _P + '}extLst')
    if extension is None:
        slide._element.append(timing)
    else:
        slide._element.insert(slide._element.index(extension), timing)
    return {**report, 'enabled': True, 'animated_shapes': len(assignments),
            'time_nodes': next_id, 'reason': 'native_grouped_appear_timing_added'}
