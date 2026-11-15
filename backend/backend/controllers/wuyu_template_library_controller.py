"""Authenticated original local previews and administrator-reviewed sharing."""
from flask import Blueprint, current_app, request, send_file

from middleware.auth import jwt_required, feature_required
from services.wuyu_identity import IdentityFailure
from utils import success_response, error_response
from services.wuyu_template_library import get_template_library, TemplateLibraryError, AXES
from services.wuyu_quota import QuotaError
from services.wuyu_design_memory import DesignMemoryError
from controllers.wuyu_quota_controller import _same_origin_cookie_write

wuyu_template_library_bp = Blueprint('wuyu_template_library', __name__, url_prefix='/api/template-library')


def _handle(operation):
    try:
        return success_response(operation(get_template_library(current_app._get_current_object())))
    except (TemplateLibraryError, QuotaError, DesignMemoryError) as exc:
        return error_response(exc.code, str(exc), exc.status)
    except IdentityFailure as exc:
        return error_response(exc.code, '统一账号权限校验未通过。', exc.status)


def _filters(submissions=False):
    allowed = {*AXES, 'source_type', 'search', 'limit', 'offset', 'scope'} | ({'status'} if submissions else set())
    if set(request.args) - allowed:
        raise TemplateLibraryError('不支持的模板筛选参数。')
    values = dict(request.args)
    for key, default, minimum, maximum in (('limit', 12, 1, 50), ('offset', 0, 0, 1000000)):
        value = values.get(key, str(default))
        if not value.isdigit() or not minimum <= int(value) <= maximum:
            raise TemplateLibraryError('分页参数超出范围。')
        values[key] = int(value)
    if len(values.get('search', '')) > 200:
        raise TemplateLibraryError('筛选关键词限200字。')
    return values


@wuyu_template_library_bp.get('')
@wuyu_template_library_bp.get('/')
@jwt_required
def templates():
    return _handle(lambda service: service.list(request.current_user, _filters()))


@wuyu_template_library_bp.get('/personal')
@feature_required('work')
def personal_templates():
    return _handle(lambda _: {'items': current_app.extensions['wuyu_personal_templates'].list(request.current_user.id)})


@wuyu_template_library_bp.post('/personal')
@feature_required('work')
def save_personal_template():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'project_id', 'expected_revision', 'title'} or not isinstance(payload['project_id'], str):
        return error_response('INVALID_REQUEST', '请指定自己的已完成项目与名称。', 400)
    return _handle(lambda _: current_app.extensions['wuyu_personal_templates'].save(request.current_user.id,
        payload['project_id'], payload['expected_revision'], payload['title']))


@wuyu_template_library_bp.get('/submissions')
@jwt_required
def submissions():
    return _handle(lambda service: service.list(request.current_user, _filters(True), submissions=True))


@wuyu_template_library_bp.get('/recommendations')
@jwt_required
def recommendations():
    if set(request.args) != {'project_id'}:
        return error_response('INVALID_REQUEST', '请指定当前项目。', 400)
    from models import Project
    project = Project.query.filter_by(id=request.args['project_id']).first()
    actor = request.current_user
    if project is None or project.user_id != actor.id and actor.role != 'admin':
        return error_response('NOT_FOUND', '项目不存在。', 404)
    return _handle(lambda service: service.recommend(actor,
        current_app.extensions['wuyu_workflow'].snapshot(project.id)['brief']))


@wuyu_template_library_bp.post('/submissions')
@feature_required('template_submit')
def submit_template():
    def operation(service):
        _same_origin_cookie_write()
        result = service.submit(request.current_user, request.get_json(silent=True), request.headers.get('Idempotency-Key'))
        service.enqueue_auto_tags(result['id'])
        return result
    return _handle(operation)


@wuyu_template_library_bp.get('/<template_id>')
@jwt_required
def template_detail(template_id):
    if template_id.startswith('uploaded:'):
        from services.wuyu_uploaded_templates import UploadedTemplates
        return _handle(lambda _: UploadedTemplates(current_app).detail(request.current_user.id, template_id))
    if template_id.startswith('personal:'):
        return _handle(lambda _: current_app.extensions['wuyu_personal_templates'].card(
            current_app.extensions['wuyu_personal_templates']._record(request.current_user.id, template_id)))
    return _handle(lambda service: service.detail(template_id, request.current_user))


@wuyu_template_library_bp.get('/<template_id>/previews/<int:page_number>.png')
@jwt_required
def template_preview(template_id, page_number):
    try:
        from services.wuyu_uploaded_templates import UploadedTemplates
        path = (UploadedTemplates(current_app).preview(request.current_user.id, template_id, page_number)
                if template_id.startswith('uploaded:') else current_app.extensions['wuyu_personal_templates'].preview(request.current_user.id, template_id, page_number)
                if template_id.startswith('personal:') else
                get_template_library(current_app._get_current_object()).preview_path(template_id, page_number, request.current_user))
        # Local rasters are preserved byte-for-byte, including JPEG/WebP input.
        # The URL remains compatible; Content-Type describes the actual file.
        response = send_file(path, conditional=True)
        response.headers['Cache-Control'] = 'private, no-cache'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    except (TemplateLibraryError, DesignMemoryError) as exc:
        return error_response(exc.code, str(exc), exc.status)
    except IdentityFailure as exc:
        return error_response(exc.code, '统一账号权限校验未通过。', exc.status)


@wuyu_template_library_bp.post('/<template_id>/review')
@feature_required('template_review')
def review_template(template_id):
    def operation(service):
        _same_origin_cookie_write()
        return service.review(template_id, request.current_user, request.get_json(silent=True), request.headers.get('Idempotency-Key'))
    return _handle(operation)


@wuyu_template_library_bp.get('/<template_id>/audit')
@feature_required('template_review')
def template_audit(template_id):
    return _handle(lambda service: service.audit(template_id, request.current_user))


@wuyu_template_library_bp.post('/tag-queue')
@feature_required('template_review')
def tag_templates():
    def operation(service):
        _same_origin_cookie_write()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) - {'dry_run', 'max_items', 'template_id', 'retry_failed'}:
            raise TemplateLibraryError('标签队列仅接受dry_run、max_items、template_id和retry_failed。')
        return service.tag_queue(request.current_user, payload.get('dry_run', True), payload.get('max_items', 1), payload.get('template_id'), payload.get('retry_failed', False))
    return _handle(operation)
