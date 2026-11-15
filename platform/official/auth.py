"""Email/password accounts; credentials never appear in public user objects."""
import hashlib
import hmac
import math
import re
import secrets
import time
import uuid
from fastapi import HTTPException, Request
from . import store as db

from .runtime_config import settings as runtime

COOKIE = runtime.session_cookie
LEGACY_COOKIE = runtime.legacy_session_cookie
WORK_COOKIE = runtime.work_session_cookie
IDENTITIES = ('政府及事业单位', '企业', '科研院所', '高校教师', '学生', '其他')


def normalize_email(email):
    email = str(email).strip().lower()
    if len(email) > 254 or not re.fullmatch(r'[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+', email):
        raise HTTPException(422, '请输入有效邮箱。')
    return email


def validate_profile(name, organization, identity):
    if not name.strip() or not organization.strip() or len(name) > 60 or len(organization) > 160 or identity not in IDENTITIES:
        raise HTTPException(422, '请填写姓名、机构并选择身份。')


def hash_password(password):
    if not 8 <= len(password) <= 128 or not password.strip():
        raise HTTPException(422, '密码需为 8–128 个字符。')
    return _encode_password(password)


def _encode_password(password):
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=32768, r=8, p=1, maxmem=64*1024*1024, dklen=32)
    return 'scrypt$32768$8$1$' + salt.hex() + '$' + value.hex()


DUMMY_HASH = hash_password(secrets.token_urlsafe(24))


def check_password(password, encoded, *, minimum_length=8):
    try:
        algorithm,n,r,p,salt,expected = encoded.split('$')
        if (algorithm,n,r,p) != ('scrypt','32768','8','1'): return False
        salt = bytes.fromhex(salt); expected = bytes.fromhex(expected)
        if len(salt) != 16 or len(expected) != 32 or not minimum_length <= len(password) <= 128: return False
        value = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=32768, r=8, p=1, maxmem=64*1024*1024, dklen=32)
        return hmac.compare_digest(expected, value)
    except (ValueError, TypeError, AttributeError): return False


def limit_attempts(email, ip, action):
    """Registration keeps its existing per-source hourly attempt limit."""
    if action != 'register':
        raise ValueError('Login attempts require a verification lease')
    now = time.time()
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM password_attempts WHERE created<?', (now-3600,))
        rows = conn.execute("SELECT created FROM password_attempts WHERE ip=? AND action='register' ORDER BY created", (ip,)).fetchall()
        if len(rows) >= 10:
            retry = max(1, math.ceil(rows[len(rows)-10][0] + 3600 - now))
            raise HTTPException(429, '操作过于频繁，请稍后重试。', headers={'Retry-After': str(retry)})
        conn.execute('INSERT INTO password_attempts VALUES(?,?,?,?)', (email,ip,action,now))


def _begin_login(email, ip):
    """Claim capacity atomically without holding SQLite during password/network I/O.

    Old ``login`` rows include successful attempts and cannot identify failures.
    Keep them for the existing retention period; only v2 rows drive this policy.
    The pending lease outlives the legacy identity bridge's 90-second timeout.
    """
    now = time.time()
    token = 'login_pending_v2:' + uuid.uuid4().hex
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DELETE FROM password_attempts WHERE created<?', (now-3600,))
        rows = conn.execute(
            "SELECT email,created,action FROM password_attempts WHERE ip=? AND "
            "((action='login_failed_v2' AND created>?) OR "
            "(substr(action,1,17)='login_pending_v2:' AND created>?))",
            (ip,now-600,now-300)).fetchall()
        retry = 0
        for selected, limit in (([r for r in rows if r['email'] == email], 10), (rows, 60)):
            if len(selected) >= limit:
                expiries = sorted(r['created'] + (600 if r['action'] == 'login_failed_v2' else 300) for r in selected)
                retry = max(retry, math.ceil(expiries[len(selected)-limit] - now))
        if retry:
            raise HTTPException(429, '操作过于频繁，请稍后重试。', headers={'Retry-After': str(max(1,retry))})
        conn.execute('INSERT INTO password_attempts VALUES(?,?,?,?)', (email,ip,token,now))
    return token


def _finish_login(email, ip, token, *, outcome):
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if outcome == 'failed':
            conn.execute("UPDATE password_attempts SET action='login_failed_v2',created=? WHERE email=? AND ip=? AND action=?",
                         (time.time(),email,ip,token))
        else:
            conn.execute('DELETE FROM password_attempts WHERE email=? AND ip=? AND action=?', (email,ip,token))
            if outcome == 'success':
                conn.execute("DELETE FROM password_attempts WHERE email=? AND ip=? AND action='login_failed_v2'", (email,ip))


def _login_attempt(email, ip, verify):
    token = _begin_login(email, ip)
    try:
        user = verify()
    except HTTPException as error:
        _finish_login(email, ip, token, outcome='failed' if error.status_code in (401,403) else 'unavailable')
        raise
    except BaseException:
        _finish_login(email, ip, token, outcome='unavailable')
        raise
    _finish_login(email, ip, token, outcome='success')
    return user


def _password_login(row, password, *, minimum_length=8, message):
    encoded = row.get('password_hash') if row else None
    valid = check_password(password, encoded or DUMMY_HASH, minimum_length=minimum_length)
    if not valid or not encoded or not row['active']:
        raise HTTPException(401, message)
    row.pop('password_hash', None)
    return row


def register(email, password, name, organization, identity, ip):
    email = normalize_email(email); validate_profile(name,organization,identity)
    limit_attempts(email,ip,'register')
    if not db.settings()['registration_open']: raise HTTPException(403, '暂未开放新用户注册。')
    encoded = hash_password(password); now = time.time(); uid = uuid.uuid4().hex
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        setting = conn.execute("SELECT value FROM settings WHERE key='registration_open'").fetchone()
        if setting and setting[0] == 'false': raise HTTPException(403, '暂未开放新用户注册。')
        if conn.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone():
            raise HTTPException(409, '该邮箱已注册，请直接登录。')
        conn.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)', (uid,email,name.strip(),organization.strip(),identity,'user',now))
        conn.execute('INSERT INTO user_passwords VALUES(?,?,?)', (uid,encoded,now))
        user = dict(conn.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone())
    db.audit(uid,'register')
    # Registration remains usable during a work-service outage; work entry retries
    # the idempotent signup grant, so a failed bridge never loses the gift.
    try:
        from . import integrated_identity as identities, identity_bridge
        target = identities.quota_target(uid)
        identity_bridge.request('POST', '/api/auth/internal/identity/credits', {'action': 'provision', 'work_user_id': target}, timeout=5)
    except Exception:
        db.audit(uid, 'signup_points_pending')
    return user


def _alias_user():
    if not runtime.login_alias or not runtime.login_alias_user_id:
        return None
    return db.query(
        "SELECT u.*,p.password_hash FROM users u JOIN identity_accounts a ON a.user_id=u.id "
        "LEFT JOIN user_passwords p ON p.user_id=u.id WHERE u.id=? "
        "AND a.account_type='platform' AND a.linked_to_user_id IS NULL "
        "AND NOT EXISTS(SELECT 1 FROM identity_accounts WHERE legacy_alias=?)",
        (runtime.login_alias_user_id, runtime.login_alias), one=True)


def login(email, password, ip):
    if isinstance(email, str) and runtime.login_alias and email.strip() == runtime.login_alias:
        row = _alias_user()
        # Both entry points share the existing account's rate-limit bucket.
        return _login_attempt(row['email'] if row else 'alias:' + runtime.login_alias, ip,
            lambda: _password_login(row, password, minimum_length=1, message='账号或密码错误，或账号暂不可用。'))
    if isinstance(email,str) and '@' not in email:
        from . import integrated_identity as identities, identity_bridge
        email = email.strip()
        def verify():
            try:return identities.legacy_login(email,password,identity_bridge.verify_legacy)
            except identities.IdentityError as error:raise HTTPException(error.status,error.code) from None
        return _login_attempt('legacy:'+email,ip,verify)
    email = normalize_email(email)
    row = db.query('SELECT u.*,p.password_hash FROM users u LEFT JOIN user_passwords p ON p.user_id=u.id WHERE u.email=?', (email,), one=True)
    return _login_attempt(email,ip,
        lambda: _password_login(row, password, message='邮箱或密码错误，或账号暂不可用。'))


def create_admin(email, password, name, organization):
    """Local operator entry point only; never promoted by public registration."""
    email = normalize_email(email); validate_profile(name,organization,'其他')
    encoded = hash_password(password); uid = uuid.uuid4().hex; now = time.time()
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT id FROM users WHERE role='admin'").fetchone(): raise ValueError('管理员已存在。需要恢复登录时使用 set-password。')
        if conn.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone(): raise ValueError('此邮箱已注册，请使用其他邮箱创建管理员。')
        conn.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)', (uid,email,name.strip(),organization.strip(),'其他','admin',now))
        conn.execute('INSERT INTO user_passwords VALUES(?,?,?)', (uid,encoded,now))
    db.audit(uid,'create_admin'); return uid


def set_password(email, password):
    """Server operator recovery, including accounts from the former OTP version."""
    email = normalize_email(email); encoded = hash_password(password)
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()
        if not row: raise ValueError('账号不存在。')
        uid = row['id']
        conn.execute('INSERT INTO user_passwords(user_id,password_hash,updated) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET password_hash=excluded.password_hash,updated=excluded.updated', (uid,encoded,time.time()))
        conn.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        conn.execute('DELETE FROM password_attempts WHERE email=?',(email,))
    db.audit(uid,'operator_password_reset')


def set_alias_password(alias, password):
    """Local operator only: rotate the declared administrator's credential."""
    if not runtime.login_alias or alias != runtime.login_alias:
        raise ValueError('Login alias is not configured')
    if not isinstance(password, str) or not 1 <= len(password) <= 128 or not password.strip():
        raise ValueError('Alias password must contain 1-128 characters')
    encoded = _encode_password(password)
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute(
            "SELECT u.id,u.email,u.role,u.active FROM users u JOIN identity_accounts a ON a.user_id=u.id "
            "WHERE u.id=? AND a.account_type='platform' AND a.linked_to_user_id IS NULL",
            (runtime.login_alias_user_id,)).fetchone()
        if not row or row['role'] != 'admin' or not row['active']:
            raise ValueError('Declared alias target is not an active platform administrator')
        if conn.execute('SELECT 1 FROM identity_accounts WHERE legacy_alias=?', (alias,)).fetchone():
            raise ValueError('Login alias conflicts with an existing account')
        cursor = conn.execute('UPDATE user_passwords SET password_hash=?,updated=? WHERE user_id=?',
                              (encoded, time.time(), row['id']))
        if cursor.rowcount != 1:
            raise ValueError('Declared administrator has no password credential')
        conn.execute('DELETE FROM sessions WHERE user_id=?', (row['id'],))
        conn.execute('DELETE FROM password_attempts WHERE email=?', (row['email'],))
    db.audit(row['id'], 'operator_alias_password_reset')


def current(request: Request):
    token = request.cookies.get(COOKIE) or request.cookies.get(LEGACY_COOKIE,'')
    user = db.query('SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.hash=? AND s.expires>? AND u.active=1',
                    (db.digest(token), time.time()), one=True) if token else None
    if not user: raise HTTPException(401, '请先登录。')
    from . import integrated_identity
    try:
        resolved=integrated_identity.public_user(user['id'])
        if not resolved['active']:raise HTTPException(401,'账号暂不可用。')
        if request.scope.get('type')=='websocket' and request.url.path.startswith('/api/voice/'):
            integrated_identity.require_feature(resolved,'work' if request.query_params.get('purpose')=='work' else 'chat')
        return resolved
    except integrated_identity.IdentityError as error:raise HTTPException(error.status,error.code) from None


def admin(request: Request):
    user = current(request)
    if user['role'] != 'admin': raise HTTPException(403, '需要系统管理员权限。')
    return user


def clear_cookie(response, name):
    """Clear both the configured domain scope and historical host-only scope."""
    response.delete_cookie(name, path='/', secure=True, httponly=True, samesite='lax')
    if runtime.cookie_domain:
        response.delete_cookie(name, path='/', domain=runtime.cookie_domain, secure=True, httponly=True, samesite='lax')


def issue_session(user, response, secure):
    token = secrets.token_urlsafe(36)
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if not conn.execute('SELECT id FROM users WHERE id=? AND active=1',(user['id'],)).fetchone(): raise HTTPException(401, '账号暂不可用。')
        conn.execute('INSERT INTO sessions VALUES(?,?,?)',(db.digest(token),user['id'],time.time()+7*86400))
    response.set_cookie(COOKIE,token,max_age=7*86400,httponly=True,secure=True,samesite='lax',domain=runtime.cookie_domain,path='/')
    clear_cookie(response, LEGACY_COOKIE)
    db.audit(user['id'],'login')
