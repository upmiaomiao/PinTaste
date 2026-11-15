"""Business actions only: clients cannot submit provider/QA success events."""
from pathlib import Path
from flask import Blueprint, request, current_app, send_file
from models import db, Project
from utils import success_response, error_response, not_found
from services.wuyu_workflow_service import WorkflowAPIError
from services.wuyu_quota import QuotaError

wuyu_workflow_bp = Blueprint('wuyu_workflow', __name__, url_prefix='/api/projects')


def enforce_managed_workflow():
    """Prevent legacy paid generation/write routes from bypassing review gates."""
    if request.method == 'OPTIONS' or not request.path.startswith('/api/projects/'):
        return None
    parts = request.path[len('/api/projects/'):].split('/')
    if not parts or len(parts) > 1 and parts[1] == 'workflow':
        return None
    suffix = parts[1] if len(parts) > 1 else ''
    blocked = suffix in {'generate', 'refine', 'export'} or (suffix == 'pages' and request.method not in {'GET', 'HEAD'})
    if not suffix and request.method in {'PUT', 'PATCH'}:
        data = request.get_json(silent=True)
        blocked = not isinstance(data, dict) or bool(set(data) - {'project_title'})
    if not blocked:
        return None
    project = db.session.get(Project, parts[0])
    if project and project.is_workflow_managed():
        return error_response('WORKFLOW_MANAGED', 'This project uses the reviewed PPT workflow; use /workflow/action', 409)
    return None


def _service(project_id):
    project = Project.query.filter_by(id=project_id).first()
    if not project:
        return None, not_found('Project')
    user = getattr(request, 'current_user', None)
    if not user:
        return None, error_response('UNAUTHORIZED', 'Authentication required', 401)
    if user.role != 'admin' and project.user_id != user.id:
        return None, error_response('FORBIDDEN', 'Access denied', 403)
    return current_app.extensions['wuyu_workflow'], None


def _handle(project_id, operation):
    service, error = _service(project_id)
    if error is not None:
        return error
    try:
        return success_response(operation(service))
    except WorkflowAPIError as exc:
        return error_response(exc.code, str(exc), exc.status)
    except QuotaError as exc:
        return error_response(exc.code, str(exc), exc.status)


@wuyu_workflow_bp.get('/workflow-generation-options')
def workflow_generation_options():
    if not getattr(request, 'current_user', None):
        return error_response('UNAUTHORIZED', 'Authentication required', 401)
    from services.wuyu_runtime_settings import get_generation_options
    try:
        from services.wuyu_identity import generation_options_for
        return success_response(generation_options_for(request.current_user,get_generation_options(dict(current_app.config))))
    except (ValueError, OSError):
        return error_response('RUNTIME_SETTINGS_INVALID', 'Model options require administrator repair', 503)


@wuyu_workflow_bp.post('/research-intent')
def arm_research_intent():
    """The browser records the composer search switch the user sent with (see wuyu_research_intent)."""
    user = getattr(request, 'current_user', None)
    if not user:
        return error_response('UNAUTHORIZED', 'Authentication required', 401)
    from services.wuyu_user_consent import RESEARCH
    if request.headers.get('X-Wuyu-Interaction') != RESEARCH:
        return error_response('RESEARCH_CONFIRMATION_REQUIRED', '联网搜索只能由用户在输入框打开。', 403)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'mode'} or data.get('mode') not in ('web', 'deep'):
        return error_response('INVALID_REQUEST', 'mode must be web or deep', 400)
    from services.wuyu_research_intent import arm
    arm(user.id, data['mode'])
    return success_response({'armed': data['mode']})


@wuyu_workflow_bp.get('/<project_id>/workflow')
def get_workflow(project_id):
    return _handle(project_id, lambda service: service.snapshot(project_id))


@wuyu_workflow_bp.get('/<project_id>/workflow/research')
def get_workflow_research(project_id):
    """Bounded conversation progress without repeatedly serializing materials."""
    from services.wuyu_deep_research import research_public
    def view(service):
        revision, body = service._read(project_id)
        return {'project_id':project_id, 'revision':revision, 'research':research_public(body),
                'brief':{key:body.get('brief', {}).get(key, '') for key in ('topic','goal')}}
    return _handle(project_id, view)


@wuyu_workflow_bp.post('/<project_id>/workflow/action')
def workflow_action(project_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get('action'), str) or not isinstance(data.get('payload', {}), dict):
        return error_response('INVALID_REQUEST', 'Expected action and object payload', 400)
    def operation(service):
        # The interactive template endpoint supplies this header. The ordinary
        # MCP business-action tool cannot manufacture template consent.
        if (data['action'] == 'confirm_template_choice'
                and request.headers.get('X-Wuyu-Interaction') != 'template-confirmation'):
            raise WorkflowAPIError('请在模板预览中点击确认使用，不能由模型代为确认。',
                                   'TEMPLATE_CONFIRMATION_UI_REQUIRED', 403)
        return service.execute_action(project_id, data['action'], data.get('payload', {}),
            expected_revision=data.get('expected_revision'),
            idempotency_key=request.headers.get('Idempotency-Key') or data.get('request_id'))
    return _handle(project_id, operation)


@wuyu_workflow_bp.route('/<project_id>/workflow/materials', methods=['GET', 'POST'])
def workflow_materials(project_id):
    if request.method == 'GET':
        return _handle(project_id, lambda service: service.snapshot(project_id)['materials'])
    return _handle(project_id, lambda service: service.add_materials(
        project_id, request.files.getlist('files') or request.files.getlist('file'),
        request.get_json(silent=True) if request.is_json else None,
        user_id=request.current_user.id, role=request.form.get('role', 'material'),
        upload_id=request.headers.get('X-Upload-Id'), declared_sha=request.headers.get('X-Upload-Sha256'),
        batch_count=request.headers.get('X-Batch-Count'), batch_bytes=request.headers.get('X-Batch-Bytes')))


@wuyu_workflow_bp.post('/<project_id>/workflow/materials/<material_id>/delete')
def delete_workflow_material(project_id, material_id):
    if request.headers.get('X-Wuyu-Interaction') != 'material-deletion':
        return error_response('MATERIAL_DELETE_CONFIRMATION_REQUIRED', '请在材料列表确认永久删除。', 403)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'confirmed', 'request_id', 'expected_revision'}:
        return error_response('INVALID_REQUEST', '删除只接受当前材料确认与项目版本。', 400)
    return _handle(project_id, lambda service: service.delete_material(project_id, material_id,
        request.current_user.id, confirmed=data['confirmed'], request_id=data['request_id'],
        expected_revision=data['expected_revision']))


@wuyu_workflow_bp.post('/<project_id>/workflow/provider-test')
def workflow_provider_test(project_id):
    if getattr(getattr(request, 'current_user', None), 'role', None) != 'admin':
        return error_response('FORBIDDEN', 'Only administrators may test model providers', 403)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or set(data) - {'role'}:
        return error_response('INVALID_REQUEST', 'Only a provider role may be supplied; secrets are server-side', 400)
    return _handle(project_id, lambda service: service.test_providers(data.get('role')))


@wuyu_workflow_bp.get('/<project_id>/workflow/evidence')
def workflow_evidence(project_id):
    return _handle(project_id, lambda service: service.evidence(project_id, request.args.get('q', ''),
        limit=request.args.get('limit', 8, type=int), material_id=request.args.get('material_id'),
        page=request.args.get('page', type=int), offset=request.args.get('offset', 0, type=int),
        max_chars=request.args.get('max_chars', 12000, type=int), include_figures=request.args.get('include_figures', 'true').lower() != 'false'))


def _web_operation(project_id, kind):
    service, error = _service(project_id)
    if error is not None:
        return error
    from services.wuyu_web_research import WuyuWebResearch
    from services.wuyu_errors import PipelineError
    data = request.get_json(silent=True)
    allowed = ({'query', 'count', 'ori_question', 'scope'} if kind == 'search' else
               {'query', 'count', 'max_pages', 'time_range', 'sort'} if kind == 'wechat_search' else
               {'reference_id', 'offset', 'max_chars'} if kind == 'wechat_fetch' else {'url'})
    if not isinstance(data, dict) or set(data) - allowed:
        return error_response('WEB_INVALID_REQUEST', '仅支持公开网页的搜索词或地址，不接受模型、密钥或执行指令。', 400)
    from services.wuyu_deep_research import research_public, bounded_web_policy
    _, body = service._read(project_id)
    research = research_public(body)
    if research['required'] and (body.get('paused') or not research['consent_current'] or research['mode'] not in {'web','deep'}
            or research['status'] not in {'queued','running','review_required','accepted'}):
        return error_response('RESEARCH_CONFIRMATION_REQUIRED', '请先在对话中的调研选项确认公开研究问题与消耗，再开始联网搜索。', 409)
    if research['required'] and research['status'] in {'queued','running'}:
        return error_response('RESEARCH_IN_PROGRESS', '已确认的调研正在执行；请查看对话中的搜索进度，或中断后补充意见，避免重复检索。', 409)
    try:
        options = {}
        if research['required']:
            from services.wuyu_runtime_settings import get_research_policy
            # Raw tools share the same confirmed round's meter. They cannot
            # silently receive a second search/fetch budget beside the graph.
            options = {'scope_id':'research:' + research['report_id'],
                       'policy':bounded_web_policy(body['research'], get_research_policy(dict(current_app.config)))}
        researcher = WuyuWebResearch(dict(current_app.config), actor_id=request.current_user.id, project_id=project_id, **options)
        key = request.headers.get('Idempotency-Key')
        if kind == 'wechat_search':
            result = researcher.wechat_search(data.get('query'), count=data.get('count', 5), max_pages=data.get('max_pages', 3),
                time_range=data.get('time_range', 'two_years'), sort=data.get('sort', 'recent'), request_id=key)
        elif kind == 'wechat_fetch':
            result = researcher.wechat_article(data.get('reference_id'), offset=data.get('offset', 0), max_chars=data.get('max_chars', 12000), request_id=key)
        else:
            result = researcher.search(data.get('query'), count=data.get('count', 50),
                                      ori_question=data.get('ori_question'), scope=data.get('scope', 'auto'), request_id=key) if kind == 'search' else researcher.fetch(data.get('url'), request_id=key)
        return success_response(result)
    except QuotaError as exc:
        return error_response(exc.code, str(exc), exc.status)
    except PipelineError as exc:
        status = 429 if exc.code in {'web_daily_limit', 'web_scope_limit', 'web_rate_limit', 'wechat_cooldown', 'wechat_busy'} else (
            409 if exc.code in {'web_idempotency_conflict', 'web_request_uncertain'} else
            400 if exc.code.startswith('web_invalid_') and exc.code != 'web_invalid_response' else
            403 if exc.code in {'FEATURE_FORBIDDEN', 'FORBIDDEN', 'web_disabled'} else 503)
        return error_response(exc.code.upper(), str(exc), status)
    except (ValueError, OSError):
        return error_response('WEB_SETTINGS_INVALID', '联网工具配置需要管理员检查。', 503)


@wuyu_workflow_bp.post('/<project_id>/workflow/web/search')
def workflow_web_search(project_id):
    return _web_operation(project_id, 'search')


@wuyu_workflow_bp.post('/<project_id>/workflow/web/fetch')
def workflow_web_fetch(project_id):
    return _web_operation(project_id, 'fetch')


@wuyu_workflow_bp.post('/<project_id>/workflow/wechat/search')
def workflow_wechat_search(project_id):
    return _web_operation(project_id, 'wechat_search')


@wuyu_workflow_bp.post('/<project_id>/workflow/wechat/article')
def workflow_wechat_article(project_id):
    return _web_operation(project_id, 'wechat_fetch')


@wuyu_workflow_bp.get('/<project_id>/workflow/jobs/<job_id>')
def get_workflow_job(project_id, job_id):
    return _handle(project_id, lambda service: service.get_job(project_id, job_id))


@wuyu_workflow_bp.get('/<project_id>/workflow/artifacts/<artifact_id>')
def get_workflow_artifact(project_id, artifact_id):
    service, error = _service(project_id)
    if error is not None:
        return error
    try:
        path, mime = service.artifact_path(project_id, artifact_id)
        from services.wuyu_delivery_names import delivery_name, download_disposition
        _,body=service._read(project_id)
        artifact=next(a for a in body['artifacts']if a['id']==artifact_id)
        response = send_file(path, mimetype=mime, as_attachment=request.args.get('download') == '1',
                             download_name=delivery_name(artifact, body))
        response.headers['Content-Disposition'] = download_disposition(delivery_name(artifact, body), request.args.get('download') == '1')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
        return response
    except WorkflowAPIError as exc:
        return error_response(exc.code, str(exc), exc.status)


@wuyu_workflow_bp.post('/<project_id>/workflow/relayout')
def prepare_presentation_relayout(project_id):
    """Prepare an existing PPTX locally; no generation, approval or model calls."""
    return _handle(project_id, lambda service: service.prepare_relayout(project_id, request.get_json(silent=True)))
