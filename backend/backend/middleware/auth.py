"""JWT authentication middleware and decorators"""
import time
from functools import wraps

import jwt
from flask import request, jsonify, current_app, has_app_context

from config import Config


def generate_token(user_id: str, role: str, *, identity_id=None, identity_session_hash=None) -> str:
    """Generate a JWT for a user."""
    payload = {
        'sub': user_id,
        'role': role,
        'iat': int(time.time()),
        'exp': int(time.time()) + (current_app.config.get('JWT_ACCESS_TOKEN_EXPIRES', Config.JWT_ACCESS_TOKEN_EXPIRES) if has_app_context() else Config.JWT_ACCESS_TOKEN_EXPIRES),
    }
    if identity_id is not None:payload['identity_id']=identity_id
    if identity_session_hash is not None:payload['identity_session_hash']=identity_session_hash
    config = current_app.config if has_app_context() else {}
    return jwt.encode(payload, config.get('JWT_SECRET_KEY') or Config.JWT_SECRET_KEY,
                      algorithm=config.get('JWT_ALGORITHM', Config.JWT_ALGORITHM))


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises jwt exceptions on failure."""
    config = current_app.config if has_app_context() else {}
    return jwt.decode(token, config.get('JWT_SECRET_KEY') or Config.JWT_SECRET_KEY,
                      algorithms=[config.get('JWT_ALGORITHM', Config.JWT_ALGORITHM)])


def _extract_token() -> str | None:
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:].strip() or None
    cookie_token = request.cookies.get('banana_staging_auth_token', '').strip()
    if cookie_token:
        return cookie_token
    return None


def _load_current_user():
    """Returns (user, error_response). On success, also sets request.current_user."""
    token = _extract_token()
    if not token:
        return None, (jsonify({'success': False, 'error': {'code': 'UNAUTHORIZED', 'message': 'Missing auth token'}}), 401)
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        return None, (jsonify({'success': False, 'error': {'code': 'TOKEN_EXPIRED', 'message': 'Token expired'}}), 401)
    except jwt.InvalidTokenError:
        return None, (jsonify({'success': False, 'error': {'code': 'INVALID_TOKEN', 'message': 'Invalid token'}}), 401)

    from models import User
    user = User.query.get(payload.get('sub'))
    if not user:
        return None, (jsonify({'success': False, 'error': {'code': 'USER_NOT_FOUND', 'message': 'User not found'}}), 401)
    try:
        from services.wuyu_identity import authorize_user, IdentityFailure
        user=authorize_user(user,payload)
    except IdentityFailure as error:
        message='请在无隅 AI 平台的统一设置中创建或停用账号，原工作记录和额度将保留。'if error.code=='CENTRAL_IDENTITY_MANAGED'else'统一账号权限校验未通过。'
        return None,(jsonify({'success':False,'error':{'code':error.code,'message':message}}),error.status)
    request.current_user = user
    return user, None


def jwt_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user, err = _load_current_user()
        if err is not None:
            return err
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user, err = _load_current_user()
        if err is not None:
            return err
        if user.role != 'admin':
            return jsonify({'success': False, 'error': {'code': 'FORBIDDEN', 'message': 'Admin access required'}}), 403
        return f(*args, **kwargs)
    return decorated


def has_feature(user,feature):
    from services.wuyu_identity import has_feature as check
    return check(user,feature)


def feature_required(feature):
    def decorator(f):
        @wraps(f)
        def guarded(*args,**kwargs):
            user,error=_load_current_user()
            if error is not None:return error
            try:
                if not has_feature(user,feature):return jsonify({'success':False,'error':{'code':'FEATURE_DISABLED','message':'此账号尚未开启该功能。'}}),403
            except Exception as error:
                from services.wuyu_identity import IdentityFailure
                if not isinstance(error,IdentityFailure):raise
                return jsonify({'success':False,'error':{'code':error.code,'message':'统一账号权限校验未通过。'}}),error.status
            return f(*args,**kwargs)
        return guarded
    return decorator
