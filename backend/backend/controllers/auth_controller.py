"""Authentication controller - login, register, current user"""
import re
import hmac
import logging
import threading

import bcrypt
from flask import Blueprint, current_app, request

from models import db, User
from middleware.auth import generate_token, jwt_required
from utils import success_response, error_response, bad_request

logger = logging.getLogger(__name__)

auth_bp = Blueprint('auth', __name__, url_prefix='/api/auth')

USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,80}$')
MIN_PASSWORD_LEN = 6
REGISTRATION_LOCK = threading.Lock()


def _validate_credentials(username, password):
    if not isinstance(username, str) or not USERNAME_RE.match(username):
        return 'Username must be 3-80 characters: letters, digits, or underscore.'
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        return f'Password must be at least {MIN_PASSWORD_LEN} characters.'
    if len(password.encode('utf-8')) > 72:
        return 'Password must be at most 72 UTF-8 bytes.'
    return None


def _read_credentials(payload):
    if not isinstance(payload, dict):
        return None, None
    username, password = payload.get('username'), payload.get('password')
    if not isinstance(username, str) or not isinstance(password, str):
        return None, None
    return username.strip(), password


def _hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def _check_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))
    except (ValueError, TypeError):
        return False


def _client_address():
    """The caller's address as nginx saw it. X-Real-IP is set by nginx from $remote_addr
    (a client cannot supply it); fall back to the socket peer when running without nginx."""
    return (request.headers.get('X-Real-IP') or request.remote_addr or '').strip()


@auth_bp.route('/register', methods=['POST'])
def register():
    """Register a new user. First-ever user becomes admin."""
    from services.wuyu_identity import enabled
    if enabled():return error_response('CENTRAL_REGISTRATION_REQUIRED','请在无隅 AI 平台完成统一注册。',409)
    payload = request.get_json(silent=True)
    username, password = _read_credentials(payload)

    err = _validate_credentials(username, password)
    if err:
        return bad_request(err)

    with REGISTRATION_LOCK:
        if User.query.filter_by(username=username).first():
            return error_response('USERNAME_TAKEN', 'Username already exists', 409)

        is_first = User.query.count() == 0
        role = 'admin' if is_first else 'employee'

        user = User(
            username=username,
            password_hash=_hash_password(password),
            role=role,
        )
        db.session.add(user)
        db.session.commit()
    logger.info("New user registered: %s (role=%s)", username, role)

    token = generate_token(user.id, user.role)
    return success_response({'token': token, 'user': user.to_dict()}, status_code=201)


@auth_bp.route('/login', methods=['POST'])
def login():
    """Authenticate a user and return a JWT."""
    payload = request.get_json(silent=True)
    username, password = _read_credentials(payload)

    if not username or not password:
        return bad_request('Username and password are required.')

    from services import wuyu_login_throttle as throttle
    address = _client_address()
    wait = throttle.retry_after(username, address)
    if wait:
        response, status = error_response('TOO_MANY_ATTEMPTS', '登录失败次数过多，请稍后再试。', 429)
        response.headers['Retry-After'] = str(wait)
        return response, status

    user = User.query.filter_by(username=username).first()
    if not user or not _check_password(password, user.password_hash):
        throttle.record_failure(username, address)
        return error_response('INVALID_CREDENTIALS', 'Invalid username or password', 401)
    throttle.record_success(username, address)

    from services.wuyu_identity import enabled,lookup,IdentityFailure
    if enabled():
        try:
            identity=lookup(work_user_id=user.id)
            if identity['permissions']['work']is not True:raise IdentityFailure('FEATURE_DISABLED_WORK',403)
        except IdentityFailure as error:return error_response(error.code,'统一账号权限校验未通过。',error.status)
    token = generate_token(user.id, user.role)
    return success_response({'token': token, 'user': user.to_dict()})


@auth_bp.route('/me', methods=['GET'])
@jwt_required
def me():
    """Return information about the authenticated user."""
    return success_response(request.current_user.to_dict())


@auth_bp.route('/workspace-access', methods=['GET'])
@jwt_required
def workspace_access():
    """Keep shared access admin-only; trust a private activated tenant ingress.

    The ingress supplies this server-only credential only after per-user routing
    is active. An account JWT remains mandatory and the current DB role wins.
    """
    secret = current_app.config.get('WUYU_TENANT_ACCESS_KEY', '')
    supplied = request.headers.get('X-Wuyu-Tenant-Access', '')
    tenant_access = (
        isinstance(secret, str)
        and len(secret.encode('utf-8')) >= 32
        and hmac.compare_digest(supplied.encode('utf-8'), secret.encode('utf-8'))
    )
    if request.current_user.role != 'admin' and not tenant_access:
        return error_response(
            'WORKSPACE_NOT_ENABLED',
            '账号已创建，工作空间待开通。请联系管理员。',
            403,
        )
    response = success_response({'workspace_access': True})
    # Nginx auth_request and browsers must recheck role changes immediately.
    if isinstance(response, tuple):
        response[0].headers['Cache-Control'] = 'no-store'
    else:
        response.headers['Cache-Control'] = 'no-store'
    return response


# All internal identity routes require a distinct server credential and are also
# denied at the public reverse proxy. Their payloads never select a model/key.
def _identity_internal():
    from services.wuyu_identity import enabled,shared_key,IdentityFailure
    if not enabled():raise IdentityFailure('IDENTITY_BRIDGE_DISABLED',503)
    supplied=request.headers.get('X-Wuyu-Identity-Key','')
    if not hmac.compare_digest(supplied.encode(),shared_key().encode()):raise IdentityFailure('INTERNAL_IDENTITY_AUTH_REQUIRED',401)

def _identity_result(operation):
    from services.wuyu_identity import IdentityFailure
    try:return operation()
    except IdentityFailure as error:return error_response(error.code,'统一身份服务暂不可用或权限已失效。',error.status)

def _identity_payload(keys,required=None):
    from services.wuyu_identity import IdentityFailure
    value=request.get_json(silent=True)
    if not isinstance(value,dict)or set(value)-set(keys)or any(value.get(name)is None for name in(required or ())):raise IdentityFailure('INVALID_IDENTITY_REQUEST',400)
    import uuid
    for name in ('work_user_id','target_work_user_id'):
        if name in value and value[name] is not None:
            try:
                if not isinstance(value[name],str)or str(uuid.UUID(value[name]))!=value[name]:raise ValueError()
            except(ValueError,TypeError,AttributeError):raise IdentityFailure('INVALID_IDENTITY_REQUEST',400)from None
    if 'session_hash'in value and value['session_hash']is not None and(not isinstance(value['session_hash'],str)or not re.fullmatch('[a-f0-9]{64}',value['session_hash'])):raise IdentityFailure('INVALID_IDENTITY_REQUEST',400)
    return value

def _ensure_identity_user(identity):
    import secrets,uuid
    from services.wuyu_identity import IdentityFailure
    try:work_id=str(uuid.UUID(identity['work_user_id']))
    except(ValueError,KeyError,TypeError):raise IdentityFailure('IDENTITY_SCOPE_INVALID',503)from None
    user=db.session.get(User,work_id)
    if user:return user
    # New platform principals receive a stable separate work UUID. Original work
    # aliases and hashes remain untouched, and no quota row is reset here.
    name='identity_'+work_id.replace('-','')
    user=User(id=work_id,username=name,password_hash=_hash_password(secrets.token_urlsafe(32)),role='employee')
    db.session.add(user)
    try:db.session.commit()
    except Exception:
        db.session.rollback();user=db.session.get(User,work_id)
        if user is None:raise
    return user

@auth_bp.post('/internal/identity/legacy-check')
def identity_legacy_check():
    def operation():
        _identity_internal();data=_identity_payload({'work_user_id','password'},{'work_user_id','password'})
        user=db.session.get(User,data.get('work_user_id'))
        password=data.get('password')
        valid=isinstance(password,str)and len(password.encode('utf-8'))<=72 and user is not None and _check_password(password,user.password_hash)
        return {'verified':bool(valid)}
    return _identity_result(operation)

@auth_bp.post('/internal/identity/principal-status')
def identity_principal_status():
    def operation():
        from services.wuyu_identity import lookup,work_history,IdentityFailure
        from models import Project
        from models.wuyu_quota import QuotaAccount,QuotaDay,QuotaReservation
        _identity_internal();data=_identity_payload({'work_user_id','session_hash'},{'work_user_id'})
        user=db.session.get(User,data.get('work_user_id'))
        if user is None:return {'has_native_records':False}
        if Project.query.filter_by(user_id=user.id).count():return {'has_native_records':True}
        account=QuotaAccount.query.filter_by(user_id=user.id).first()
        if account:
            if any(getattr(account,key)>0 for key in ['pages_granted','points_granted','pages_spent','points_spent','pages_reserved','points_reserved']):return {'has_native_records':True}
            if any(any(getattr(day,key)>0 for key in ['pages_spent','points_spent','pages_reserved','points_reserved'])for day in QuotaDay.query.filter_by(account_id=account.id).all()):return {'has_native_records':True}
            if QuotaReservation.query.filter_by(account_id=account.id,status='reserved').first():return {'has_native_records':True}
        # This server-only metadata check also supports a reverse double-proof
        # link, whose current browser session belongs to the other principal.
        identity=lookup(work_user_id=user.id)
        token=generate_token(user.id,user.role,identity_id=identity['identity_id'],identity_session_hash=data.get('session_hash'))
        items=work_history(token,user.id)
        if any(type(row.get('blank'))is not bool for row in items):raise IdentityFailure('WORK_HISTORY_INVALID',503)
        return {'has_native_records':any(row['blank']is False or row.get('running')is True for row in items)}
    return _identity_result(operation)

@auth_bp.post('/internal/identity/exchange')
def identity_exchange():
    def operation():
        from services.wuyu_identity import lookup,IdentityFailure
        _identity_internal();data=_identity_payload({'session_hash','work_user_id'},{'session_hash'})
        if not data['session_hash']:raise IdentityFailure('INVALID_IDENTITY_REQUEST',400)
        identity=lookup(session_hash=data.get('session_hash'),work_user_id=data.get('work_user_id'))
        if identity['permissions']['work']is not True:raise IdentityFailure('FEATURE_DISABLED_WORK',403)
        user=_ensure_identity_user(identity)
        token=generate_token(user.id,user.role,identity_id=identity['identity_id'],identity_session_hash=data['session_hash'])
        return {'token':token,'work_user_id':user.id}
    return _identity_result(operation)

@auth_bp.get('/sso')
def identity_sso():
    def operation():
        import hashlib
        from flask import redirect
        from services.wuyu_identity import enabled,lookup,IdentityFailure
        if not enabled():raise IdentityFailure('IDENTITY_BRIDGE_DISABLED',503)
        from services.wuyu_identity import validate_destination
        allowed={'next','return_to','work_user_id'}
        if set(request.args)-allowed or any(len(values)!=1 for _,values in request.args.lists())or 'next'in request.args and'return_to'in request.args:raise IdentityFailure('INVALID_SSO_RETURN',400)
        destination=validate_destination(request.args.get('next',request.args.get('return_to','/work')))
        raw=request.cookies.get('wuyu_staging_identity_session','')
        if not raw:raise IdentityFailure('CENTRAL_LOGIN_REQUIRED',401)
        work_id=request.args.get('work_user_id')
        if work_id is not None:
            import uuid
            try:
                if str(uuid.UUID(work_id))!=work_id:raise ValueError()
            except ValueError:raise IdentityFailure('INVALID_SSO_RETURN',400)from None
        sid=hashlib.sha256(raw.encode()).hexdigest();identity=lookup(session_hash=sid,work_user_id=work_id)
        if not identity['permissions']['work']:raise IdentityFailure('FEATURE_DISABLED_WORK',403)
        if destination=='/work/admin'and not identity['permissions']['work_admin']:raise IdentityFailure('FEATURE_DISABLED_WORK_ADMIN',403)
        user=_ensure_identity_user(identity)
        token=generate_token(user.id,user.role,identity_id=identity['identity_id'],identity_session_hash=sid)
        if destination=='/work?templates=review'and not identity['permissions']['template_review']:raise IdentityFailure('FEATURE_DISABLED_TEMPLATE_REVIEW',403)
        result=redirect(destination,302)
        result.set_cookie('banana_staging_auth_token',token,httponly=True,secure=True,samesite='Lax',path='/',max_age=3500)
        result.headers['Cache-Control']='no-store';result.headers['Referrer-Policy']='no-referrer'
        return result
    return _identity_result(operation)


@auth_bp.post('/internal/identity/grant')
def identity_grant():
    def operation():
        from services.wuyu_identity import lookup,IdentityFailure
        from services.wuyu_quota import get_quota_service,QuotaError
        _identity_internal();data=_identity_payload({'session_hash','target_work_user_id','pages','points','reason','idempotency_key'},{'session_hash','target_work_user_id','pages','points','reason','idempotency_key'})
        actor=lookup(session_hash=data.get('session_hash'))
        if actor['permissions'].get('work_quota_admin')is not True:raise IdentityFailure('WORK_QUOTA_ADMIN_REQUIRED',403)
        target=lookup(work_user_id=data.get('target_work_user_id'))
        actor_user=_ensure_identity_user(actor);target_user=_ensure_identity_user(target)
        try:result=get_quota_service().adjust(actor_user.id,target_user.id,data.get('pages'),data.get('points'),data.get('reason'),data.get('idempotency_key'))
        except QuotaError as error:return error_response(error.code,str(error),error.status)
        return {'data':result}
    return _identity_result(operation)


@auth_bp.post('/internal/identity/credits')
def identity_credits():
    """Server-only provisioning and bounded work-voice charges, never browser prices."""
    def operation():
        from services.wuyu_identity import lookup, IdentityFailure
        from services.wuyu_quota import get_quota_service, QuotaError
        from models.wuyu_quota import QuotaReservation
        _identity_internal()
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise IdentityFailure('INVALID_IDENTITY_REQUEST', 400)
        action = data.get('action')
        fields = {'action', 'work_user_id'} | ({'operation_key'} if action == 'reserve' else {'reservation_id'} if action in {'settle', 'refund'} else set())
        if action not in {'provision', 'reserve', 'settle', 'refund'} or set(data) != fields:
            raise IdentityFailure('INVALID_IDENTITY_REQUEST', 400)
        if any(not isinstance(data[field], str) or not data[field] or len(data[field]) > (128 if field == 'operation_key' else 36) for field in fields - {'action'}):
            raise IdentityFailure('INVALID_IDENTITY_REQUEST', 400)
        service = get_quota_service()
        try:
            if action in {'provision', 'reserve'}:
                principal = lookup(work_user_id=data['work_user_id'])
                if not principal['permissions']['work']:
                    raise IdentityFailure('FEATURE_DISABLED_WORK', 403)
                user = _ensure_identity_user(principal)
                if action == 'provision':
                    return {'data': service.summary(user.id)}
                return {'data': service.reserve(user.id, 'voice:' + data['operation_key'], 'voice')}
            row = db.session.get(QuotaReservation, data['reservation_id'])
            if row is None or row.user_id != data['work_user_id'] or row.kind != 'voice':
                raise IdentityFailure('NOT_FOUND', 404)
            return {'data': service.settle(row.id) if action == 'settle' else service.refund(row.id, reason='voice_no_accepted_result')}
        except QuotaError as error:
            return error_response(error.code, str(error), error.status)
    return _identity_result(operation)


@auth_bp.get('/internal/identity/history')
@jwt_required
def identity_history():
    def operation():
        from middleware.auth import _extract_token
        from services.wuyu_identity import work_history
        _identity_internal()
        return {'items':work_history(_extract_token(),request.current_user.id)}
    return _identity_result(operation)
