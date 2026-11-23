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


def habits(action):
    return handle(lambda _: action(current_app.extensions['wuyu_design_habits']))


@wuyu_design_memory_bp.get('/habits')
@feature_required('work')
def list_habits():
    return habits(lambda service: service.list(request.current_user.id))


@wuyu_design_memory_bp.post('/habits')
@feature_required('work')
def create_habit():
    return habits(lambda service: service.create(request.current_user.id, request.get_json(silent=True)))


@wuyu_design_memory_bp.post('/habits/onboarding')
@feature_required('work')
def seed_onboarding_habits():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'items'}:
        return error_response('INVALID_REQUEST', '请提供初始设置时选择的习惯。', 400)
    return habits(lambda service: service.seed_onboarding(request.current_user.id, payload['items']))


@wuyu_design_memory_bp.patch('/habits/<habit_id>')
@feature_required('work')
def update_habit(habit_id):
    return habits(lambda service: service.update(request.current_user.id, habit_id, request.get_json(silent=True)))


@wuyu_design_memory_bp.delete('/habits/<habit_id>')
@feature_required('work')
def delete_habit(habit_id):
    return habits(lambda service: service.delete(request.current_user.id, habit_id))


@wuyu_design_memory_bp.put('/habits/settings')
@feature_required('work')
def habit_settings():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'paused'} or type(payload['paused']) is not bool:
        return error_response('INVALID_REQUEST', '请指定是否暂停使用设计习惯。', 400)
    return habits(lambda service: service.set_paused(request.current_user.id, payload['paused']))


@wuyu_design_memory_bp.post('/habits/reset')
@feature_required('work')
def reset_habits():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'confirm'} or payload['confirm'] is not True:
        return error_response('INVALID_REQUEST', '请确认清空全部设计习惯。', 400)
    return habits(lambda service: service.reset(request.current_user.id))


@wuyu_design_memory_bp.post('/habits/notices')
@feature_required('work')
def respond_habit_notice():
    """op196: the conversation told the user about learned habits (announced), or the user undid them (undo)."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {'habit_ids', 'action'}:
        return error_response('INVALID_REQUEST', '请提供习惯编号与操作。', 400)
    from services.wuyu_habit_learning import get_learner
    return habits(lambda _: get_learner(current_app).respond(request.current_user.id, payload['habit_ids'], payload['action']))


@wuyu_design_memory_bp.get('/habits/export')
@wuyu_design_memory_bp.get('/habits/export/<cursor>')
@feature_required('work')
def export_habits(cursor=None):
    # Paged: an answer that carries "next" continues at /habits/export/<next>. The position travels in the
    # path because the platform bridge forwards no query strings.
    return habits(lambda service: service.export(request.current_user.id, cursor))
