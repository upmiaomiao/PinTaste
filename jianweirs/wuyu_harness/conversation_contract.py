"""Conversational task contract. Uploaded clues are quoted data, never commands.

The dialogue map is transient UI state. It cannot change permissions, charge a
quota or certify a geographic boundary extracted from an attachment.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import re

from fastapi import HTTPException

from .conversation import preflight, _question

GOALS = {'discovery', 'changes', 'environment'}


def today():
    return datetime.now(timezone(timedelta(hours=8))).date()


def normalise_dialogue(body, documents=()):
    body = deepcopy(body)
    dialogue = body.pop('_dialogue', {})
    if (not isinstance(dialogue, dict) or set(dialogue) - {'mode', 'goal', 'time_choice', 'material_choice', 'policy_choice'}
            or any(not isinstance(v, str) or len(v) > 50 for v in dialogue.values())):
        raise HTTPException(422, '对话选项无效，请重新确认需求')
    allowed = {'mode': {'complaint', 'image', 'document'}, 'goal': GOALS,
               'time_choice': {'latest', 'custom', 'since2021', 'recent90', 'this_year'},
               'material_choice': {'complaint', 'summary'}, 'policy_choice': {'national', 'local'}}
    if any(value not in allowed[key] for key, value in dialogue.items()):
        raise HTTPException(422, '对话选项无效')
    objective = str(body.get('objective', ''))
    texts = [str(d.get('text', ''))[:12000] for d in documents if d.get('document_kind') != 'xlsx']
    material = '\n\n'.join(texts)[:12000]
    explicit = bool(re.search(r'举报|投诉|群众反映|有人反映|覆盖式扫描|区域扫描|周边扫描', objective))
    excluded = bool(re.search(r'不是举报|不要(?:举报|区域|周边)?扫描|不做(?:区域|周边)?扫描', objective))
    if explicit and not excluded and dialogue.get('mode') != 'image':
        dialogue['mode'] = 'complaint'
    if dialogue.get('material_choice') == 'complaint':
        dialogue['mode'] = 'complaint'
    if dialogue.get('material_choice') == 'summary':
        dialogue['mode'] = 'document'
        body['search_only'] = True
        body['objective'] = objective + '\n本轮只整理上传材料中的事实主张、位置与时间线索，不申请卫星影像。'
    if body.get('online_supplement') == 'uploaded_only':
        dialogue['mode'] = 'image'
        body.pop('complaint_scan', None)
    if dialogue.get('mode') == 'complaint':
        spec = body.setdefault('complaint_scan', {})
        if not isinstance(spec, dict):
            raise HTTPException(422, '举报区域格式无效')
        # The document can supply clue text, never consent or trusted coordinates.
        spec.setdefault('text', (objective + ('\n用户上传的线索原文（未独立验证）：\n' + material if material else ''))[:12000])
        spec.setdefault('region_confirmed', False)
        spec.setdefault('zoom', 19)
        spec.setdefault('max_windows', 16)
        goal = dialogue.get('goal')
        if goal:
            kinds = {'discovery': ['suspected_site_discovery', 'waste_type_screening'],
                     'changes': ['stockpile_change', 'cleanup_progress'],
                     'environment': ['suspected_site_discovery', 'environmental_context', 'surrounding_relationship']}
            body['task_types'] = kinds[goal]
    if not body.get('date_start') and not body.get('date_end'):
        # The period is parsed once here, with the same parser the acquisition
        # layer uses, and stored on the task. Written years and relative cues
        # ("近3个月", "去年") both count; an undated "December 1" still asks.
        from .optical_time import optical_time_intent
        from .schemas import TaskRequest
        intent = optical_time_intent(TaskRequest(objective=objective, task_types=['suspected_site_discovery']))
        if intent.get('requested_start') and intent.get('requested_end'):
            body.update(date_start=intent['requested_start'], date_end=intent['requested_end'])
    return body, dialogue, material


def around(center, radius):
    if not isinstance(center, (list, tuple)) or len(center) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in center):
        raise ValueError('请先确认有效的检索中心')
    lon, lat = center
    if not -180 <= lon <= 180 or abs(lat) > 84:
        raise ValueError('请改为提供明确的 WGS84 边界')
    dy = radius / 111320
    dx = dy / math.cos(math.radians(lat))
    bounds = [round(lon-dx, 7), round(lat-dy, 7), round(lon+dx, 7), round(lat+dy, 7)]
    if bounds[0] < -180 or bounds[2] > 180:
        raise ValueError('跨日期变更线的区域请分区核查')
    return bounds


def dialogue_question(body, dialogue, *, parent_task=None, attachments=(), material=''):
    from .task_modes import resolve_workflow, confirmed_by_user
    mode = resolve_workflow(body, parent_task)
    if mode == 'contextual_qa':
        return None
    if (mode == 'environmental_risk' and 'waste_status' not in body
            and not confirmed_by_user(str(body.get('objective', '')))):
        return _question('waste_confirmation', '这处固废堆存是否已经由你或现场材料确认？我会据此区分既有堆存风险评估与条件性风险分析。', [
            ('user_confirmed', '已确认存在固废堆存', '直接评估环境场景、迁移路径和风险等级，并交叉复核。'),
            ('unverified', '还未确认，先评估可能风险', '保留物料性质的不确定性，给出条件性风险与需要补证的内容。')])
    if (material and not dialogue.get('mode') and not dialogue.get('material_choice')
            and re.search(r'举报|投诉|反映|倾倒', material)):
        return _question('material_goal', '我读到材料里包含举报或倾倒线索。这次希望我开展哪种工作？', [
            ('complaint', '按线索开展遥感核查', '先确认地点、范围和时段，再逐窗取证。'),
            ('summary', '先整理材料与核查要点', '提炼原文主张与缺失信息，暂不申请影像。')])
    complaint = mode == 'complaint_investigation'
    if complaint and not dialogue.get('goal'):
        return _question('complaint_goal', '这条举报最希望核查什么？我会据此安排影像和证据。', [
            ('discovery', '是否有堆存及大致类型', '排查露天疑点，区分建筑、施工物料和疑似废弃堆体。'),
            ('changes', '何时出现或是否已清理', '检索有实际日期的历史影像，比较同一范围。'),
            ('environment', '周边环境与风险关系', '重点查看水体、沟渠、居民区及可能的迁移路径。')])
    answers = {'comparison_period': 'current_only'} if dialogue.get('time_choice') == 'current_only' else None
    standard = preflight(body, parent_task=parent_task, attachments=attachments, answers=answers)
    if standard['question']:
        return standard['question']
    spec = body.get('complaint_scan') or {}
    if complaint and not spec.get('region_confirmed'):
        bounds = spec.get('region_bbox') or body.get('bbox')
        if bounds:
            return _question('complaint_region_confirm',
                '我已整理待核查边界：' + '，'.join(str(v) for v in bounds) + '（WGS84 西、南、东、北）。是否按这一范围逐窗核查？', [
                    ('confirm', '确认这个范围', '只在这片范围内安排扫描，覆盖不足会明确列出。'),
                    ('revise', '重新调整范围', '可缩小或扩大周边范围，也可直接给出边界。')], region_bbox=bounds)
        return _question('complaint_region', '地点已有参考位置。你希望我先核查周边多大范围？', [
            ('250', '周边约 250 米', '先覆盖最接近线索的位置，再按疑点追加复核。'),
            ('500', '周边约 500 米', '覆盖村边道路、空地和沟渠等周边地块。'),
            ('custom', '我提供具体边界', '在下方直接填写 WGS84 西、南、东、北四个数值。')])
    scientific = bool(re.search(r'多光谱|高光谱|雷达|(?<![A-Za-z])SAR(?![A-Za-z])|Sentinel|Landsat|EnMAP|EMIT', str(body.get('objective', '')), re.I)
                      or set(body.get('selected_sources') or []) & {'sentinel2','landsat','enmap','emit','sentinel1-rtc','sentinel1-grd','sentinel1-slc'})
    if not body.get('date_start') or not body.get('date_end'):
        if scientific:
            return _question('science_period', '这次科学影像需要覆盖哪段时间？我会按实际场景拍摄时间检索。', [
                ('recent90', '最近三个月', '查询该时段实际可用的多光谱、雷达或高光谱场景。'),
                ('this_year', '今年至今', '按实际获取日期选择场景，另行核对覆盖和质量。'),
                ('custom', '我指定时间范围', '直接告诉我年份、月份或开始及结束日期。')])
        if complaint and dialogue.get('time_choice') != 'latest':
            return _question('complaint_period', '你希望核查举报发生时的情况，还是先看可取得的近期影像？', [
                ('latest', '先看近期可用影像', '拍摄日期未知会单独说明，不把版次当日期。'),
                ('custom', '核查指定年份或时段', '直接告诉我发现日期或希望比较的起止时间。'),
                ('since2021', '从历史到最近做时序比较', '先检索2021年至今可用时相，再筛选可比较的影像。')])
    if (body.get('search_policy') is True and dialogue.get('policy_choice') != 'national'
            and body.get('jurisdiction', '中国') in {'', '中国', '全国'}
            and re.search(r'环境|风险|地方政策|当地政策|省.{0,3}政策', str(body.get('objective', '')))):
        return _question('policy_region', '为匹配地方政策，这个点位属于哪个省、市或地区？', [
            ('national', '先查国家层面的规定', '地方辖区未确认时，先给出国家层面的管理依据。'),
            ('local', '我补充所在省市', '直接输入例如“四川省成都市”，再匹配省级和市县规定。')])
    return None


def apply_dialogue_reply(body, dialogue, kind, value):
    """Called only for explicit UI replies, never with extracted attachment text."""
    if kind == 'waste_confirmation' and value in {'user_confirmed','unverified'}:
        body['waste_status'] = value
    elif kind == 'material_goal' and value in {'complaint', 'summary'}:
        dialogue['material_choice'] = value
    elif kind == 'complaint_goal' and value in GOALS:
        dialogue['goal'] = value
    elif kind == 'complaint_region' and value in {'250', '500'}:
        spec = body.setdefault('complaint_scan', {})
        spec['region_bbox'] = around(body.get('center'), int(value))
        spec['region_confirmed'] = False
    elif kind == 'complaint_region_confirm' and value in {'confirm', 'revise'}:
        spec = body.setdefault('complaint_scan', {})
        if value == 'confirm':
            spec['region_bbox'] = spec.get('region_bbox') or body.get('bbox')
            spec['region_confirmed'] = True
        else:
            spec.pop('region_bbox', None)
            spec['region_confirmed'] = False
            if body.get('bbox'):
                w, s, e, n = body.pop('bbox')
                body['center'] = [(w+e)/2, (s+n)/2]
    elif kind == 'comparison_period' and value == 'current_only':
        # Narrow to the present scene without rewriting the user's question.
        dialogue['time_choice'] = 'current_only'
        body['task_types'] = ['suspected_site_discovery']
        body['research'] = {'mode': 'auto', 'sources': []}
        body['workflow_mode'] = 'site_investigation'
    elif kind == 'free_text' and value.strip():
        # A free-form answer is kept as a separate supplement for the planner;
        # the objective (and thus the routed mode) is not rewritten.
        supplements = dialogue.setdefault('supplements', [])
        supplements.append(value.strip()[:500])
        dialogue['supplements'] = supplements[-6:]
        note = '\n'.join('用户补充：' + item for item in dialogue['supplements'])
        base = str(body.get('prompt') or '').split('\n[用户补充]\n')[0]
        body['prompt'] = (base + '\n[用户补充]\n' + note)[-4000:]
        from .optical_time import optical_time_intent
        from .schemas import TaskRequest
        if not body.get('date_start') and not body.get('date_end'):
            intent = optical_time_intent(TaskRequest(objective=value.strip()[:500], task_types=['suspected_site_discovery']))
            if intent.get('requested_start') and intent.get('requested_end'):
                body.update(date_start=intent['requested_start'], date_end=intent['requested_end'])
    elif kind in {'complaint_period', 'science_period', 'comparison_period'} and value in {'latest', 'custom', 'since2021', 'recent90', 'this_year'}:
        dialogue['time_choice'] = value
        if value == 'latest' and dialogue.get('goal') == 'changes':
            dialogue['goal'] = 'discovery'
        end = today()
        if value in {'since2021', 'recent90', 'this_year'}:
            start = end.replace(year=2021, month=1, day=1) if value == 'since2021' else end-timedelta(days=90) if value == 'recent90' else end.replace(month=1, day=1)
            body.update(date_start=start.isoformat(), date_end=end.isoformat())
            if value == 'since2021':
                body['research'] = {'mode': 'timeseries', 'sources': []}
    elif kind == 'policy_region' and value in {'national', 'local'}:
        dialogue['policy_choice'] = value
    else:
        raise HTTPException(422, '对话选择无效，请重新确认')
