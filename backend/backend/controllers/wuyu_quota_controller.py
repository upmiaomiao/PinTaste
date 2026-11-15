"""Authenticated quota views and admin-only, idempotent persistent top-ups."""
from urllib.parse import urlsplit
from flask import Blueprint, request, current_app
from middleware.auth import jwt_required, feature_required
from services.wuyu_quota import QuotaError, get_quota_service
from utils import success_response, error_response

wuyu_quota_bp = Blueprint('wuyu_quota', __name__, url_prefix='/api')


def _handle(operation):
    try:
        return success_response(operation(get_quota_service()))
    except QuotaError as error:
        return error_response(error.code, str(error), error.status)


def _arguments(allowed=()):
    if set(request.args) - set(allowed):
        raise QuotaError('QUOTA_REQUEST_INVALID', '此接口不接受账号或计费规则覆盖参数。')


def _number(name, default, maximum):
    value = request.args.get(name, str(default))
    if not value.isdigit() or not (1 if name == 'limit' else 0) <= int(value) <= maximum:
        raise QuotaError('QUOTA_REQUEST_INVALID', '分页参数超出允许范围。')
    return int(value)


def _same_origin_cookie_write():
    if request.headers.get('Authorization', '').startswith('Bearer '):
        return
    try:
        origin = urlsplit(request.headers.get('Origin', ''))
        target = urlsplit(current_app.config.get('WUYU_PUBLIC_ORIGIN') or request.host_url)
        normalized = lambda item: (item.scheme.lower(), item.hostname, item.port or (443 if item.scheme == 'https' else 80))
        if origin.scheme not in {'http', 'https'} or origin.username or origin.password or normalized(origin) != normalized(target):
            raise ValueError()
    except ValueError:
        raise QuotaError('REQUEST_ORIGIN_DENIED', '请从当前网站提交操作请求。', 403) from None


@wuyu_quota_bp.get('/auth/quota')
@jwt_required
def own_quota():
    def operation(service):
        _arguments()
        return service.summary(request.current_user.id)
    return _handle(operation)


@wuyu_quota_bp.get('/auth/quota/ledger')
@jwt_required
def own_ledger():
    def operation(service):
        _arguments({'limit', 'offset'})
        return service.ledger(request.current_user.id, _number('limit', 50, 100), _number('offset', 0, 1_000_000))
    return _handle(operation)


@wuyu_quota_bp.post('/auth/quota/redeem')
@jwt_required
def redeem_code():
    def operation(service):
        _same_origin_cookie_write(); _arguments()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {'code'} or not isinstance(payload['code'], str) or len(payload['code']) > 40:
            raise QuotaError('QUOTA_REQUEST_INVALID', '请输入兑换码。')
        return service.redeem(request.current_user.id, payload['code'])
    return _handle(operation)


@wuyu_quota_bp.get('/admin/users/<user_id>/quota')
@feature_required('work_quota_admin')
def user_quota(user_id):
    def operation(service):
        _arguments()
        return service.summary(user_id)
    return _handle(operation)


@wuyu_quota_bp.post('/admin/users/<user_id>/quota/adjustments')
@feature_required('work_quota_admin')
def user_quota_adjustment(user_id):
    def operation(service):
        _same_origin_cookie_write(); _arguments()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {'pages', 'points', 'reason'}:
            raise QuotaError('QUOTA_REQUEST_INVALID', '加额请求只接受页数、积分和说明。')
        return service.adjust(request.current_user.id, user_id, payload['pages'], payload['points'],
                              payload['reason'], request.headers.get('Idempotency-Key'))
    return _handle(operation)


@wuyu_quota_bp.get('/admin/quota/overview')
@feature_required('work_quota_admin')
def quota_overview():
    return _handle(lambda service: service.admin_overview(request.current_user.id))


@wuyu_quota_bp.post('/admin/quota/command')
@feature_required('work_quota_admin')
def admin_quota_command():
    def operation(service):
        _same_origin_cookie_write(); _arguments()
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise QuotaError('QUOTA_REQUEST_INVALID', '积分操作无效。')
        action = data.get('action')
        if action == 'ledger' and set(data) == {'action', 'user_id', 'limit', 'offset'}:
            return service.ledger(data['user_id'], data['limit'], data['offset'])
        if action == 'codes_create' and set(data) <= {'action', 'count', 'points', 'expires_at', 'note'} and {'count', 'points'} <= set(data):
            return service.codes_create(request.current_user.id, data['count'], data['points'], data.get('expires_at'), data.get('note', ''))
        if action == 'codes_list' and set(data) <= {'action', 'limit'}:
            return service.codes_list(request.current_user.id, data.get('limit', 50))
        if action == 'codes_revoke' and set(data) == {'action', 'batch_id'}:
            return service.codes_revoke(request.current_user.id, data['batch_id'])
        if action == 'adjustment' and set(data) == {'action', 'user_id', 'points', 'reason', 'idempotency_key'}:
            return service.adjust(request.current_user.id, data['user_id'], 0, data['points'], data['reason'], data['idempotency_key'])
        fields = {'action', 'settings', 'expected_revision', 'reason', 'idempotency_key'}
        if action == 'control':
            fields.add('user_id')
        if action not in {'policy', 'control'} or set(data) != fields:
            raise QuotaError('QUOTA_REQUEST_INVALID', '积分操作字段无效。')
        return service.admin_change(request.current_user.id, **data)
    return _handle(operation)
