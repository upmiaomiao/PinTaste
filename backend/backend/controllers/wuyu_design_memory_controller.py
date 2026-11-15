"""Administrator-only system memory management; private user design retrieval."""
from flask import Blueprint, current_app, request
from middleware.auth import feature_required
from services.wuyu_design_memory import DesignMemoryError
from services.wuyu_quota import QuotaError
from services.wuyu_identity import IdentityFailure
from utils import success_response, error_response

wuyu_design_memory_bp = Blueprint('wuyu_design_memory', __name__, url_prefix='/api/design-memory')


def handle(action):
    try:
        return success_response(action(current_app.extensions['wuyu_design_memory']))
    except (DesignMemoryError, QuotaError) as exc:
        return error_response(exc.code, str(exc), exc.status)
    except IdentityFailure as exc:
        return error_response(exc.code, '统一账号权限校验未通过。', exc.status)


@wuyu_design_memory_bp.get('/admin')
@feature_required('work_admin')
def settings():
    return handle(lambda service: service.admin_view())


@wuyu_design_memory_bp.put('/admin')
@feature_required('work_admin')
def update():
    return handle(lambda service: service.save_policy(request.current_user.id, request.get_json(silent=True)))


@wuyu_design_memory_bp.post('/admin/compile')
@feature_required('work_admin')
def compile_system():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'request_id'}:
        return error_response('INVALID_REQUEST', '请提供请求标识。', 400)
    return handle(lambda service: service.compile(request.current_user.id, 'system', payload['request_id']))


@wuyu_design_memory_bp.post('/admin/review')
@feature_required('work_admin')
def review():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'rule_set_id', 'action'} or not isinstance(payload['rule_set_id'], str):
        return error_response('INVALID_REQUEST', '请指定规则版本及审核操作。', 400)
    return handle(lambda service: service.review(request.current_user.id, payload['rule_set_id'], payload['action']))


@wuyu_design_memory_bp.post('/retrieve')
@feature_required('work')
def retrieve():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'query'} or not isinstance(payload['query'], str) or len(payload['query']) > 4000:
        return error_response('INVALID_REQUEST', '请提供设计查询。', 400)
    return handle(lambda service: {'version':1,'rules':service.retrieve(request.current_user.id, payload['query'], include_system=False)['rules']})
