"""Admin controller - user management endpoints (admin-only)"""
import logging

from flask import Blueprint, request, current_app

from models import db, User, Project
from middleware.auth import admin_required
from controllers.auth_controller import (
    _validate_credentials,
    _hash_password,
    _read_credentials,
)
from utils import success_response, error_response, bad_request, not_found

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__, url_prefix='/api/admin')


@admin_bp.route('/workflow-settings', methods=['GET', 'PUT'])
@admin_required
def workflow_settings():
    """Manage PPT image profiles; credentials are write-only and revisions immutable."""
    from services.wuyu_runtime_settings import get_runtime_settings, update_runtime_settings, SettingsConflict
    try:
        if request.method == 'GET':
            return success_response(get_runtime_settings(current_app.config))
        return success_response(update_runtime_settings(current_app.config, request.get_json(silent=True)))
    except SettingsConflict as exc:
        return error_response('SETTINGS_CONFLICT', str(exc), 409)
    except ValueError as exc:
        return bad_request(str(exc))
    except OSError:
        return error_response('SETTINGS_STORAGE_ERROR', '运行配置暂时无法保存，请稍后重试。', 503)


@admin_bp.route('/users', methods=['GET'])
@admin_required
def list_users():
    from services.wuyu_quota import get_quota_service, QuotaError
    from controllers.wuyu_quota_controller import _number, _arguments
    try:
        _arguments({'q', 'limit', 'offset'})
        limit, offset = _number('limit', 50, 100), _number('offset', 0, 1_000_000)
        query = User.query
        needle = request.args.get('q', '').strip()[:80]
        if needle:
            query = query.filter(User.username.contains(needle, autoescape=True))
        total = query.count()
        users = query.order_by(User.created_at.desc(), User.id).limit(limit).offset(offset).all()
        service = get_quota_service()
        return success_response({'users': [{**u.to_dict(), 'quota': service.summary(u.id)} for u in users],
                                 'total': total, 'limit': limit, 'offset': offset})
    except QuotaError as error:
        return error_response(error.code, str(error), error.status)


@admin_bp.route('/users', methods=['POST'])
@admin_required
def create_user():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return bad_request('Expected an object.')
    username, password = _read_credentials(payload)
    role = payload.get('role') or 'employee'

    if role not in ('admin', 'employee'):
        return bad_request('Role must be "admin" or "employee".')

    err = _validate_credentials(username, password)
    if err:
        return bad_request(err)

    if User.query.filter_by(username=username).first():
        return error_response('USERNAME_TAKEN', 'Username already exists', 409)

    user = User(
        username=username,
        password_hash=_hash_password(password),
        role=role,
    )
    db.session.add(user)
    db.session.commit()
    logger.info("Admin %s created user: %s (role=%s)", request.current_user.username, username, role)
    return success_response(user.to_dict(), status_code=201)


@admin_bp.route('/users/<user_id>', methods=['DELETE'])
@admin_required
def delete_user(user_id: str):
    if user_id == request.current_user.id:
        return error_response('CANNOT_DELETE_SELF', 'You cannot delete your own account', 400)

    user = User.query.get(user_id)
    if not user:
        return not_found('User')

    # Orphan their projects rather than cascade-deleting
    Project.query.filter_by(user_id=user_id).update({'user_id': None})
    # Preserve immutable credit history while making the deleted account inaccessible.
    from models.wuyu_quota import QuotaAccount
    QuotaAccount.query.filter_by(user_id=user_id).update({'user_id': None})
    from models.wuyu_workflow import WuyuWorkflowJob
    WuyuWorkflowJob.query.filter_by(actor_user_id=user_id).update({'actor_user_id': None})

    db.session.delete(user)
    db.session.commit()
    logger.info("Admin %s deleted user: %s", request.current_user.username, user.username)
    return success_response({'message': 'User deleted'})
