"""Readable regulatory reports with evidence first and a folded audit appendix."""
from __future__ import annotations
import html
import json
import re
import shutil
from urllib.parse import urlsplit
from PIL import Image, ImageDraw
from .schemas import TASK_LABELS, TaskKind
from .store import Store
from .recommendations import normalize_recommendations
from .consumer_conclusion import package_consumer_conclusion
from .task_outputs import presentation_contract
from .answer_layout import public_review_text, readable_blocks


def _presentation_html(contract, esc):
    pieces, markdown = [], []
    for section in contract['sections']:
        pieces.append('<h2>' + esc(section['heading']) + '</h2>')
        markdown.extend(['## ' + section['heading'], ''])
        for value in section['paragraphs']:
            for block in readable_blocks(public_review_text(value)).split('\n\n'):
                pieces.append('<p>' + esc(block).replace('\n', '<br>') + '</p>')
            markdown.extend([readable_blocks(public_review_text(value)), ''])
        if section['columns'] and section['records']:
            pieces.append('<div class="table-wrap"><table><thead><tr>' + ''.join('<th>'+esc(v)+'</th>' for v in section['columns']) + '</tr></thead><tbody>')
            markdown.extend(['| ' + ' | '.join(section['columns']) + ' |', '| ' + ' | '.join('---' for _ in section['columns']) + ' |'])
            for record in section['records']:
                pieces.append('<tr>' + ''.join('<td>'+esc(v)+'</td>' for v in record) + '</tr>')
                markdown.append('| ' + ' | '.join(str(v).replace('|','／') for v in record) + ' |')
            pieces.append('</tbody></table></div>'); markdown.append('')
    return pieces, markdown


def _contextual_report(package, folder, contract):
    """A follow-up answer is a direct response, not an empty inspection report."""
    esc = lambda value: html.escape(str(value or ''))
    pieces = ['<h1>'+esc(contract['report_title'])+'</h1>', '<p>'+esc(package.get('task',{}).get('objective'))+'</p>',
              '<h2>'+esc(contract['heading'])+'</h2>', '<p>'+esc(contract['lead']).replace('\n','<br>')+'</p>']
    lines = ['# '+contract['report_title'], '', package.get('task',{}).get('objective',''), '', '## '+contract['heading'], '', contract['lead'], '']
    content, markdown = _presentation_html(contract, esc); pieces.extend(content); lines.extend(markdown)
    sources = (package.get('policy') or {}).get('sources') or []
    if sources:
        pieces.append('<h2>本轮引用来源</h2>'); lines.extend(['## 本轮引用来源',''])
    for source in sources:
        url = str(source.get('url') or '')
        if urlsplit(url).scheme not in ('http', 'https'): continue
        title = str(source.get('title') or '参考资料')
        pieces.append('<p><a rel="noreferrer" href="'+html.escape(url,quote=True)+'">'+esc(title)+'</a></p>')
        lines.extend(['['+title+']('+url+')',''])
    style = "body{font:17px/1.8 system-ui,'Microsoft YaHei',sans-serif;max-width:960px;margin:40px auto;padding:0 24px;color:#202124;background:#fff}h1{font-size:27px}h2{font-size:21px;margin-top:30px}p,td{overflow-wrap:anywhere}table{border-collapse:collapse;width:100%}th,td{padding:10px;border:1px solid #ddd;text-align:left}.table-wrap{overflow:auto}"
    (folder/'report.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(contract['report_title'])+'</title><style>'+style+'</style><body>'+''.join(pieces)+'</body></html>', encoding='utf-8')
    (folder/'report.md').write_text('\n'.join(lines), encoding='utf-8')

SOURCE_NAMES = {'arcgis':'Esri 世界影像','bing':'Bing 影像','amap':'高德卫星底图','tencent':'腾讯卫星底图','google':'Google / GG 卫星底图','mapquest':'MapQuest 影像','sentinel1-rtc':'Sentinel-1 雷达','sentinel1-grd':'Sentinel-1 雷达','sentinel1-slc':'Sentinel-1 雷达','sentinel2':'Sentinel-2 多光谱','landsat':'Landsat 多光谱','emit':'EMIT 高光谱','enmap':'EnMAP 高光谱','user_upload':'用户上传','user_workbook_embedded':'用户工作簿影像'}
ROLE_NAMES = {'current':'当前影像','baseline':'基准影像','target_crop':'目标局部','local_context':'周边环境','full_scene':'区域全景','sar':'雷达影像','false_color':'假彩色影像'}
SOURCE_NAMES.update(gaofen2026='2026高清图源', century='世纪空间年度影像', jilin2024='吉林一号2024版',
                    siwei2025='四维地球2025版', history246='谷歌历史影像246', tianditu='天地图影像')


def _public_text(value, figures: dict[str,str]) -> str:
    if isinstance(value, list):
        value = '；'.join(_public_text(item, figures) for item in value)
    text = re.sub(r'<think(?:ing)?\b[^>]*>[\s\S]*?(?:</think(?:ing)?\s*>|$)', '', str(value or ''), flags=re.I)
    for identity,label in figures.items():
        text = text.replace(identity,label)
        text = re.sub(r'(?<![0-9a-f])'+re.escape(identity[:8])+r'(?![0-9a-f])',label,text)
    text = re.sub(r'；?像素框由整图\s*(?:normalized_1000\s*)?坐标换算[。.]?', '。', text, flags=re.I)
    text = re.sub(r'原生模型标注候选\s*R\d+[，,]\s*模型类别为', '标注区域的初筛候选类别为', text)
    text = re.sub(r'(?:像素(?:窗|窗口)|归一化(?:框|坐标)|候选框|框)?\s*\[\s*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*\]', '', text)
    replacements = {'任务数据合同要求':'根据任务证据需求选择','按已核验能力与可见特征适配排序':'结合可用性与目标细节确定顺序','原生模型最终判读（待核验）：':'影像初筛线索（待核查）：','esri_mosaic':'ArcGIS 影像来源组','bing_mosaic':'Bing 影像来源组','conflict_present=false':'未记录跨图正负判断分歧','conflict_present=true':'存在跨图判断分歧','substantive_visual_review_supported=false':'视觉交叉复核尚未形成同一目标的独立佐证','same_target_supported=false':'同一目标对应关系尚未确认','can_override_positive_candidate=false':'正向疑点予以保留','independent_evidence=false':'不构成独立佐证','full_task_aoi_covered=false':'尚未覆盖全部任务区域'}
    for key,label in replacements.items():
        text = text.replace(key,label)
    modalities={'optical':'光学影像','sar':'雷达辅助影像','multispectral':'多光谱影像','hyperspectral':'高光谱影像','auto':'自动选源'}
    text=re.sub(r'(?<![A-Za-z0-9_])(optical|sar|multispectral|hyperspectral|auto)(?![A-Za-z0-9_])',lambda match:modalities[match.group().lower()],text,flags=re.I)
    return public_review_text(text.strip())


def _brief_review(text: str, limit: int = 350) -> str:
    if len(text)<=limit:
        return text
    paragraph=text.split('\n\n',1)[0]
    budget=limit-18
    if len(paragraph)<=budget:
        return paragraph+'（完整复核见文末记录。）'
    sentences=re.findall(r'[^。！？]+[。！？]|[^。！？]+$',paragraph)
    result=''
    for sentence in sentences:
        if len(result)+len(sentence)>budget:
            break
        result+=sentence
    return (result or '专业复核完整意见见文末记录。')+'（完整复核见文末记录。）'


def _science_summary(item: dict) -> str:
    subset = item.get('subset') or {}
    if subset.get('status') == 'science_window_downloaded':
        return '已下载科学数据窗口；像素分析情况见本次分析记录。' if item.get('science_pixels_analyzed') else '已下载并解码核验科学数据窗口；尚未开展定量像素分析。'
    if subset.get('status') == 'unavailable':
        return '已检索到候选场景，本次科学数据窗口未能取得或读取。'
    if item.get('status') == 'catalog_only':
        return '已检索到候选场景，尚未取得可分析的数据窗口。'
    return {'analyzed':'已取得科学影像分析结果。','downloaded_unanalyzed':'已取得窗口，科学分析尚未完成。',
        'catalog_found_no_usable_window':f"目录有 {item.get('scene_count',0)} 景，本次未取得可分析窗口。",
        'query_unavailable':'目录请求未完整成功，不能解释为该时段没有数据。','unsupported_source':'当前服务未配置该科学图源。',
        'unavailable':'本次未取得可用科学数据。','failed':'本次科学数据获取未完成。','no_results':'本次检索未返回候选场景。','not_queried':'本次未执行科学数据检索。'}.get(item.get('status'),'科学数据成果以本次实际下载和分析记录为准。')


def _science_read_summary(item):
    probes=item.get('quality_probes') or []
    attempts=item.get('download_attempts') or []
    counts=[]
    if probes:counts.append(f'检查 {len(probes)} 景点位质量')
    failed=sum(row.get('status')=='read_failed' for row in attempts if isinstance(row,dict))
    rejected=sum(row.get('status')=='quality_rejected' for row in attempts if isinstance(row,dict))
    if failed:counts.append(f'读取失败 {failed} 次')
    if rejected:counts.append(f'云影或无效像元拦截 {rejected} 景')
    if item.get('download_status')=='provider_authorization_required':counts.append('科学文件需要提供方授权')
    elif item.get('download_status')=='native_window_adapter_not_available':counts.append('尚未接通该图源科学窗口读取')
    return '；'.join(counts)


def _artifact_link(value: str) -> str:
    return value if value and '/' not in value and '\\' not in value and value not in ('.','..') and not urlsplit(value).scheme else ''


def render_report(package: dict, store: Store, run_id: str, *, tolerate_missing_saved_images: bool = False) -> None:
    folder = store.run_dir(run_id)
    (folder/'finding-package.json').write_text(json.dumps(package,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'annotations.geojson').write_text(json.dumps({'type':'FeatureCollection','features':[],'wuyu_status':'not_georeferenced','reason':'像素标注保存在 finding-package.json；未经地理变换验证不输出地图对象'},ensure_ascii=False,indent=2),encoding='utf-8')
    presentation = presentation_contract(package)
    if presentation['enabled'] and presentation['workflow_mode'] == 'contextual_qa':
        _contextual_report(package, folder, presentation)
        return
    assets=package.get('assets',[])
    figures={asset['id']:f'图{index+1}' for index,asset in enumerate(assets)}
    public=lambda value:_public_text(value,figures)
    esc=lambda value:html.escape(public(value))
    lines,sections=[],[]
    def paragraph(text):
        if text:
            clean=readable_blocks(public(text))
            lines.extend([clean,''])
            sections.extend('<p>'+html.escape(block).replace('\n','<br>')+'</p>' for block in clean.split('\n\n'))
    def heading(title):
        lines.extend(['## '+title,''])
        sections.append('<h2>'+html.escape(title)+'</h2>')
    def bullets(values):
        values=list(dict.fromkeys(clean for value in values if value and (clean:=public(value))))
        if values:
            sections.append('<ul>')
            for value in values:
                lines.append('- '+value)
                sections.append('<li>'+html.escape(value)+'</li>')
            lines.append('');sections.append('</ul>')
    task=package.get('task',{})
    information_only=bool(task.get('search_only'))
    fusion=package.get('fusion') or {}
    research=package.get('research_result') or {}
    is_research=bool(research) or fusion.get('screening_applicable') is False or fusion.get('level')=='not_applicable'
    if presentation['enabled']:
        information_only, is_research = False, False
    risk_mode = presentation['enabled'] and presentation['workflow_mode'] == 'environmental_risk'
    recognition=package.get('recognition') or {}
    narrative=package.get('narrative') or {}
    observations=recognition.get('observations',[])
    candidates=recognition.get('candidates',[])
    from .task_results import task_report_title
    title='无隅见微 · 联网资料问答' if information_only else '无隅见微 · 卫星影像研究报告' if is_research else task_report_title(task.get('task_types', []))
    if presentation['enabled']: title = presentation['report_title']
    lines.extend(['# '+title,'','任务：'+task.get('objective',''),''])
    sections.extend(["<header class='report-header'><span class='eyebrow'>WUYU · REMOTE SENSING</span><h1>"+title+'</h1>','<p>'+esc(task.get('objective',''))+'</p></header>'])
    task_rows=(package.get('task_results') or {}).get('results', [])
    # Match the workbench's projection, including pre-upgrade evidence packages
    # that do not yet have a persisted concise conclusion.
    consumer=package_consumer_conclusion(package)
    consumer_text=public(consumer.get('text','')) if isinstance(consumer,dict) and not is_research and not information_only else ''
    primary_task=task_rows[0] if len(task_rows)==1 and task_rows[0].get('task_type')!='suspected_site_discovery' else None
    heading(presentation['heading'] if presentation['enabled'] else '回答' if information_only else '研究结果' if is_research else primary_task['heading'] if primary_task and not consumer_text else '本次核查结论')
    judgment=fusion.get('judgment') or {'needs_configuration':'专业识别尚未完成，本次无法形成判读结论','needs_more_data':'现有证据不足，需要补充资料','no_reviewable_finding':'本次未形成可审查疑点'}.get(package.get('status'),'请结合下列证据与复核意见判断')
    if task_rows and task_rows[0].get('task_type') != 'suspected_site_discovery':
        judgment='。'.join(str(row.get('conclusion','')).rstrip('。') for row in task_rows[:2] if row.get('conclusion'))+'。'
    if not is_research and consumer_text:
        sections.append("<section class='conclusion'><p><strong>"+esc(consumer_text)+'</strong></p></section>')
        lines.extend(['**'+consumer_text+'**',''])
    elif not is_research:
        sections.append("<section class='conclusion'><h3>"+esc(judgment)+'</h3>')
        lines.extend(['**'+public(judgment)+'**',''])
        paragraph(fusion.get('basis'))
    if not is_research and not consumer_text and fusion.get('conflict_present'):
        paragraph('不同影像的判断存在分歧，正向疑点予以保留，需结合现场或追加证据核查。')
    if not is_research and not consumer_text and fusion and not fusion.get('task_evidence_sufficient',True):
        paragraph('当前证据尚不足以完整回答任务要求。'+('本次影像不能替代所需的历史变化证据。' if fusion.get('temporal_evidence',{}).get('required') else ''))
    if not is_research and not consumer_text:sections.append('</section>')
    summary=public(narrative.get('summary',''))
    if summary and not consumer_text:
        concise=summary if is_research or len(summary)<=900 else summary.split('\n\n',1)[0]
        if not is_research and len(concise)>900:
            concise=concise[:900].rsplit('。',1)[0]+'。完整分析见文末记录。'
        paragraph(concise)
    if presentation['enabled']:
        specific, specific_md = _presentation_html(presentation, esc)
        sections.extend(specific); lines.extend(specific_md)
    if not is_research and not information_only and not presentation['enabled']:
        for result in (package.get('task_results') or {}).get('results', []):
            if result is primary_task and not consumer_text: continue
            heading(result['heading'])
            paragraph(result.get('conclusion'))
            if result.get('alternatives'):bullets('需排除：'+value for value in result['alternatives'])
        timeline=(package.get('task_results') or {}).get('timeline', [])
        if timeline and any(row.get('timeline') is not None for row in (package.get('task_results') or {}).get('results', [])):
            heading('已取得时相与覆盖缺口')
            sections.append('<table><thead><tr><th>图号及来源</th><th>实际拍摄日期</th><th>提供方日期或版次</th><th>逐图状态</th></tr></thead><tbody>')
            lines.extend(['| 图号及来源 | 实际拍摄日期 | 提供方日期或版次 | 逐图状态 |','|---|---|---|---|'])
            for row in timeline:
                dates='、'.join(row.get('reported_actual_dates') or [])
                scope={'whole_scene':'整幅日期','full_scene':'整幅日期','displayed_view_date':'界面显示日期，适用范围待核实'}.get(row.get('date_scope'),'日期适用范围待核实')
                label=(dates+'（'+scope+'）') if dates else (str(row['product_year'])+'版；不是拍摄日期') if row.get('product_year') else row.get('period_label') or '未核实'
                cells=[row.get('figure','')+' '+SOURCE_NAMES.get(row.get('source'),row.get('source') or ''),row.get('actual_capture_date') or '未知',label,
                    {'positive':'保留疑点','negative':'本图未检出','unknown':'暂不可判'}.get(row.get('decision'),'暂不可判')]
                sections.append('<tr>'+''.join('<td>'+esc(value)+'</td>' for value in cells)+'</tr>')
                lines.append('| '+' | '.join(public(value).replace('|','／') for value in cells)+' |')
            sections.append('</tbody></table>');lines.append('')
    recommendations=normalize_recommendations(narrative.get('recommendations'))
    if not risk_mode and (not is_research or recommendations):heading('后续分析建议' if is_research else '核查与处置建议')
    if recommendations and not risk_mode:
        bullets(recommendations)
    elif not is_research and not risk_mode:
        paragraph('本次未形成具体处置建议，请先补充任务所需的可用证据。')
    heading({'environmental_risk':'环境风险场景证据','complaint_investigation':'举报核查影像证据','temporal_change':'逐期影像证据'}.get(presentation['workflow_mode'], '数据记录与观测' if is_research else '影像证据与疑点') if presentation['enabled'] else '数据记录与观测' if is_research else '影像证据与疑点')
    ledger=package.get('evidence_summary',[])
    if ledger:
        sections.append('<table><thead><tr><th>图源与用途</th><th>拍摄时间</th><th>参数</th><th>位置核验</th><th>观测摘要</th></tr></thead><tbody>')
        lines.extend(['| 图源与用途 | 拍摄时间 | 参数 | 位置核验 | 观测摘要 |','|---|---|---|---|---|'])
        for row in ledger:
            params=row.get('parameters') or {}
            bits=[]
            if row.get('native_resolution_m'):bits.append(str(row['native_resolution_m'])+'m 原生')
            if params.get('bands'):bits.append('波段 '+','.join(params['bands']))
            if params.get('polarization'):bits.append('极化 '+str(params['polarization']))
            source=SOURCE_NAMES.get(row.get('source'),row.get('source','未知图源'))
            spatial={'verified':'空间对应已核验','corrected':'已执行相对位置校正','unknown':'空间对应未知','unverified':'空间对应未核验','misaligned':'位置存在偏差','failed':'空间核验未通过','user_supplied':'用户提供，位置待核实'}.get(row.get('spatial_status'),row.get('spatial_status') or '空间对应未核验')
            if row.get('same_point_eligible') is False:spatial+='；仅供背景参考，不用于同点目标判断'
            cells=[f"{row.get('figure','')} {source} · {row.get('role','')}",row.get('acquisition_time') or '未知（获取时间不等于拍摄时间）','；'.join(bits) or '原生参数未核实',spatial,row.get('observation','')]
            sections.append('<tr>'+''.join('<td>'+esc(cell)+'</td>' for cell in cells)+'</tr>')
            lines.append('| '+' | '.join(public(cell).replace('|','／').replace('\n',' ') for cell in cells)+' |')
        sections.append('</tbody></table>');lines.append('')
    assessments={row['asset_id']:row for row in package.get('image_assessments',[])}
    technical_assets=[]
    for index,asset in enumerate(assets,1):
        try:
            raw=store.get(asset['id'],'image')
        except (KeyError, ValueError, FileNotFoundError):
            if not tolerate_missing_saved_images:
                raise
            # Read-only report refreshes must not reach into another asset
            # store to replace missing saved attachments.
            caption=f"图{index} · {SOURCE_NAMES.get(asset.get('metadata',{}).get('source'),'已记录影像')}"
            sections.append("<figure><figcaption><strong>"+esc(caption)+"</strong></figcaption>")
            lines.extend(['### '+caption,''])
            paragraph('已保存的原图附件缺失，本次仅保留历史判读记录，未重新取图或重新识别。')
            for obs_index,obs in enumerate(observations):
                if obs.get('asset_id')==asset['id']:
                    paragraph(f'E{obs_index+1}（图{index}）：'+obs.get('statement',''))
            sections.append('</figure>')
            technical_assets.append({'figure':f'图{index}','id':asset['id'],'attachment_status':'missing_saved_image'})
            continue
        source_name='source-'+raw['file']
        shutil.copyfile(store.asset_path(raw),folder/source_name)
        marked='annotated-'+asset['id']+'.png'
        with Image.open(store.asset_path(raw)) as source:
            output=source.convert('RGB');draw=ImageDraw.Draw(output);box_count=0
            for obs_index,obs in enumerate(observations):
                if obs.get('asset_id')==asset['id'] and obs.get('pixel_window'):
                    x1,y1,x2,y2=obs['pixel_window']
                    draw.rectangle((x1,y1,x2-1,y2-1),outline='#ffc943',width=3)
                    draw.text((x1+3,y1+3),f'E{obs_index+1}',fill='#ffc943',stroke_width=1,stroke_fill='black')
                    box_count+=1
            output.save(folder/marked)
        meta=asset.get('metadata',{})
        source_label=SOURCE_NAMES.get(meta.get('source'),meta.get('source','来源未注明'))
        role='疑点局部复核' if asset.get('derived_from_asset_id') else ROLE_NAMES.get(meta.get('role'),'遥感影像')
        caption=f'图{index} · {source_label} · {role}'
        note='黄色框与 E 编号对应下方观测线索，待人工核查。' if box_count else '本图未绘制目标框，保留原图供复核。'
        metadata=f"拍摄时间 {meta.get('acquisition_time') or '未知'} · 分辨率 {str(meta['resolution_m'])+' 米' if meta.get('resolution_m') else '未知'}"
        if asset.get('derived_from_asset_id'):
            note+=' 来源于'+figures.get(asset['derived_from_asset_id'],'原始影像')+'的局部裁切，不增加独立来源数。'
        if any(row.get('asset_id')==asset['id'] and row.get('same_point_eligible') is False for row in ledger):
            note+=' 本图位置关系尚不支持同点目标判断，仅供背景参考。'
        optical_review=(assessments.get(asset['id'],{}) or {}).get('optical_false_positive_review') or {}
        if optical_review.get('status')=='completed':
            note+=' 标注采用同图复核后的有效候选；原始见微输出单独保存在完整证据记录中。'
        lines.extend(['### '+caption,'',metadata,'',f'![{caption}]({marked})','',note,''])
        sections.append(f"<figure id='figure-{index}'><figcaption><strong>{esc(caption)}</strong><span>{esc(metadata)}</span></figcaption><a href='{html.escape(marked,quote=True)}'><img src='{html.escape(marked,quote=True)}' alt='{esc(caption)}'></a><p class='caption-note'>{esc(note)} <a href='{html.escape(source_name,quote=True)}'>查看原图</a></p>")
        assessment=assessments.get(asset['id'],{})
        if assessment:
            if not risk_mode:
                paragraph({'positive':'本图发现疑似线索','negative':'本图未发现目标','unknown':'本图暂无法判断'}.get(assessment.get('decision'),'本图尚待判读'))
            if optical_review.get('status')=='completed':
                paragraph('同图误报与边界复核：'+optical_review.get('summary',''))
                for reviewed in optical_review.get('candidates',[]):
                    if reviewed.get('effective_status')=='excluded_obvious_false_positive':
                        paragraph('已排除的具体误报：'+reviewed.get('competing_interpretation','')+'；'+'；'.join(reviewed.get('visible_evidence',[])))
                    elif reviewed.get('effective_status')=='uncertain':
                        paragraph('仍待核实的候选：'+'；'.join(reviewed.get('visible_evidence',[])))
            if not any(obs.get('asset_id')==asset['id'] for obs in observations):
                bullets(assessment.get('analysis_summary',[]))
        for obs_index,obs in enumerate(observations):
            if obs.get('asset_id')==asset['id']:
                paragraph(f'E{obs_index+1}（图{index}）：'+obs.get('statement',''))
        sections.append('</figure>')
        technical_assets.append({'figure':f'图{index}','id':asset['id'],'name':asset.get('name'),'sha256':asset.get('sha256'),'metadata_scope':meta.get('metadata_scope','未知'),'derived_from_asset_id':asset.get('derived_from_asset_id')})
    if not assets and not is_research:
        paragraph('本次没有可用于风险场景判读的影像，环境初评与复核仍需补充证据。' if risk_mode else '本次没有可用于判读的影像，无法据此识别堆存疑点。')
    for index,candidate in enumerate(candidates if not risk_mode else [],1):
        sections.append("<article class='finding'><h3>疑点 "+str(index)+'：'+esc(candidate.get('label','待核查线索'))+'</h3>')
        lines.extend([f'### 疑点 {index}：'+public(candidate.get('label','待核查线索')),''])
        references='、'.join(f"E{i+1}（{figures.get(observations[i].get('asset_id'),'关联影像')}）" for i in candidate.get('evidence_indices',[]) if 0<=i<len(observations))
        paragraph('关联证据：'+references if references else '尚需补充可定位的关联证据。')
        paragraph('需排除的其他解释：'+'；'.join(candidate.get('alternative_explanations',[])))
        bullets(candidate.get('verification',[]));sections.append('</article>')
    if not candidates and not is_research and not risk_mode:
        paragraph('本次未形成可审查候选；这不等于区域不存在固废。')
    heading('任务执行摘要')
    types=[TASK_LABELS[TaskKind(kind)] for kind in task.get('task_types',[])]
    if information_only:paragraph('本任务围绕问题检索公开资料并读取重点正文。')
    elif is_research:paragraph('研究目标：'+{'availability':'影像数据可得性','analysis':'科学影像分析','timeseries':'历史变化与时序研究'}.get(research.get('mode'),'科学影像研究'))
    elif types:paragraph('任务范围：'+'、'.join(types))
    bullets(stage.get('label','')+'：'+{'completed':'已完成','running':'执行中','pending':'待执行','failed':'未完成'}.get(stage.get('status'),'尚待确认') for stage in package.get('workflow_stages',[]))
    used={asset.get('metadata',{}).get('source') for asset in assets}
    for rank in package.get('selection',{}).get('ranking',[]):
        source=rank.get('source')
        if source in used:paragraph(SOURCE_NAMES.get(source,source)+'：已获取影像用于本次判读。选用依据：'+rank.get('reason',''))
    review=package.get('adaptive_review') or {}
    for request in review.get('requests',[]):
        outcome='已取得追加复核影像' if request.get('asset_ids') else '本次未取得可用追加影像'
        paragraph(SOURCE_NAMES.get(request.get('source'),request.get('source','追加图源'))+'：'+outcome+'；'+request.get('reason',''))
    for crop in review.get('local_crops',[]):
        paragraph('疑点局部复核：'+figures.get(crop.get('asset_id'),'局部图')+'来自'+figures.get(crop.get('parent_asset_id'),'原图')+'，用于检查细部，不构成独立来源。')
    for item in package.get('scientific_evidence',[]):
        subset=item.get('subset') or {}
        paragraph(SOURCE_NAMES.get(item.get('source'),item.get('source','科学数据'))+'：'+_science_summary(item))
        if item.get('time_range'):paragraph(f"检索时段：{item['time_range']}；候选场景：{item.get('scene_count','未记录')} 景。")
        paragraph(_science_read_summary(item))
        monthly=[row for row in item.get('monthly_availability',[])[:120] if isinstance(row,dict)]
        if monthly:
            columns=['月份','目录景数','实际成像日期','点位检查／可用窗口','目录状态']
            sections.append('<table><thead><tr>'+''.join('<th>'+esc(value)+'</th>' for value in columns)+'</tr></thead><tbody>')
            lines.extend(['| '+' | '.join(columns)+' |','|---|---|---|---|---|'])
            for row in monthly:
                dates=[value for value in row.get('acquisition_dates',[])[:50] if isinstance(value,str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}',value)]
                cells=[row.get('period') or row.get('time_range','未记录'),str(row.get('scene_count','未记录')),
                    '、'.join(dates) or '未记录',str(row.get('quality_checked_scenes',0))+'／'+str(row.get('usable_windows',0)),
                    '已查全' if row.get('catalog_complete') is True else '未查全']
                sections.append('<tr>'+''.join('<td>'+esc(value)+'</td>' for value in cells)+'</tr>')
                lines.append('| '+' | '.join(public(value).replace('|','／').replace('\n',' ') for value in cells)+' |')
            sections.append('</tbody></table>');lines.append('')
            paragraph('目录有景、点位质量和实际取得窗口分别记录；目录完整只适用于本次检索范围，不表示每景均已分析。')
        if subset.get('limitation'):paragraph(subset['limitation'])
        analysis=item.get('analysis') or {}
        if analysis.get('status')=='completed':
            paragraph(analysis.get('summary',''))
            bullets(analysis.get('uncertainties',[]))
        if subset.get('full_task_aoi_covered') is False:
            paragraph('本次为局部数据窗口，尚未覆盖全部任务区域。')
        if subset.get('actual_coverage_verified') is False:
            paragraph('实际地理覆盖范围尚待核验。')
        windows=item.get('subsets') or [subset]
        seen_artifacts=set()
        for window in windows:
            artifact=_artifact_link(window.get('artifact',''))
            if artifact and artifact not in seen_artifacts:
                seen_artifacts.add(artifact)
                label='下载科学数据窗口'+('（'+str(window.get('scene_acquired_at') or '拍摄时间未知')+'）' if len(windows)>1 else '')
                sections.append("<p><a href='"+html.escape(artifact,quote=True)+"'>"+esc(label)+"</a></p>")
                lines.extend([f'[{public(label)}]({artifact})',''])
    if not risk_mode and (not is_research or (package.get('specialist_review') or {}).get('status')=='completed'):heading('独立复核')
    visual=review.get('main_visual_review') or {}
    if visual.get('status')=='completed':
        paragraph('视觉交叉复核：'+visual.get('assessment',''));bullets(visual.get('limitations',[]))
    specialist=package.get('specialist_review') or {}
    if specialist.get('status')=='completed':
        paragraph(_brief_review(public(specialist.get('assessment',''))))
        if specialist.get('conflicts'):paragraph('尚待解决的分歧：');bullets(specialist['conflicts'])
        if specialist.get('verification'):paragraph('专业复核建议：');bullets(specialist['verification'])
    elif not is_research and not risk_mode:
        paragraph({'not_configured':'本次未开展专业复核。','disabled_by_user':'本任务未启用专业复核。','failed':'专业复核未能完成，相关疑点仍需核查。'}.get(specialist.get('status'),'本次尚无专业复核意见。'))
    policy=package.get('policy') or {};sources=policy.get('sources',[])
    if not is_research or sources or package.get('documents'):heading('联网来源与参考资料' if is_research else '联网来源与用户资料')
    if not sources and not is_research:paragraph({'disabled_by_user':'本任务未启用联网监管依据检索。','not_configured':'本次未完成联网监管依据检索。'}.get(policy.get('status'),'本次未取得可引用的政策资料。'))
    for source in sources:
        url=source.get('url','')
        if urlsplit(url).scheme not in ('http','https'):continue
        label=f"[{source.get('id','')}] {source.get('title','政策资料')}"
        sections.append("<p><a rel='noreferrer' href='"+html.escape(url,quote=True)+"'>"+esc(label)+'</a></p>')
        lines.extend([f'[{public(label)}]({url})',''])
        kind={'wechat':'微信公众号','academic':'学术文献','webpage':'网页资料'}.get(source.get('source_type'),'网页资料')
        metadata=' · '.join(str(value) for value in (kind, source.get('account'), source.get('published_at')) if value)
        if source.get('content_status') in ('read','partial'):metadata+=' · '+('已读正文片段' if source['content_status']=='partial' else '已读正文')
        paragraph(metadata)
    if sources:paragraph('引用资料的现行性、地域与时间适用范围仍需核对。')
    for index,doc in enumerate(package.get('documents',[]),1):paragraph(f"用户资料 {index}：{doc.get('name','未命名资料')}（用户提供，未独立核验）。")
    heading('待核查事项')
    bullets([*recognition.get('unknowns',[]),*recognition.get('quality_issues',[]),*package.get('warnings',[]),*package.get('limitations',[]),*package.get('quality',{}).get('warnings',[])])
    paragraph(fusion.get('boundary') or '本报告用于形成监管线索，不替代现场调查、材料鉴别或执法认定。')
    lines.extend(['<details>','<summary>技术记录与完整分析</summary>',''])
    sections.append("<details class='technical'><summary>技术记录与完整分析</summary><h3>数据质量与能力记录</h3><ul>")
    gate_labels={'visual_screening':'场景筛查','area_distance':'面积与距离测量','attributed_change':'跨期变化判断','volume':'体积估算','legal_determination':'法律性质认定'}
    for key,gate in package.get('quality',{}).get('gates',{}).items():
        value=f"{gate_labels.get(key,key)}：{'具备条件' if gate.get('allowed') else '本次不作此项结论'}；{gate.get('reason','')}"
        sections.append('<li>'+esc(value)+'</li>');lines.append('- '+public(value))
    audit={'run_id':run_id,'assets':technical_assets,'documents':[{key:doc.get(key) for key in ('id','name','sha256')} for doc in package.get('documents',[])]}
    sections.append('</ul><h3>影像与资料追溯</h3><pre>'+html.escape(json.dumps(audit,ensure_ascii=False,indent=2))+'</pre>')
    lines.extend(['','```json',json.dumps(audit,ensure_ascii=False,indent=2),'```',''])
    for index,obs in enumerate(observations,1):
        value=f"E{index}：{figures.get(obs.get('asset_id'),'关联影像')}；像素窗口 {obs.get('pixel_window')}"
        sections.append('<p>'+esc(value)+'</p>');lines.append(value)
    if summary and not is_research:
        sections.append('<h3>完整综合分析</h3><p>'+html.escape(summary).replace('\n','<br>')+'</p>')
        lines.extend(['','### 完整综合分析','',summary,''])
    if specialist.get('assessment'):
        assessment=public(specialist['assessment'])
        sections.append('<h3>文本一致性复核完整记录</h3><p>'+html.escape(assessment).replace('\n','<br>')+'</p>')
        lines.extend(['','### 文本一致性复核完整记录','',assessment,''])
    sections.append("<p><a href='finding-package.json'>下载完整证据与执行记录</a></p></details><footer>无隅见微 · 多星遥感监管研判 · 可打印保存为 PDF</footer>")
    lines.extend(['[完整证据与执行记录](finding-package.json)','','</details>',''])
    stylesheet="body{font:18px/1.85 system-ui,'Microsoft YaHei',sans-serif;max-width:1180px;margin:48px auto;padding:0 28px;color:#111;background:#fff}h1{font-size:28px;margin:10px 0}h2{font-size:21px;color:#111;border-bottom:1px solid #dfe9e2;padding-bottom:11px;margin:40px 0 20px}h3{font-size:18px;margin:0 0 12px}p,li{overflow-wrap:anywhere}.eyebrow{font-size:11px;letter-spacing:2px;color:#444}.report-header p{color:#444}.conclusion{border:1px solid #cadfd0;border-left:4px solid #222;background:#fff;padding:24px;border-radius:10px}.conclusion p:last-child{margin-bottom:0}.finding,figure{background:#fff;padding:22px;border:1px solid #e1e9e3;border-radius:12px;margin:22px 0}.finding{border-left:3px solid #c4a05a}figure img{display:block;max-width:100%;height:auto;max-height:760px;margin:16px auto}figcaption strong{display:block;font-size:16px}figcaption span,.caption-note{font-size:12px;color:#444}.technical{margin-top:40px;border:1px solid #dbe6de;background:#fff;border-radius:10px;padding:18px 22px;font-size:12px;color:#444}.technical summary{cursor:pointer;color:#111;font-size:14px}.technical h3{font-size:14px;margin-top:22px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:11px;background:#fff;padding:16px;border-radius:8px}a{color:#111;text-underline-offset:3px}footer{font-size:11px;color:#444;text-align:center;margin:35px 0 20px}@media(max-width:640px){body{margin:25px auto;padding:0 16px}h1{font-size:23px}figure,.finding{padding:15px}}@media print{body{margin:0;background:white;font-size:11pt}figure,.finding,.conclusion{break-inside:avoid}.technical{font-size:9pt}h2{break-after:avoid}}"
    stylesheet+="table{width:100%;border-collapse:collapse;font-size:15px;line-height:1.7}th,td{text-align:left;vertical-align:top;padding:9px 7px;border-bottom:1px solid #ddd}th{font-weight:600}"
    page="<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>无隅见微遥感报告</title><style>"+stylesheet+'</style></head><body>'+'\n'.join(sections)+'</body></html>'
    (folder/'report.html').write_text(page,encoding='utf-8')
    (folder/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
