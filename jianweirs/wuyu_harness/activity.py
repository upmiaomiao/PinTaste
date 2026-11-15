"""Chronological, factual work records built from executed operations.

This is an allowlist view of durable events and owned assets, not a rendering of
model prompts, private provider payloads, or hidden reasoning. Legacy internal
phase markers are retained in storage but never expanded into a fixed UI flow.
"""
from __future__ import annotations

import re
from datetime import date

SOURCE_NAMES = {
    'amap': '高德影像', 'tencent': '腾讯影像', 'google': 'Google 影像',
    'arcgis': 'ArcGIS 世界影像', 'bing': 'Bing 影像', 'mapbox': 'Mapbox 影像',
    'mapquest': 'MapQuest 影像', 'tianditu': '天地图影像', 'siwei': '四维地球影像',
    'siwei_daily': '四维地球日新图', 'sentinel2': 'Sentinel-2 多光谱',
    'landsat': 'Landsat 多光谱', 'hls': 'HLS 多光谱',
    'sentinel1': 'Sentinel-1 SAR', 'sentinel1-rtc': 'Sentinel-1 RTC SAR',
    'emit': 'EMIT 高光谱', 'enmap': 'EnMAP 高光谱',
    'user_upload': '上传影像', 'user_workbook_embedded': '点位表内嵌影像',
    'gaofen2026': '2026 高清图源', 'century': '世纪空间年度影像', 'jilin2024': '吉林一号 2024 版',
    'siwei2025': '四维地球 2025 版', 'history246': 'Google 历史时序影像',
    'hls_s30': 'HLS Sentinel 多光谱', 'hls_l30': 'HLS Landsat 多光谱',
    'alos-palsar-rtc': 'ALOS PALSAR 雷达', 'nisar-gcov': 'NISAR 雷达', 'jaxa-palsar-mosaic': 'PALSAR 年度镶嵌',
}
TERMINAL = {'completed', 'succeeded', 'positive', 'negative', 'unknown', 'failed',
            'unavailable', 'background_only', 'partial', 'excluded', 'skipped',
            'not_requested', 'interrupted', 'cancelled', 'draft_pending_human_review'}


def _text(value, maximum=500):
    return value[:maximum].strip() if isinstance(value, str) else ''


def _identity(value):
    return value if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value) else None


def _count(value):
    return value if type(value) is int and 0 <= value <= 100000 else None


def _name(source):
    return SOURCE_NAMES.get(source, '所选图源')


def _acquisition_failure(event):
    raw = event.get('code')
    code = str(raw) if type(raw) is int or isinstance(raw,str) else ''
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',code):
        return '', ''
    reasons = {'401':'接口认证未通过', '403':'图源权限未通过', '404':'所请求资源未找到',
               '429':'图源接口繁忙或达到调用限额', '500':'上游图源服务发生错误',
               '502':'上游图源服务暂未正常返回', '503':'图源服务暂不可用', '504':'图源服务响应超时',
               'unified_timeout':'图源服务响应超时', 'unified_unavailable':'暂时无法连接图源服务',
               'unified_unauthorized':'图源认证或权限未通过', 'unified_rate_limited':'图源接口繁忙或达到调用限额',
               'unsupported_source_zoom':'该图源不支持请求的显示层级',
               'unsupported_requested_zoom':'该图源不支持请求的显示层级',
               'image_coverage_window_budget_exceeded':'所需覆盖窗口超过本次预算',
               'source_coverage_budget_exceeded':'所需覆盖窗口超过本次预算',
               'source_coverage_unavailable':'本次未能完成该来源的覆盖规划'}
    if code in reasons:
        return code, reasons[code]+'（代码 '+code+'）'
    if code.isdigit():
        return code, '图源服务返回错误代码 '+code
    return code, ''


def _dates(values, limit=8):
    if isinstance(values,dict):values=[values]
    if not isinstance(values,(list,tuple)):
        return []
    result=[]
    for value in values[:100]:
        if isinstance(value,dict):value=value.get('date')
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):continue
        try:date.fromisoformat(value)
        except ValueError:continue
        if value not in result:result.append(value)
        if len(result)>=limit:break
    return result


def _observation(value, prefix='我复核后发现：', maximum=900):
    text = _text(value, maximum)
    text = re.sub(r'[（(]\s*(?:uncertain|unknown|supports_candidate|obvious_false_positive)\s*[）)]', '', text, flags=re.I)
    return text if text.startswith('我') else prefix + text if text else ''


def _asset_facts(asset):
    metadata = asset.get('metadata') or {}
    actual = asset.get('data_api_metadata') or {}
    facts = {'width': asset.get('width'), 'height': asset.get('height')}
    source_label = _text(actual.get('map_name'), 120)
    if source_label:
        facts['source_label'] = source_label
    zoom = actual.get('zoom')
    if type(zoom) is int and 0 <= zoom <= 30:
        facts['zoom'] = zoom
    elif type((asset.get('requested_view') or {}).get('zoom')) is int:
        facts['requested_zoom'] = asset['requested_view']['zoom']
        facts['zoom_applied'] = actual.get('zoom_parameter_applied') is True
    # The pipeline only promotes full-scene timing to ImageMetadata; point
    # timing in data_api_metadata must never become an image-wide capture date.
    if metadata.get('acquisition_time'):
        facts['acquisition_time'] = _text(metadata['acquisition_time'], 60)
        facts['acquisition_time_basis'] = 'user_declared' if metadata.get('source') in {'user_upload', 'user_workbook_embedded'} else metadata.get('metadata_scope', 'unknown')
    else:
        facts['acquisition_time'] = None
    registration = actual.get('spatial_registration') or {}
    if registration.get('status') in {'verified', 'failed', 'unknown', 'not_verified', 'reference'}:
        facts['registration_status'] = registration['status']
    return {key: value for key, value in facts.items() if value is not None or key == 'acquisition_time'}


def activity_entries(record, *, assets=(), documents=(), sanitize=lambda value: value):
    """Return real work entries, retiring a start when its own action terminates.

    Distinct assets/scenes/phases are distinct actions. A completion for one
    image must not retire a second image's currently running model call.
    """
    result, actions = [], {}
    by_id = {asset['id']: asset for asset in assets if _identity(asset.get('id'))}

    def add(step, status, title, summary, when, *, event_index=None, asset_id=None, source=None, facts=None, action_key=None):
        entry = {'id': f'{step}-{event_index if event_index is not None else len(result)}',
                 'step': step, 'status': status, 'title': title,
                 'summary': sanitize(_text(summary, 1200)), 'created_at': when}
        if asset_id:
            entry['asset_id'] = asset_id
        if source in SOURCE_NAMES:
            entry['source'] = source
        if facts:
            entry['facts'] = {key: sanitize(value) if isinstance(value, str) else value for key, value in facts.items()}
        key = action_key or (step, asset_id, source)
        if status not in {'running', 'started', 'pending'}:
            previous = actions.pop(key, None)
            if previous is not None:
                result[previous]['status'] = 'completed' if status not in {'failed', 'unavailable', 'cancelled', 'interrupted'} else status
                result[previous]['summary'] = ('这项操作已返回结果，具体内容见后续记录。' if status not in {'failed','unavailable','cancelled','interrupted'}
                                                else '这项操作未完成，失败或停止情况见后续记录。')
                result[previous]['superseded_by'] = entry['id']
        else:
            previous = actions.get(key)
            if previous is not None:
                result[previous]['status'] = 'unknown'
                result[previous]['summary'] = '这次操作未记录完成结果，后续已另行发起请求；不能将其计为成功。'
                result[previous]['superseded_by'] = entry['id']
            actions[key] = len(result)
        result.append(entry)

    created = record.get('created_at')
    if record.get('location_basis'):
        basis = record['location_basis']
        name = _text(basis.get('name'))
        automatic = basis.get('match_method') == 'deepseek_amap_high_confidence'
        add('location_resolution', 'completed', '我找到了要查看的位置',
            f"{'我结合你的描述和高德候选匹配到' if automatic else '我已按你确认的区域定位'}{('：'+name) if name else ''}。这里是取图区域，具体疑点的位置还没有核实。", created)
    task = record.get('task') or {}
    for asset_id in task.get('asset_ids', [])[:8]:
        asset = by_id.get(asset_id)
        if not asset:
            continue
        meta, source = asset.get('metadata') or {}, (asset.get('metadata') or {}).get('source', 'user_upload')
        if source not in {'user_upload', 'user_workbook_embedded'}:
            continue
        facts = _asset_facts(asset)
        size = f"{facts['width']} × {facts['height']} 像素" if type(facts.get('width')) is int and type(facts.get('height')) is int else '显示影像'
        add('material_preparation', 'completed', '我读到了你上传的影像', f"我已读取 {_text(asset.get('name'), 180)}，大小为 {size}。", created, asset_id=asset_id, source=source, facts=facts)
    for document in documents[:12]:
        name, kind = _text(document.get('name'), 180), document.get('document_kind')
        summary = document.get('archive_summary') or {}
        if kind == 'zip':
            images = _count(summary.get('image_count'))
            unsupported = _count(summary.get('unsupported_file_count'))
            add('archive_inspection', 'completed', '我检查了压缩包里的材料', f"我已检查 {name}" + (f"，找到 {images} 张影像" if images is not None else '') + (f"、{unsupported} 个暂不支持的文件" if unsupported is not None else '') + '。我保留了原始数值栅格，只有选入任务的影像会用于判读。', created)
        elif kind == 'xlsx':
            count = _count((document.get('workbook_summary') or {}).get('record_count'))
            add('document_reading', 'completed', '我读到了表里的点位', f"我已读取 {name}" + (f"，找到 {count} 条点位记录" if count is not None else '') + '。这些是表内记录，尚不能视为已经核实的固废点位。', created)
        else:
            add('document_reading', 'completed', '我读到了你提供的资料', f'我已读取 {name} 中能够提取的文字，并保留了资料中的原始说法。它们与影像事实仍需分开看待。', created)
    if record.get('status') == 'planning':
        add('task_planning', 'running', '我先看看要核查什么', '我正在结合你的问题、已有材料和可用工具，确定需要核查的内容。', created)
    elif record.get('plan'):
        add('task_planning', 'completed', '我理清了要核查的内容', '我已整理你指定的位置、图源和资料范围，按这些条件安排操作。', created)

    for index, event in enumerate(record.get('events', [])[-200:]):
        if not isinstance(event, dict):
            continue
        step, status = event.get('step'), event.get('status')
        if not isinstance(step, str) or not isinstance(status, str):
            continue
        source = event.get('source') if event.get('source') in SOURCE_NAMES else None
        asset_id = _identity(event.get('asset_id'))
        asset = by_id.get(asset_id) or {}
        source = source or (asset.get('metadata') or {}).get('source')
        facts, title, summary = {}, '', ''
        if step in {'environmental_risk_initial','environmental_risk_review','environmental_risk_validation',
                    'contextual_plan','contextual_recognition','contextual_review','contextual_search','contextual_answer',
                    'contextual_place','contextual_events'}:
            active = status in {'running','started'}
            names = {'environmental_risk_initial':('我正在描述环境场景并作初步风险评估','我完成了场景与初步风险评估'),
                'environmental_risk_review':('我正在全面复核风险与标准适用性','我完成了环境风险的交叉复核'),
                'environmental_risk_validation':('我正在核对风险依据和标准对照','我核对了风险依据和标准对照'),
                'contextual_plan':('我先判断这次追问需要哪些证据','我确定了本轮问题所需的核对'),
                'contextual_recognition':('我正在查看本题涉及的影像细节','我完成了针对本题的细节核对'),
                'contextual_review':('我正在复核当前问题的场景与证据','我完成了本题的交叉复核'),
                'contextual_search':('我正在检索当前问题的适用资料','我保留了本题返回的资料与出处'),
                'contextual_place':('我正在查询点位所在区域和周边设施','我查到了点位所在区域和周边设施'),
                'contextual_events':('我正在检索该区域近期的相关事件','我保留了近期事件报道与出处'),
                'contextual_answer':('我正在组织针对本题的答复','我整理了这次追问的答复')}
            title = names[step][0 if active else 1]
            if status in {'failed','unavailable','not_configured'}: title = title.replace('我完成了','我未能完成').replace('我整理了','我未能整理')
            summary = _text(event.get('summary'), 1200) or ('我正在依据实际材料核对。' if active else '结果与证据限制已保留。')
        elif step == 'catalog':
            title = '我核对了能用哪些图源' if status == 'completed' else '我还没能核对全部图源'
            summary = '我已核对当前配置的图源和接口能力。' if status == 'completed' else '我没能核验部分图源的目录，已保留可用材料；这些缺口仍需单独核实。'
        elif step == 'coverage_plan':
            planned=_count(event.get('required_windows'))
            scope=event.get('scope') or {}
            scope=scope if isinstance(scope,dict) else {}
            zoom=event.get('zoom_decision') or {}
            zoom=zoom if isinstance(zoom,dict) else {}
            level=zoom.get('zoom')
            level=level if type(level) is int and 1<=level<=22 else None
            plan_status=event.get('plan_status') or status
            if planned is not None:facts['planned_windows']=planned
            if level is not None:facts['requested_zoom']=level
            pixels=event.get('window_pixels')
            if type(pixels) is int and pixels in {512,1024,2048}:facts['window_pixels']=pixels
            downloaded=_count(event.get('downloaded_window_count'))
            if downloaded is not None:facts['downloaded_windows']=downloaded
            if plan_status=='over_budget':
                title='我需要先调整这次查看的范围'
                summary=(f'按当前层级覆盖{_name(source)}需要 {planned} 个窗口，超过本次预算。' if planned is not None else
                         f'按当前层级覆盖{_name(source)}的窗口数量超过本次预算。')
                summary+='这一步没有发起取图，也没有降低层级或截断后冒充全范围覆盖；请缩小工作范围或明确提高预算。'
            elif plan_status in {'needs_scope_confirmation','needs_region_confirmation','needs_location'}:
                title='我还需要明确要查看的范围'
                summary='地名目前只提供参考位置，还不足以确定本次工作范围；我没有据此把行政中心当成举报点或开始全域取图。'
            elif plan_status in {'unsupported_requested_zoom','unavailable','failed','error'}:
                title='我还没能形成这份影像的覆盖计划'
                summary='我尚未形成可执行的范围与层级组合；这一计划没有发起取图，也不能作为已经覆盖或排除疑点的依据。'
            else:
                title='我划分了要查看的周边范围'
                summary=f'我已为{_name(source)}安排'+(f' {planned} 个窗口' if planned is not None else '所需查看窗口')
                if level is not None:
                    basis=zoom.get('selection_basis')
                    if basis=='user_requested_zoom':summary+=f'，按你指定的 {level} 级请求'
                    elif basis=='confirmed_complaint_window':summary+=f'，沿用已确认窗口的 {level} 级'
                    elif basis=='declared_native_maximum':summary+=f'，按来源声明的原生层级上限请求 {level} 级'
                    elif basis=='declared_provider_cap_native_unknown':summary+=f'，按图源声明的服务上限请求 {level} 级，原生最高层级尚未核实'
                    else:summary+=f'，暂用 {level} 级，最高支持层级尚未核实'
                if type(pixels) is int and pixels in {512,1024,2048}:summary+=f'，每窗 {pixels} × {pixels} 像素'
                summary+='。这里只完成了取图规划；实际收到影像、位置质量和逐窗判读以随后记录为准，不能把计划完成当成覆盖已核验。'
            if scope.get('basis') in {'bounded_geocoded_vicinity','user_requested_vicinity','administrative_reference_center'}:
                summary+='这只是围绕参考位置的工作范围，不是村界、行政边界或已经定位的倾倒点。'
        elif step == 'temporal_catalog':
            if status in {'running','started'}:
                title='我先查查这个位置有哪些历史日期'
                summary=f'我正在查询{_name(source)}的日期目录，还没有据此取得或分析这些日期的影像。'
            elif status in {'completed','succeeded'}:
                count=_count(event.get('date_count'))
                title='我没有查到本次条件下的历史日期' if count==0 else '我查到了历史日期'
                if count is not None:facts['catalog_dates']=count
                sampled=_dates(event.get('sample_dates'))
                nearest=_dates(event.get('nearest_available') or event.get('nearest_dates'))
                summary=(f'我从{_name(source)}本次目录中查到 {count} 个日期。' if count is not None else
                         f'我已读取{_name(source)}本次返回的历史日期目录。')
                target=event.get('target_time') or event.get('time_intent') or {}
                target=target if isinstance(target,dict) else {}
                start=_dates([target.get('requested_start')])
                end=_dates([target.get('requested_end')])
                if start or end:summary+='本次关注时段为 '+(start[0] if start else '未限定起点')+' 至 '+(end[0] if end else '未限定终点')+'。'
                if sampled:summary+='我找到的日期对照候选是 '+ '、'.join(sampled)+'；这里只是目录查询，实际取图与判读以各项执行记录为准。'
                if event.get('requested_period_catalog_gap') is True:
                    summary+='本次目录没有列出请求时段内的日期。'
                    if nearest:summary+='最近列出的候选日期为 '+nearest[0]+'，只可作为异时参考，不能冒充请求时段证据。'
                if event.get('catalog_complete') is not True:summary+='目录完整性尚未核实，不能据此断定其他时段没有影像。'
                summary+='图源版次和目录日期均不能代替实际下载影像的拍摄日期核验。'
            else:
                title='我暂时没能查清历史日期'
                summary=f'我本次对{_name(source)}的日期查询没有完整成功；这不等于该位置没有历史影像，也没有将失败查询当作已完成核查。'
        elif step == 'selection':
            title = '我选好了要查看的影像来源'
            ranking = event.get('ranking') or []
            chosen = [_name(row.get('source')) for row in ranking if isinstance(row, dict) and row.get('status') in {'selected', 'primary', 'eligible', 'ready'}][:2]
            summary = ('我选择优先查看' + '、'.join(dict.fromkeys(chosen)) + '，先回答你当前的问题。') if chosen else '我已按你的问题选择数据来源。'
            summary += '是否需要补充其他影像，要看实际覆盖和判读中是否仍有疑点。'
        elif step == 'acquisition':
            title = '我正在获取这一带的影像' if status in {'running', 'started'} else '我拿到了这一带的影像' if status == 'completed' else '我暂时没能取得这份影像'
            if status in {'running', 'started'}:
                summary = f'我正在请求{_name(source)}，影像和相关信息还在获取中。'
                if event.get('operation_stage')=='validation':
                    title='我收到了影像，正在检查能否判读'
                    summary=f'我已收到{_name(source)}的影像字节，正在核查格式、缺图提示和图像质量；这一步尚未开始模型判读。'
                requested = (event.get('view') or {}).get('zoom')
                if type(requested) is int and 0 <= requested <= 30:
                    facts['requested_zoom'] = requested
            elif status == 'completed':
                facts = _asset_facts(asset)
                label = facts.get('source_label')
                summary = f'我已取得{_name(source)}'
                if label:
                    summary += f'，图源名称为“{label}”'
                if 'zoom' in facts:
                    summary += f"，取图级别 {facts['zoom']}"
                elif 'requested_zoom' in facts:
                    summary += f"，请求级别 {facts['requested_zoom']}（实际输出按提供商服务处理）"
                if type(facts.get('width')) is int and type(facts.get('height')) is int:
                    summary += f"，{facts['width']} × {facts['height']} 像素"
                summary += '。'
                summary += '我还没有核实整幅影像的实际拍摄日期，图源的更新标识不能代替拍摄日期。' if not facts.get('acquisition_time') else f"我读到的拍摄日期记录为 {facts['acquisition_time'][:10]}，它的适用范围仍以原始元数据为准。"
            else:
                code, reason = _acquisition_failure(event)
                if code: facts['error_code'] = code
                summary = f'我这次没能从{_name(source)}取得可分析的影像'+('：'+reason if reason else '，已保留失败记录')+'。这不能说明该来源在这里没有覆盖，也不能据此判断没有固废。'
            elapsed=event.get('elapsed_seconds')
            if isinstance(elapsed,(float,int)) and 0<=elapsed<86400:facts['elapsed_seconds']=elapsed
        elif step == 'image_crop':
            title = '我截取了这处局部，继续核对细节'
            width, height = _count(event.get('width')), _count(event.get('height'))
            summary = '我已从原图提取这个候选的局部像素'
            reference=event.get('candidate_reference')
            if isinstance(reference,str) and re.fullmatch(r'E\d+(?:、E\d+)*',reference):
                facts['candidate_reference']=reference
                summary=f'我已从原图提取候选 {reference} 的局部像素'
            if width and height:
                summary += f'，大小为 {width} × {height} 像素'
                facts.update(width=width, height=height)
            summary += '，局部图已保留在右侧影像栏，接下来交给见微重读并复核。裁图只提取已有细节，不增加原生分辨率、独立来源或定位精度。'
        elif step == 'quality':
            title, summary = '我检查了影像是否看得清、对得上', '我已检查影像的分辨条件、成像质量和位置对应。还不满足条件的影像，我只作为背景参考或保留待核实。'
        elif step == 'image_recognition':
            title = '我正在逐图查看可疑线索' if status in {'running', 'started'} else '我看到了这些初步线索'
            if status == 'negative':
                title = '我没有看到明确的可疑堆体'
            elif status == 'unknown':
                title = '我还不能确定这张图里的情况'
            elif status == 'background_only':
                title = '我先把这张图作为背景参考'
            facts = {key: event[key] for key in ('candidate_count', 'raw_candidate_count') if _count(event.get(key)) is not None}
            if status not in {'running', 'started'}:
                facts['decision'] = status if status in {'positive', 'negative', 'unknown', 'background_only'} else 'unknown'
            summary = _observation(event.get('summary'),prefix='我初步看到：')
            if not summary:
                summary = {'running': '我正在逐图查看堆体的形态、纹理和周边关系，同时检查有没有其他合理解释。', 'started': '我正在逐图查看堆体的形态、纹理和周边关系，同时检查有没有其他合理解释。', 'positive': '我初步看到了疑似堆存线索；它是否可信，还需要结合具体场景和复核证据判断。', 'negative': '我在这张图中没有发现明确的疑似堆体，但这个判断仍受影像质量和位置核验情况限制。', 'background_only': '我目前只能把这张图作为背景参考，还不能据此判断目标点位。'}.get(status, '我还不能从这张图中形成可靠判断，先保留待核实。')
        elif step == 'image_assessment':
            title = '我把这张图的线索重新核对了一遍'
            if status in {'unknown','background_only'}:
                title = '我还不能确认目标点位的情况'
            summary = _observation(event.get('summary')) or '我已把初判线索、可能的误报和位置对应放在一起核对，仍有疑问的地方保留待核实。'
            facts['decision'] = status if status in {'positive','negative','unknown','background_only'} else 'unknown'
            if _count(event.get('candidate_count')) is not None:
                facts['candidate_count'] = event['candidate_count']
        elif step == 'optical_false_positive_review':
            title = '我再确认一下会不会看错' if status in {'running', 'started'} else '我复核了这些可疑线索'
            if status in {'running','started'}:
                summary = '我正在对照同一幅影像，检查它是否其实是建筑、硬化地面或裸土，也检查标注有没有框偏。'
            elif status in {'failed','unavailable','unknown'}:
                title = '我还没能可靠完成这次复核'
                summary = _observation(event.get('summary'),prefix='我这次复核仍有不确定性：') or '我这次还没能可靠完成复核，已有疑点仍保留待核实，不能按没有固废处理。'
            else:
                summary = _observation(event.get('summary')) or '我已把能够明确排除的误报和仍有疑问的线索分开保留。'
            for key in ('excluded_candidate_count', 'uncertain_candidate_count'):
                if _count(event.get(key)) is not None:
                    facts[key] = event[key]
            if isinstance(event.get('model'), str):
                facts['model'] = _text(event['model'], 100)
        elif step in {'science_query', 'science_quality_probe', 'science_subset', 'research_optical_context'}:
            active = status in {'running','started'}
            title = {'science_query': '我先找找这一带的影像' if active else '我查到了这一带的影像记录',
                     'science_quality_probe': '我先看看这景影像能不能用' if active else '我核对了这景影像的可用性',
                     'science_subset': '我正在提取需要查看的区域' if active else '我取得了需要查看的影像区域',
                     'research_optical_context': '我再看看这里的高分影像' if active else '我补充查看了这里的高分影像'}[step]
            count = _count(event.get('scene_count'))
            if step == 'science_query' and status == 'completed':
                if count == 0:
                    title = '我没有查到符合条件的影像记录'
                elif count is None:
                    title = '我查了这一带的影像记录'
                summary = f'我从{_name(source)}查到 {count} 景候选影像记录。' if count is not None else f'我已查询{_name(source)}的影像记录。'
                summary += '这些记录是否能用于分析，还要看下载权限、目标区域的覆盖和有效像元；查到记录不等于已经拿到或分析了影像。' if count != 0 else '这个查询结果只针对指定区域、时段和检索条件，不能据此断定该来源没有覆盖。'
            elif status in {'running', 'started'}:
                summary = {'science_query': f'我正在从{_name(source)}查找与你指定区域和时段匹配的影像记录。',
                           'science_quality_probe': f'我正在检查{_name(source)}这景影像中，目标区域有没有云、阴影或无效像元。',
                           'science_subset': f'我正在从{_name(source)}提取目标区域，确认是否能拿到实际影像数据。',
                           'research_optical_context': f'我正在获取{_name(source)}，用来查看这里的地表形态和周边场景。'}[step]
            elif status in {'failed', 'unavailable'}:
                title = '我这次没能取得可用证据'
                summary = f'我这次对{_name(source)}的查询或处理没有形成可用证据；这不代表没有影像覆盖，也不能说明没有固废。'
            elif status == 'unknown':
                title = '我还不能确认这份影像是否可用'
                summary = f'我还不能确认{_name(source)}这份记录或影像的可用性，先保留待核实；不能因此认定没有覆盖或没有固废。'
            else:
                summary = f'我已完成对{_name(source)}的这项检查，是否适合判读仍以实际有效像元、定标和质量记录为准。'
            if count is not None:
                facts['scene_count'] = count
            if step == 'science_quality_probe':
                quality = event.get('quality_status')
                if quality in {'screened_usable','obscured_or_unusable','unknown'}:
                    facts['quality_status'] = quality
                if quality == 'screened_usable':
                    summary = f'我检查了{_name(source)}这景影像的 SCL 质量信息，目标区域的地表质量筛查通过。所需波段是否获取成功，需要看实际下载记录。'
                elif quality == 'obscured_or_unusable':
                    title = '我发现这景影像看不清目标'
                    summary = f'我发现{_name(source)}这景影像的目标区域受到云、阴影或无效像元影响，没有把它作为有效地表证据。'
            scene = event.get('scene_id')
            if isinstance(scene,str) and len(scene)<=160 and re.fullmatch(r'(?:S[12][ABC]_[A-Za-z0-9_.-]+|L[CEOT][0-9]{2}_[A-Za-z0-9_.-]+|scene-[A-Za-z0-9_-]+)',scene):
                facts['scene_id'] = scene
        elif step == 'science_analysis':
            title = '我正在查看影像里的观测证据' if status in {'running','started'} else '我核对了影像里的观测证据'
            summary = (f'我正在分析{_name(source)}的实际影像区域，并核对波段、定标和可观测条件。'
                       if status in {'running','started'} else _observation(event.get('summary'))
                       or f'我已核对{_name(source)}的观测证据，判断范围仍受有效像元、分辨率和位置对应的限制。')
            if status in {'failed','unavailable','unknown'}:
                title = '我还没能形成可靠的影像判断'
                summary = _observation(event.get('summary'),prefix='我这次分析仍有不确定性：') or f'我还没能从{_name(source)}取得可靠的分析结论，保留待核实，不能按没有固废处理。'
        elif step == 'adaptive_review':
            active = status in {'running','started','planned'}
            title = {'local_crop': '我再放大看看这个疑点' if active else '我放大查看了这个疑点',
                     'same_coverage_alternate': '我换一份影像对照看看' if active else '我用另一份影像作了对照',
                     'scientific_evidence': '我再找一份能够补充判断的影像' if active else '我查看了补充的影像证据'}.get(event.get('action'), '我再核对一下这个疑点' if active else '我进一步核对了这个疑点')
            summary = _observation(event.get('outcome'),prefix='我进一步核查后发现：',maximum=700) or ('我正在获取能帮助核实当前疑点的补充材料。' if active else '我已保留这次补充核查实际取得的材料，仍没有解决的疑点继续待核实。')
            if status in {'failed','unavailable'}:
                title = '我这次没能补齐需要的证据'
        elif step == 'visual_cross_review':
            active=status in {'running','started'}
            title='我再核对几份影像的分歧' if active else '我完成了跨图补充核对'
            summary=_observation(event.get('summary')) or '我在核对不同影像的目标对应与分歧，逐图尚未排除的候选仍然保留。'
            if active:
                limit=event.get('timeout_seconds')
                if isinstance(limit,(float,int)) and 0<limit<=180:
                    facts['timeout_seconds']=limit
                    summary+=f'这项补充核对最多等待 {int(limit)} 秒；若未返回有效结果，我会保留已有证据继续处理。'
            if status in {'failed','unavailable'}:title='我先保留已有判读，继续核查疑点'
            elapsed=event.get('elapsed_seconds')
            if isinstance(elapsed,(float,int)) and 0<=elapsed<86400:facts['elapsed_seconds']=elapsed
        elif step == 'spatial_registration_refresh':
            active=status in {'running','started'}
            title='我正在核对几份影像是否对得上' if active else '我重新核对了几份影像是否对得上'
            summary=('我正在核对已有影像的位置对应，已有图内疑点会保留。' if active else
                     '我已根据实际配准结果重新检查已有影像。几份图相互对齐，仍不代表绝对位置或测量精度已经核实。')
        elif step == 'policy':
            if status in {'disabled_by_user', 'disabled', 'skipped'}:
                continue
            title = '我找到了相关的公开资料' if status in {'completed','succeeded'} else '我还没能确认相关的公开资料'
            summary = '我已检索并保留相关来源，资料中的说法、依据的有效性和影像事实会分开表述。' if status in {'completed','succeeded'} else '我还没能完成或核实部分资料检索，尚未验证的来源不能作为确定依据。'
        elif step in {'report_synthesis', 'synthesis'}:
            if step == 'synthesis' and status in {'running', 'started'}:
                title, summary = '我正在整理哪些判断有依据', '我正在把有效疑点、已排除的误报和相互矛盾的证据分开，梳理还需要核查什么。'
            elif step == 'report_synthesis' or status in {'completed', 'succeeded'}:
                title, summary = '我把能确认和不能确认的地方整理好了', '我已依据实际影像和工具记录整理判断，详细来源、标注及尚不确定的地方都保留在分析材料中。'
        elif step == 'report_export':
            title = '我把报告整理成了可下载文件' if status in {'completed','succeeded'} else '我正在整理可下载的报告'
            artifact=event.get('artifact')
            if status in {'completed','succeeded'} and artifact in {'report.docx','report.html','report.md'}:
                facts['artifact'] = artifact
                summary = f'我已生成 {artifact}，你可以在成果中下载。影像、标注和证据的适用范围都随报告保留。'
            elif status in {'running','started'}:
                summary = '我正在把已有的分析内容整理成可下载报告。'
            else:
                title = '我暂时没能导出这份报告'
                summary = '我这次没能完成文件导出，已有分析和其他可用成果仍然保留。'
        elif step == 'user_cancelled':
            title, summary = '我已经停下来了', '我已停止后续操作，保留了取得的影像和工作记录。你可以修改要求后继续。'
        # Internal four-phase markers and arbitrary provider payloads are not UI
        # progress. Unknown steps can gain a reviewed public template later.
        if not title:
            continue
        key = (step, asset_id, source, event.get('phase'), event.get('scene_id'), event.get('action'))
        if step == 'acquisition':
            view = event.get('view') or {}
            key = (step,source,event.get('phase'),event.get('role'),str(view.get('bbox')))
        add(step, status, title, summary, event.get('time'), event_index=index,
            asset_id=asset_id, source=source, facts=facts, action_key=key)
    terminal = record.get('status') not in {'planning', 'queued', 'running'}
    if terminal:
        for index in actions.values():
            result[index]['status'] = 'cancelled' if record.get('status') in {'cancelled', 'interrupted'} else 'unknown'
            result[index]['title'] = '我还没有核实这项操作的完成结果'
            result[index]['summary'] = ('这项操作已随任务停止，未记录完成结果，不能计为已获取或已分析。'
                                        if record.get('status') in {'cancelled','interrupted'} else
                                        '任务已经结束，但这项操作未记录完成结果，不能计为已获取或已分析；需要核对来源记录。')
    return result[-100:]
