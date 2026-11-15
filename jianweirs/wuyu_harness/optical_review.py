"""Evidence-preserving, same-image review of optical detector hypotheses.

The visual reviewer may reject a *specific* obvious false positive. Its failure
or disagreement is never a negative scene finding, and another source's empty
image cannot erase a candidate. Original detector results stay in the audit.
"""
from __future__ import annotations

import base64
import io
from dataclasses import replace
from typing import Literal

from PIL import Image
from pydantic import Field

from .evidence import sanitize_claims
from .models import ProviderError
from .schemas import Observation, Recognition, Record


class OpticalCandidateReview(Record):
    candidate_index: int = Field(ge=0, le=29)
    verdict: Literal['supports_candidate', 'obvious_false_positive', 'uncertain']
    visible_evidence: list[str] = Field(min_length=1, max_length=4)
    competing_interpretation: str = Field(min_length=1, max_length=300)
    exclusion_category: Literal['building', 'road_or_pavement', 'vegetation', 'water', 'bare_ground', 'other', 'none'] = 'none'
    sufficient_visible_detail: bool = False
    corrected_pixel_window: tuple[int, int, int, int] | None = None
    visible_boundary_confirmed: bool = False


class OpticalFalsePositiveReview(Record):
    asset_id: str
    summary: str = Field(min_length=1, max_length=900)
    full_scene_inspected: bool = False
    image_quality_sufficient: bool = False
    additional_suspected_regions: bool = False
    candidates: list[OpticalCandidateReview] = Field(min_length=1, max_length=30)
    limitations: list[str] = Field(max_length=5)


REVIEW_INSTRUCTION = '''你是高分光学遥感的独立目视复核员。本次只有一幅原始影像及其未标注局部裁剪，均为同一asset；不是多时相或跨图投票。
无隅见微提供固废候选类型，不能预设候选存在。逐候选核对原图实际纹理、轮廓和场景关系，检查正常建筑/屋顶、道路硬化地、植被、水体、正常裸地等明显误报。
只有清晰、肯定的独立可见特征支持非堆体解释，且sufficient_visible_detail=true时才可选obvious_false_positive；visible_evidence至少列出两个具体可见特征，不得只写“不像固废”或“未看见”。裸地不能仅因颜色均一而排除覆盖或混杂堆体；施工或堆料用途合法与否无法据图判断。
若只是模型分歧、框太大、看不清、疑似建筑旁还有杂乱物料、或缺少细节，一律uncertain，不得为了给结论选负类。supports_candidate仅保留疑似类别，不能确认实际材料化学成分或法定属性。
检查全景是否还有未解答疑点。additional_suspected_regions=true时不能给全图未发现；未复核全景或影像质量不足也不能给全图负判。不要从采集时间、2026高清图源标签、显示放大倍数猜拍摄日期、原生GSD、地理定位或米制面积。
只有supports_candidate且真实可见边界明确时可提出corrected_pixel_window，坐标必须是原始整图像素[left,top,right,bottom]；这是人工复核用的视觉框建议，不是精确测量。边界不清则null。每个输入candidate_index返回且仅返回一项，不创造新编号。排除项不得另给固废框。仅返回可核验的简短依据，不输出内部推理。'''


def candidate_windows(recognition: Recognition, asset_id: str) -> list[dict]:
    rows = []
    for index, candidate in enumerate(recognition.candidates):
        windows = [list(recognition.observations[e].pixel_window) for e in candidate.evidence_indices
                   if recognition.observations[e].asset_id == asset_id and recognition.observations[e].pixel_window]
        rows.append({'candidate_index': index, 'label': candidate.label, 'pixel_windows': windows})
    return rows


def _review_parts(content: bytes, mime: str, width: int, height: int, windows: list[dict]) -> list[dict]:
    parts = [{'type': 'text', 'text': f'同一原始影像，全景像素尺寸{width}×{height}。后续局部图也来自此图，不是独立数据。'},
             {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{base64.b64encode(content).decode()}'}}]
    # Small candidates need their original pixels, rather than an upsampled
    # screenshot. Context is deliberately included around each unmarked crop.
    with Image.open(io.BytesIO(content)) as image:
        for row in windows[:6]:
            if not row['pixel_windows']:
                continue
            x1, y1, x2, y2 = row['pixel_windows'][0]
            pad = max(24, (max(x2-x1, y2-y1) + 1)//2)
            box = (max(0, x1-pad), max(0, y1-pad), min(width, x2+pad), min(height, y2+pad))
            if box == (0, 0, width, height):
                continue
            stream = io.BytesIO(); image.crop(box).convert('RGB').save(stream, 'PNG')
            parts.extend([{'type': 'text', 'text': f"候选{row['candidate_index']}局部上下文；整图裁剪窗口{list(box)}，保持原始像素，不要将局部坐标误写为整图坐标。"},
                          {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,'+base64.b64encode(stream.getvalue()).decode()}}])
    return parts


def valid_correction(window, original_windows, width, height) -> bool:
    """No clipping: invalid/moved boxes remain proposals, never silent labels."""
    if not window or len(window) != 4 or any(type(v) is not int for v in window):
        return False
    x1, y1, x2, y2 = window
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        return False
    area = (x2-x1)*(y2-y1)
    for left, top, right, bottom in original_windows:
        old_area = (right-left)*(bottom-top)
        intersection = max(0, min(x2,right)-max(x1,left))*max(0, min(y2,bottom)-max(y1,top))
        if old_area and .1 <= area/old_area <= 4 and intersection/min(area,old_area) >= .25:
            return True
    return False


def apply_review(recognition: Recognition, review: OpticalFalsePositiveReview, *, width: int, height: int):
    """Return an effective view and an explicit audit; never mutate raw output."""
    raw = recognition.model_dump(mode='json')
    expected = set(range(len(recognition.candidates)))
    indices = [r.candidate_index for r in review.candidates]
    if len(indices) != len(set(indices)) or set(indices) != expected:
        raise ValueError('同图复核必须逐一对应所有已有候选')
    if any(o.asset_id != review.asset_id for o in recognition.observations):
        raise ValueError('同图复核不得混用其他影像')
    cleaned, removed_claims = sanitize_claims(review.model_dump(mode='json'))
    for item in cleaned['candidates']:
        item['visible_evidence'] = [v for v in item['visible_evidence'] if v] or ['（原依据含未经核验表述，已删除）']
        item['competing_interpretation'] = item['competing_interpretation'] or '（已删除未经核验表述）'
    cleaned['summary'] = cleaned['summary'] or '同图复核摘要含未经核验表述，已删除；逐候选结论保留。'
    review = OpticalFalsePositiveReview.model_validate(cleaned)
    rows = []; kept = []; effective = recognition.model_copy(deep=True)
    excluded_indices = set(); disputed = []
    windows = {row['candidate_index']: row['pixel_windows'] for row in candidate_windows(recognition, review.asset_id)}
    for row in review.candidates:
        value = row.model_dump(mode='json'); value['original_pixel_windows'] = windows[row.candidate_index]
        value['proposed_corrected_pixel_window']=value['corrected_pixel_window']
        value['corrected_pixel_window']=None
        # Model self-confidence is not a threshold. Two concrete observations,
        # adequate detail and a recognized non-waste object are required.
        excluded = (row.verdict == 'obvious_false_positive' and row.sufficient_visible_detail
                    and review.image_quality_sufficient and row.exclusion_category in {'building','road_or_pavement','vegetation','water'}
                    and len(set(s.strip() for s in row.visible_evidence if s.strip())) >= 2
                    and bool(windows[row.candidate_index]))
        status = 'excluded_obvious_false_positive' if excluded else 'supported_candidate' if row.verdict == 'supports_candidate' and row.sufficient_visible_detail and review.image_quality_sufficient else 'uncertain'
        value['effective_status'] = status
        if excluded:
            excluded_indices.add(row.candidate_index)
            value['box_correction_status']='not_adopted_excluded_candidate'
        else:
            kept.append(row.candidate_index)
            if status == 'uncertain': disputed.append(row.candidate_index)
            proposed = row.corrected_pixel_window
            accepted = (status == 'supported_candidate' and row.visible_boundary_confirmed and
                        valid_correction(proposed, windows[row.candidate_index], width, height))
            value['box_correction_status'] = 'visual_proposal_adopted_pending_human_review' if accepted else 'proposal_rejected' if proposed else 'not_proposed'
            if accepted:
                candidate = effective.candidates[row.candidate_index]
                evidence = [i for i in candidate.evidence_indices if effective.observations[i].pixel_window]
                if len(evidence) == 1:
                    # Create a distinct observation: the raw detector box remains
                    # in jianwei_raw_recognition and is never overwritten there.
                    old = evidence[0]
                    candidate.evidence_indices = [i for i in candidate.evidence_indices if i != old]
                    candidate.evidence_indices.append(len(effective.observations))
                    effective.observations.append(Observation(asset_id=review.asset_id, pixel_window=tuple(proposed),
                        statement='Gemini同图复核提出的疑似堆体视觉边界，尚待人工核验；不用于面积或地理测量。'))
                    value['corrected_pixel_window']=list(proposed)
                else:
                    value['box_correction_status'] = 'proposal_rejected_multiple_evidence_windows'
        rows.append(value)
    effective.candidates = [effective.candidates[i] for i in kept]
    retained_evidence = {i for c in effective.candidates for i in c.evidence_indices}
    unused_box_indices = {i for i,o in enumerate(effective.observations) if o.pixel_window and i not in retained_evidence}
    remap = {}; observations = []
    for index, observation in enumerate(effective.observations):
        if index in unused_box_indices: continue
        remap[index] = len(observations); observations.append(observation)
    for candidate in effective.candidates:
        candidate.evidence_indices = [remap[i] for i in candidate.evidence_indices]
    effective.observations = observations
    if excluded_indices or disputed:
        # A raw whole-scene positive summary must not be re-promoted after its
        # sole candidate was rejected. Raw wording is kept in the audit only.
        effective.observations = [o.model_copy(update={'statement': '同图复核：'+review.summary}) if o.pixel_window is None else o for o in effective.observations]
    if disputed:
        effective.unknowns.append('见微候选与同图视觉复核存在未解决分歧；待核实，不按无固废处理。')
    all_excluded = bool(expected) and excluded_indices == expected
    qualified_negative = (all_excluded and review.full_scene_inspected and review.image_quality_sufficient
                          and not review.additional_suspected_regions and not recognition.quality_issues)
    # An unresolved reviewer disagreement keeps the detector's positive clue:
    # only an explicit, qualified exclusion may lower a retained candidate.
    decision = ('positive' if any(r['effective_status'] in {'supported_candidate', 'uncertain'} for r in rows)
                else 'negative' if qualified_negative else 'unknown')
    audit = {'status': 'completed', 'purpose': 'optical_same_image_false_positive_review', 'reviewer_role': 'science_analyst',
             'asset_id': review.asset_id, 'summary': review.summary, 'candidates': rows,
             'full_scene_inspected': review.full_scene_inspected, 'image_quality_sufficient': review.image_quality_sufficient,
             'additional_suspected_regions': review.additional_suspected_regions, 'limitations': review.limitations,
             'excluded_candidate_count': len(excluded_indices), 'uncertain_candidate_count': len(disputed),
             'supported_candidate_count': sum(r['effective_status']=='supported_candidate' for r in rows),
             'qualified_scene_negative': qualified_negative, 'effective_decision': decision,
             'same_image_only': True, 'raw_detector_output_preserved': True,
             'removed_unsupported_sentences': removed_claims,
             'boundary': '排除仅限已复核的具体误报；未发现仅限该图可见范围和时点，不等于现实中没有固废。'}
    return effective, audit, raw


TRANSIENT_KINDS = frozenset({'timeout', 'transport_error'})


def is_transient_failure(error) -> bool:
    """哪些失败值得重试一次：超时、网络错误、限流和服务端 5xx。内容问题（格式错误、超长）不重试。"""
    kind = getattr(error, 'failure_kind', None)
    status = getattr(error, 'http_status', None)
    if kind in TRANSIENT_KINDS:
        return True
    return kind == 'http_error' and status is not None and (status == 429 or 500 <= status <= 599)


def retry_timeout(pipeline) -> float:
    return float(getattr(pipeline.settings, 'optical_review_retry_timeout', 45) or 45)


async def review_optical_candidates(pipeline, run_id, task, asset, recognition, context, image_content, *, context_parts=None):
    config = pipeline.settings.models.get('science_analyst')
    if not config or not config.ready or 'image' not in config.input_modalities:
        return recognition, {'status': 'not_configured_for_images', 'reviewer_role': 'science_analyst',
                             'reason': 'optical_visual_reviewer_not_configured'}, None
    config=replace(config,timeout=min(config.timeout,getattr(pipeline.settings,'optical_review_timeout',90)))
    pipeline.event(run_id, 'optical_false_positive_review', 'running',
                   {'asset_id': asset['id'], 'source': asset['metadata']['source'], 'model': config.model,
                    'candidate_count': len(recognition.candidates), 'purpose': 'same_image_false_positive_review'})
    try:
        instruction=REVIEW_INSTRUCTION
        parts=_review_parts(image_content, asset['mime'], asset['width'], asset['height'], candidate_windows(recognition, asset['id']))
        if context_parts:
            instruction+='最后附上同源父图，仅作周边关系参考，不是独立来源；所有候选坐标必须仍用本次主图像素，不得用父图坐标。'
            parts += [{'type':'text','text':'以下是同源父图的全景上下文；不是新时相或独立来源，不在父图上重新编号本次候选。'},*context_parts]
        payload = {'purpose': 'optical_same_image_false_positive_review', 'task': task.objective, 'asset': context,
                   'candidate_hypotheses': candidate_windows(recognition, asset['id']),
                   'no_cross_image_override': True, 'pixel_coordinate_order': '[left,top,right,bottom]'}
        try:
            result, trace = await pipeline.models.complete(config, instruction, payload, OpticalFalsePositiveReview, parts)
        except ProviderError as first:
            if not is_transient_failure(first):
                raise
            # 超时、网络抖动、限流、5xx 往往是暂时的：只重试一次，并缩短等待，避免两次都耗满超时。
            pipeline.event(run_id, 'optical_false_positive_review', 'retrying',
                           {'asset_id': asset['id'], 'model': config.model, 'failure_kind': first.failure_kind,
                            'http_status': first.http_status, 'summary': '独立复核遇到暂时性故障，重试一次。'})
            config = replace(config, timeout=min(config.timeout, retry_timeout(pipeline)))
            result, trace = await pipeline.models.complete(config, instruction, payload, OpticalFalsePositiveReview, parts)
        if result.asset_id != asset['id']: raise ValueError('同图复核引用未知影像')
        effective, review, _ = apply_review(recognition, result, width=asset['width'], height=asset['height'])
        trace.update(purpose='optical_same_image_false_positive_review', asset_id=asset['id'])
        review['model'] = config.model
        pipeline.event(run_id, 'optical_false_positive_review', 'completed',
                       {'asset_id': asset['id'], 'source': asset['metadata']['source'], 'model': config.model,
                        'summary': review['summary'], 'effective_decision': review['effective_decision'],
                        'excluded_candidate_count': review['excluded_candidate_count'], 'uncertain_candidate_count': review['uncertain_candidate_count']})
        return effective, review, trace
    except (ProviderError, ValueError) as error:
        # Do not change detector output to a negative on a provider failure.
        review = {'status': 'unavailable', 'reviewer_role': 'science_analyst', 'model': config.model,
                  'reason': getattr(error, 'failure_kind', None) or type(error).__name__,
                  'http_status': getattr(error, 'http_status', None),
                  'effective_decision': 'unknown', 'same_image_only': True,
                  'summary': '独立视觉复核未形成有效结果，见微候选保留待核实，不能按未发现处理。'}
        pipeline.event(run_id, 'optical_false_positive_review', 'unavailable',
                       {'asset_id': asset['id'], 'model': config.model, 'reason': review['reason'], 'summary': review['summary']})
        return recognition, review, None
