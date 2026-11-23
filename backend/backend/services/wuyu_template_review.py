"""A user's reference choice precedes image generation; image review precedes tracing."""
from copy import deepcopy
import hashlib
import json

from .wuyu_state import apply_event
from .wuyu_visual_settings import FOLLOW_TEMPLATE_STYLE, follows_template, effective_visual_settings


def choice_signature(body):
    """Bind consent to the current visual inputs and parsed private template bytes."""
    visual = {key: body['brief'].get(key) for key in (
        'scenario', 'subscenario', 'style', 'aspect_ratio', 'language', 'image_profile', 'template_id')}
    templates = [{'id': item['id'], 'sha256': item.get('sha256'), 'status': item.get('status')}
                 for item in body['materials'] if item.get('role') == 'template']
    value = {'visual': visual, 'recipe': body['reference_recipe'], 'templates': templates}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def template_key(body, choice):
    """A template's identity across projects (op195), or None.

    Uploaded templates are the same template wherever they are reused (the account list
    de-duplicates them by file SHA-256); personal saved designs keep their own ID; library
    templates keep their catalogue source ID.
    """
    if not isinstance(choice, dict) or not isinstance(choice.get('source_id'), str):
        return None
    if choice.get('source_kind') == 'library':
        return 'library:' + choice['source_id']
    if choice.get('source_kind') != 'upload' or not choice['source_id'].startswith('material:'):
        return None
    material = next((m for m in body.get('materials', []) if m.get('id') == choice['source_id'][len('material:'):]), None)
    if not isinstance(material, dict):
        return None
    if isinstance(material.get('personal_template_id'), str):
        return 'personal:' + material['personal_template_id']
    sha = material.get('sha256')
    return 'upload:' + sha if isinstance(sha, str) and len(sha) == 64 else None


def record_template_choice(app, owner_id, body, request_key=None):
    """Keep a confirmed choice as a design event keyed by deck type. Never blocks the choice."""
    from .wuyu_deck_types import effective_deck_type
    habits = app.extensions.get('wuyu_design_habits')
    choice = body.get('template_choice')
    key = template_key(body, choice)
    if not habits or not owner_id or not key:
        return None
    deck_type = effective_deck_type(body.get('brief'))
    return habits.record_event(owner_id, body['workflow']['project_id'], 'template_choice',
        {'template_key': key, 'source_kind': choice.get('source_kind'), 'title': str(choice.get('title') or '')[:200],
         'deck_type': deck_type},
        context={'deck_type': deck_type}, request_key=request_key, commit=False)


def current_template_choice(body):
    choice = body.get('template_choice')
    # Historical local choices were consent to synthesized neutral layouts,
    # not to the original render now shown under the same stable source ID.
    protocol_one = body['workflow'].get('requires_final_image_review') is True
    legacy_library = isinstance(choice, dict) and choice.get('source_kind') == 'library' and not choice.get('preview_kind')
    ui_confirmed = isinstance(choice, dict) and choice.get('confirmed_via') == 'template_confirmation_ui'
    return bool(isinstance(choice, dict) and (not protocol_one or ui_confirmed and not legacy_library)
                and choice.get('signature') == choice_signature(body))


def public_template_choice(body):
    """Expose only the decision and public identifiers, without source content or paths."""
    choice = body.get('template_choice') or {}
    if body['workflow'].get('requires_final_image_review') is not True:
        return {'protocol_version':0,'required':False,'confirmed':False}
    single = choice.get('source_kind') == 'upload' or choice.get('reference_scope') == 'single_image'
    return {'protocol_version': 1, 'required': not current_template_choice(body),
            'confirmed': current_template_choice(body),
            'source_kind': choice.get('source_kind'), 'source_id': choice.get('source_id'),
            'title': choice.get('title'), 'preview_count': choice.get('preview_count', 0),
            'minimum_library_preview_count': 1 if single else 4, 'minimum_body_reference_count': 1 if single else 5,
            **effective_visual_settings(body, current_template_choice(body))}


class TemplateReviewMixin:
    """Validate the explicit, versioned choice through the same workflow transaction."""

    def _require_template_choice(self, body):
        from .wuyu_workflow_service import WorkflowAPIError
        if body['workflow'].get('requires_final_image_review') is not True:
            return
        if not current_template_choice(body):
            raise WorkflowAPIError('请先查看推荐模板或上传自己的模板，确认后再生成页面图片。',
                                   'TEMPLATE_SELECTION_REQUIRED', 409)

    def _require_template_confirmation_ui(self, project_id):
        """Only the person clicking the template preview can confirm; a model cannot."""
        from .wuyu_user_consent import require_user_interaction, TEMPLATE
        require_user_interaction(self.app, project_id, TEMPLATE)

    def _confirm_template_choice(self, body, payload):
        from .wuyu_workflow_service import WorkflowAPIError, _id, _now
        self._require_template_confirmation_ui(body['workflow']['project_id'])
        if set(payload) not in ({'source_id'}, {'material_id'}):
            raise WorkflowAPIError('Choose exactly one library template or private uploaded template')
        personal_id = payload.get('source_id')
        if isinstance(personal_id, str) and personal_id.startswith(('personal:', 'uploaded:')):
            from models import db, Project
            project = db.session.get(Project, body['workflow']['project_id'])
            if personal_id.startswith('uploaded:'):
                from .wuyu_uploaded_templates import UploadedTemplates
                from .wuyu_template_library import TemplateLibraryError
                try:
                    material = UploadedTemplates(self.app).attach(project.user_id, personal_id, body)
                except TemplateLibraryError as exc:
                    raise WorkflowAPIError(str(exc), exc.code, exc.status) from None
            else:
                material = self.app.extensions['wuyu_personal_templates'].attach(project.user_id, personal_id, body)
            payload = {'material_id': material['id']}
        if 'material_id' in payload:
            material = next((item for item in body['materials']
                             if item['id'] == payload['material_id'] and item.get('role') == 'template'), None)
            if not material or material.get('status') != 'completed' or not material.get('template_images'):
                raise WorkflowAPIError('请等待自己的模板解析完成，再确认使用。', 'TEMPLATE_NOT_READY', 409)
            source_id, kind = 'material:' + material['id'], 'upload'
            title, count = material['filename'], len(material['template_images'])
        else:
            source_id, kind = payload['source_id'], 'library'
            if not isinstance(source_id, str):
                raise WorkflowAPIError('Template source ID must be text')
            path = self._catalog_path()
            catalog = json.loads(path.read_text(encoding='utf-8-sig'))
            source = next((item for item in catalog.get('sources', []) if item['id'] == source_id), None)
            original_local = bool(source and source.get('source_type') == 'local'
                and source.get('preview_kind') == 'original_template_render' and source.get('neutralized') is False)
            reviewed_derivative = bool(source and source.get('neutralized') is True
                and source.get('preview_kind') in {'neutral_layout_redraw', 'neutral_native_redraw', 'sanitized_original_render'})
            external = bool(source and source.get('source_type') in {'website', 'wechat'}
                and source.get('preview_kind') == 'external_template_render' and source.get('neutralized') is False)
            if not source or source.get('review_status') != 'approved' or not (original_local or reviewed_derivative or external):
                raise WorkflowAPIError('请选择当前已审核的真实本地模板或共享模板。', 'TEMPLATE_NOT_AVAILABLE', 409)
            count = sum(bool(page.get('selected')) for page in source.get('pages', []))
            single_reference = source.get('reference_scope') == 'single_image'
            if single_reference and not (body['brief'].get('creation_kind') == 'asset' or body['brief'].get('creation_kind') == 'pages' and body['brief'].get('page_count') == 1):
                raise WorkflowAPIError('这是单张图参考模板，请先选择专业单图或1页内容；整套PPT请选择整套模板。', 'TEMPLATE_SCOPE_MISMATCH', 409)
            if count < (1 if single_reference else 5):
                raise WorkflowAPIError('此模板可用正文预览不足5页，请选择其他模板或上传自己的模板。',
                                       'TEMPLATE_PREVIEW_INSUFFICIENT', 409)
            if original_local and not single_reference and (source.get('body_reference_count', 0) < 5 or source.get('reference_sufficient') is not True):
                raise WorkflowAPIError('此原模板实际正文参考不足5页，请选择其他模板或上传自己的模板。',
                                       'TEMPLATE_PREVIEW_INSUFFICIENT', 409)
            title = source.get('title') or source_id
        recipe = self._normalize_recipe({'style_anchor_id': source_id, 'references': []})
        self._validate_recipe(recipe, body)
        if (current_template_choice(body) and (body.get('template_choice') or {}).get('source_id') == source_id
                and follows_template(body) and not body['brief'].get('design_notes')):
            return
        # Confirming a template explicitly replaces the previous visual preset.
        # Content requirements remain intact; old additional styling is superseded.
        body['brief']['style'] = FOLLOW_TEMPLATE_STYLE
        body['brief'].pop('design_notes', None)
        body['reference_recipe'] = deepcopy(recipe)
        body['workflow'] = self._new_state(body['workflow']['project_id'], body['brief'], 'template_and_final'
                                          if body['brief'].get('creation_kind', 'deck') == 'deck' else 'template_only')
        body['template_choice'] = {'source_kind': kind, 'source_id': source_id, 'title': title,
                                   'confirmed_via': 'template_confirmation_ui', 'style_mode': 'template',
                                   'preview_kind': source.get('preview_kind') if kind == 'library' else 'private_uploaded_template',
                                   'preview_count': count, 'reference_scope': source.get('reference_scope') if kind == 'library' else 'single_image', 'confirmed_at': _now(),
                                   'signature': choice_signature(body)}
        try:
            from models import db, Project
            from sqlalchemy.exc import SQLAlchemyError
            owner = db.session.get(Project, body['workflow']['project_id'])
            record_template_choice(self.app, owner.user_id if owner else None, body,
                request_key='template:%s:%s' % (body['workflow']['project_id'], body['template_choice']['signature']))
        except SQLAlchemyError:
            raise  # the workflow's own transaction handling must see a database error
        except Exception as exc:
            self.app.logger.warning('Template choice capture skipped: type=%s', type(exc).__name__)
        self._restore_template_choice(body)
        body['epoch'] += 1

    def _restore_template_choice(self, body):
        """Restore the same genuine reference choice after accepted text draft reconstruction."""
        from .wuyu_workflow_service import _id
        if not current_template_choice(body):
            return
        choice = body['template_choice']
        state = apply_event(body['workflow'], {'event_id': _id(), 'type': 'template.selected',
                                              'payload': {'template_id': choice['source_id']}})
        body['workflow'] = apply_event(state, {'event_id': _id(), 'type': 'template.choice_confirmed',
            'payload': {'template_revision': state['template']['revision'], 'source_kind': choice['source_kind']}})

    def _generate_review_images(self, job, pipeline):
        """Generate the requested real pages once, without an extra sample-image cycle."""
        _, body = self._read(job.project_id)
        self._require_template_choice(body)
        if not body['slide_specs']:
            self._outline(job, pipeline)
            _, body = self._read(job.project_id)
        if body['brief'].get('creation_kind', 'deck') != 'deck':
            self._event(job.project_id, 'focused.requested', {'generation_requested': True}, job)
        self._continue(job, pipeline)
