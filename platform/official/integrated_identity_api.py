"""Typed public identity APIs and authenticated server-only introspection."""

import hmac, json, time, re, uuid
import anyio
from urllib.parse import urlsplit, parse_qsl, urlencode, quote
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, SecretStr
from . import (
    store as db,
    auth,
    integrated_identity as identity,
    identity_bridge as bridge,
)

from .runtime_config import settings as runtime

router = APIRouter()


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkSessionInput(Input):
    next: str = Field(default="/work", max_length=200)
    work_user_id: str | None = None


class PermissionInput(Input):
    user_ids: list[str] = Field(min_length=1, max_length=500)
    patch: dict[str, StrictBool]
    idempotency_key: str = Field(min_length=1, max_length=128)


class RoleInput(Input):
    user_id: str
    role: Literal["admin", "user"]
    idempotency_key: str = Field(min_length=1, max_length=128)


class Lookup(Input):
    session_hash: str | None = None
    work_user_id: str | None = None


class LinkInput(Input):
    alias: str = Field(min_length=3, max_length=80)
    password: SecretStr = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


class WorkLinkInput(Input):
    username: str = Field(min_length=3, max_length=80)
    password: SecretStr = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


class PlatformLinkInput(Input):
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


class GrantInput(Input):
    user_id: str
    pages: int = Field(default=0, ge=0, le=0, strict=True)
    points: int = Field(ge=0, le=100000000, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=200)


class BulkGrantInput(Input):
    user_ids: list[str] = Field(min_length=1, max_length=100)
    pages: int = Field(default=0, ge=0, le=0, strict=True)
    points: int = Field(ge=0, le=10000000, strict=True)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,100}$')
    reason: str = Field(min_length=1, max_length=200)


class EmailGrantInput(Input):
    targets: list[str] = Field(min_length=1, max_length=200)
    points: int = Field(ge=-10000000, le=10000000, strict=True)
    reason: str = Field(default='', max_length=200)
    idempotency_key: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,100}$')


class RedeemInput(Input):
    code: str = Field(min_length=4, max_length=40)


class DesignPolicyInput(Input):
    expected_revision: int = Field(ge=0, strict=True)
    settings: dict


class DesignCompileInput(Input):
    request_id: str = Field(pattern=r'^[a-zA-Z0-9_.:-]{1,100}$')


class DesignReviewInput(Input):
    rule_set_id: str = Field(max_length=36)
    action: Literal['activate', 'disable']


HabitScope = dict[Literal['purpose', 'page_role', 'content_type'], str]


class DesignHabitInput(Input):
    category: Literal['integration', 'layout', 'expression']
    slot: str = Field(pattern=r'^[a-z][a-z0-9_]{0,59}$')
    rule: str = Field(min_length=1, max_length=300)
    scope: HabitScope | None = None
    value: dict | None = None
    pinned: StrictBool | None = None


class DesignHabitOnboardingItem(Input):
    category: Literal['integration', 'layout', 'expression']
    slot: str = Field(pattern=r'^[a-z][a-z0-9_]{0,59}$')
    rule: str = Field(min_length=1, max_length=300)
    scope: HabitScope | None = None
    value: dict | None = None


class DesignHabitOnboardingInput(Input):
    # As many picks as the work service applies to one generation.
    items: list[DesignHabitOnboardingItem] = Field(min_length=1, max_length=12)


class DesignHabitPatchInput(Input):
    version: int = Field(ge=1, strict=True)
    rule: str | None = Field(default=None, min_length=1, max_length=300)
    scope: HabitScope | None = None
    value: dict | None = None
    pinned: StrictBool | None = None
    paused: StrictBool | None = None
    status: Literal['active', 'revoked'] | None = None


class DesignHabitSettingsInput(Input):
    paused: StrictBool


class DesignHabitResetInput(Input):
    confirm: StrictBool


def invoke(action):
    try:
        return action()
    except identity.IdentityError as e:
        message = getattr(e, 'user_message', None)
        # detail stays the code for existing clients; the readable reason travels alongside it.
        raise HTTPException(e.status, e.code, headers={'X-Wuyu-Message': quote(message)} if message else None) from None


def session_hash(request):
    raw = request.cookies.get(auth.COOKIE) or request.cookies.get(
        auth.LEGACY_COOKIE, ""
    )
    if not raw:
        raise HTTPException(401, "SESSION_REQUIRED")
    return db.digest(raw)


def bridge_auth(request):
    supplied = request.headers.get("X-Wuyu-Identity-Key", "")
    try:
        expected = bridge.key()
    except identity.IdentityError as e:
        raise HTTPException(e.status, e.code) from None
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "INTERNAL_IDENTITY_AUTH_REQUIRED")


@router.post("/api/internal/identity/introspect")
async def introspect(body: Lookup, request: Request):
    # A synchronous outer /work call waits for the work backend, which calls
    # this endpoint back. Its DB/key IO must not compete for the same default
    # worker tokens held by those outer calls. Keep IO off the event loop and
    # bound this separate authority lane to eight workers per application.
    limiter = getattr(request.app.state, "identity_lookup_limiter", None)
    if limiter is None:
        limiter = anyio.CapacityLimiter(8)
        request.app.state.identity_lookup_limiter = limiter

    def inspect():
        bridge_auth(request)
        return invoke(
            lambda: identity.introspect(
                session_hash=body.session_hash, work_user_id=body.work_user_id
            )
        )

    return await anyio.to_thread.run_sync(inspect, limiter=limiter)


@router.get("/api/admin/integrated-users")
def users(user=Depends(auth.admin)):
    return invoke(lambda: identity.list_users(user["id"]))


@router.post("/api/admin/integrated-users/permissions")
def permissions(body: PermissionInput, user=Depends(auth.admin)):
    return invoke(
        lambda: identity.change_permissions(
            user["id"], body.user_ids, body.patch, body.idempotency_key
        )
    )


@router.post("/api/admin/integrated-users/role")
def role(body: RoleInput, user=Depends(auth.admin)):
    return invoke(
        lambda: identity.change_role(
            user["id"], body.user_id, body.role, body.idempotency_key
        )
    )


@router.post("/api/identity/link")
def link(body: LinkInput, request: Request, user=Depends(auth.current)):
    return invoke(
        lambda: identity.link_legacy(
            user["id"],
            body.alias,
            body.password.get_secret_value(),
            body.idempotency_key,
            bridge.verify_legacy,
            lambda wid: bridge.has_work_records(wid, session_hash(request)),
        )
    )


def validate_destination(destination):
    if (
        not isinstance(destination, str)
        or len(destination) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in destination)
    ):
        raise identity.IdentityError("INVALID_WORK_DESTINATION")
    value = urlsplit(destination)
    if (
        value.scheme
        or value.netloc
        or value.fragment
        or value.path not in ("/work", "/admin", "/work/admin")
        or "\\" in destination
    ):
        raise identity.IdentityError("INVALID_WORK_DESTINATION")
    pairs = parse_qsl(value.query, keep_blank_values=True)
    if value.path in ("/admin", "/work/admin") and pairs:
        raise identity.IdentityError("INVALID_WORK_DESTINATION")
    if value.path == "/work" and pairs:
        if len(pairs) != 1:
            raise identity.IdentityError("INVALID_WORK_DESTINATION")
        name, content = pairs[0]
        if name == "templates" and content == "review":
            return destination
        if name != "session" or not re.fullmatch("session-[0-9a-f-]{36}", content):
            raise identity.IdentityError("INVALID_WORK_DESTINATION")
        try:
            uuid.UUID(content[8:])
        except ValueError:
            raise identity.IdentityError("INVALID_WORK_DESTINATION") from None
    return "/work/admin" if value.path == "/admin" else destination


@router.post("/api/work/session")
def work_session(
    request: Request,
    response: Response,
    body: WorkSessionInput = WorkSessionInput(),
    user=Depends(auth.current),
):
    def action():
        identity.require_feature(user, "work")
        destination = validate_destination(body.next)
        if destination == "/work/admin":
            identity.require_feature(user, "work_admin")
        if destination == "/work?templates=review":
            identity.require_feature(user, "template_review")
        selected = body.work_user_id or user["work_user_id"]
        if selected not in {row["work_user_id"] for row in user["work_principals"]}:
            raise identity.IdentityError("WORK_IDENTITY_MISMATCH", 403)
        parameters = {"next": destination}
        if body.work_user_id is not None:
            parameters["work_user_id"] = selected
        return {
            "work_url": runtime.public_url("/work-auth/sso?" + urlencode(parameters)),
            "work_user_id": selected,
        }

    result = invoke(action)
    # An already signed-in platform user may still carry only the original
    # host-only cookie. Reuse its validated session for the configured work
    # handoff, without issuing a new session or extending its expiry.
    if not request.cookies.get(auth.COOKIE):
        token = request.cookies.get(auth.LEGACY_COOKIE, "")
        session = db.query(
            "SELECT expires FROM sessions WHERE hash=? AND expires>?",
            (db.digest(token), time.time()),
            one=True,
        )
        if not session:
            raise HTTPException(401, "请先登录。")
        response.set_cookie(
            auth.COOKIE,
            token,
            max_age=max(0, int(session["expires"] - time.time())),
            httponly=True,
            secure=True,
            samesite="lax",
            domain=runtime.cookie_domain,
            path="/",
        )
    return result


@router.get("/api/work/quota")
def quota(request: Request, user=Depends(auth.current)):
    def action():
        identity.require_feature(user, "work")
        token = bridge.work_token(session_hash(request))
        result = bridge.request("GET", "/api/auth/quota", token=token)
        return result.get("data", result)

    return invoke(action)


@router.post("/api/admin/work/quota/grant")
def grant(body: GrantInput, request: Request, user=Depends(auth.current)):
    def action():
        identity.require_feature(user, 'work_quota_admin')
        target = identity.quota_target(body.user_id)
        return bridge.request(
            "POST",
            "/api/auth/internal/identity/grant",
            {
                "session_hash": session_hash(request),
                "target_work_user_id": target,
                "pages": body.pages,
                "points": body.points,
                "reason": body.reason,
                "idempotency_key": body.idempotency_key,
            },
        ).get("data", {})

    return invoke(action)


@router.get('/api/admin/work/quota/users')
def quota_users(user=Depends(auth.current)):
    return invoke(lambda: identity.quota_users(user['id']))


@router.get('/api/admin/work/quota/users/{user_id}')
def quota_user(user_id: str, request: Request, user=Depends(auth.current)):
    def action():
        identity.require_feature(user, 'work_quota_admin')
        target = identity.quota_target(user_id)
        bridge.request('POST', '/api/auth/internal/identity/credits', {'action': 'provision', 'work_user_id': target})
        token = bridge.work_token(session_hash(request))
        return bridge.request('GET', '/api/admin/users/' + target + '/quota', token=token).get('data', {})
    return invoke(action)


@router.post('/api/admin/work/quota/bulk-grant')
def bulk_grant(body: BulkGrantInput, request: Request, user=Depends(auth.current)):
    def action():
        identity.require_feature(user, 'work_quota_admin')
        if len(set(body.user_ids)) != len(body.user_ids) or not (body.pages or body.points) or not body.reason.strip():
            raise identity.IdentityError('INVALID_QUOTA_GRANT')
        # Validate and deduplicate all aliases before crediting any account.
        targets = sorted({identity.quota_target(uid) for uid in body.user_ids})
        results = []
        for target in targets:
            try:
                result = bridge.request('POST', '/api/auth/internal/identity/grant', {
                    'session_hash': session_hash(request), 'target_work_user_id': target,
                    'pages': body.pages, 'points': body.points, 'reason': body.reason,
                    'idempotency_key': body.idempotency_key,
                }).get('data', {})
                results.append({'work_user_id': target, 'success': True, **result})
            except identity.IdentityError as error:
                # Each ledger is independently durable. Retry the exact batch key
                # after an ambiguous response; settled grants cannot be duplicated.
                results.append({'work_user_id': target, 'success': False, 'code': error.code})
                break
        return {'complete': len(results) == len(targets) and all(x['success'] for x in results),
                'requested_users': len(body.user_ids), 'target_accounts': len(targets), 'results': results}
    return invoke(action)


def _match_targets(actor, raw_targets):
    """Resolve typed emails / names / aliases to directory users. Exact matches only."""
    directory = identity.quota_users(actor)['users']
    found, missing, ambiguous = [], [], []
    for raw in raw_targets:
        needle = raw.strip().lower()
        if not needle:
            continue
        hits = [u for u in directory if needle in {str(u.get(k) or '').strip().lower() for k in ('email', 'name', 'legacy_alias')}]
        if len(hits) == 1:
            found.append((raw.strip(), hits[0]))
        elif hits:
            ambiguous.append(raw.strip())
        else:
            missing.append(raw.strip())
    return found, missing, ambiguous


@router.post('/api/admin/work/quota/resolve')
def resolve_targets(body: dict, user=Depends(auth.current)):
    def action():
        identity.require_feature(user, 'work_quota_admin')
        targets = body.get('targets')
        if not isinstance(targets, list) or not 1 <= len(targets) <= 200 or any(not isinstance(t, str) or len(t) > 254 for t in targets):
            raise identity.IdentityError('INVALID_QUOTA_TARGETS', 400)
        found, missing, ambiguous = _match_targets(user['id'], targets)
        return {'found': [{'input': raw, 'id': u['id'], 'name': u.get('name'), 'email': u.get('email'),
                           'organization': u.get('organization'), 'active': u.get('active')} for raw, u in found],
                'missing': missing, 'ambiguous': ambiguous}
    return invoke(action)


@router.post('/api/admin/work/quota/grant-by-email')
def grant_by_email(body: EmailGrantInput, request: Request, user=Depends(auth.current)):
    """One step: paste emails/names, give points. Each person's grant is independently idempotent."""
    def action():
        identity.require_feature(user, 'work_quota_admin')
        if body.points == 0:
            raise identity.IdentityError('INVALID_QUOTA_GRANT', 400)
        found, missing, ambiguous = _match_targets(user['id'], body.targets)
        reason = body.reason.strip() or ('管理员赠送' if body.points > 0 else '管理员扣回')
        sid = session_hash(request)
        results, seen = [], set()
        for raw, person in found:
            if person['id'] in seen:
                continue
            seen.add(person['id'])
            entry = {'input': raw, 'name': person.get('name'), 'email': person.get('email')}
            try:
                if not person.get('active'):
                    raise identity.IdentityError('IDENTITY_DISABLED', 403)
                target = identity.quota_target(person['id'])
                bridge.request('POST', '/api/auth/internal/identity/credits', {'action': 'provision', 'work_user_id': target})
                key = (body.idempotency_key + ':' + target)[:128]
                if body.points > 0:
                    data = bridge.request('POST', '/api/auth/internal/identity/grant', {
                        'session_hash': sid, 'target_work_user_id': target, 'pages': 0, 'points': body.points,
                        'reason': reason, 'idempotency_key': key}).get('data', {})
                else:
                    token = bridge.work_token(sid)
                    data = bridge.request('POST', '/api/admin/quota/command', {'action': 'adjustment', 'user_id': target,
                        'points': body.points, 'reason': reason, 'idempotency_key': key}, token=token).get('data', {})
                quota = data.get('quota') or {}
                results.append(entry | {'success': True, 'available_points': (quota.get('available') or {}).get('points')})
            except identity.IdentityError as error:
                results.append(entry | {'success': False, 'code': error.code, 'message': getattr(error, 'user_message', None)})
        return {'complete': all(r['success'] for r in results) and not missing and not ambiguous,
                'results': results, 'missing': missing, 'ambiguous': ambiguous, 'reason': reason}
    return invoke(action)


@router.post('/api/work/quota/redeem')
def redeem_code(body: RedeemInput, request: Request, user=Depends(auth.current)):
    def action():
        identity.require_feature(user, 'work')
        token = bridge.work_token(session_hash(request))
        return bridge.request('POST', '/api/auth/quota/redeem', {'code': body.code}, token=token).get('data', {})
    return invoke(action)


@router.get('/api/admin/work/quota/overview')
def points_overview(request: Request, user=Depends(auth.current)):
    def action():
        identity.require_feature(user, 'work_quota_admin')
        token = bridge.work_token(session_hash(request))
        return bridge.request('GET', '/api/admin/quota/overview', token=token).get('data', {})
    return invoke(action)


@router.post('/api/admin/work/quota/command')
def points_command(body: dict, request: Request, user=Depends(auth.current)):
    def action():
        identity.require_feature(user, 'work_quota_admin')
        payload = dict(body)
        if payload.get('action') not in {'ledger', 'adjustment', 'policy', 'control', 'codes_create', 'codes_list', 'codes_revoke'}:
            raise identity.IdentityError('INVALID_QUOTA_COMMAND', 400)
        if 'user_id' in payload:
            payload['user_id'] = identity.quota_target(payload['user_id'])
            bridge.request('POST', '/api/auth/internal/identity/credits', {'action': 'provision', 'work_user_id': payload['user_id']})
        token = bridge.work_token(session_hash(request))
        return bridge.request('POST', '/api/admin/quota/command', payload, token=token).get('data', {})
    return invoke(action)


def design_admin_request(request, method, path, payload=None):
    token = bridge.work_token(session_hash(request))
    return invoke(lambda: bridge.request(method, '/api/design-memory/admin' + path, payload, token=token).get('data', {}))


@router.get('/api/admin/work/design-memory')
def design_memory_settings(request: Request, user=Depends(auth.admin)):
    return design_admin_request(request, 'GET', '')


@router.put('/api/admin/work/design-memory')
def design_memory_save(body: DesignPolicyInput, request: Request, user=Depends(auth.admin)):
    return design_admin_request(request, 'PUT', '', body.model_dump())


@router.post('/api/admin/work/design-memory/compile')
def design_memory_compile(body: DesignCompileInput, request: Request, user=Depends(auth.admin)):
    return design_admin_request(request, 'POST', '/compile', body.model_dump())


@router.post('/api/admin/work/design-memory/review')
def design_memory_review(body: DesignReviewInput, request: Request, user=Depends(auth.admin)):
    return design_admin_request(request, 'POST', '/review', body.model_dump())


def design_habits_request(request, user, method, path='', payload=None):
    """Personal design habits: the caller's own work principal, never an administrator token."""
    def action():
        identity.require_feature(user, 'work')
        # A first-run submission carries several habits; each is checked like a single one.
        for habit in [] if payload is None else payload.get('items', [payload]):
            scope, value = habit.get('scope') or {}, habit.get('value')
            if any(not 1 <= len(item.strip()) <= 60 for item in scope.values()):
                raise identity.IdentityError('INVALID_HABIT_SCOPE', 400)
            # Escaped length, as the work service measures it.
            if value is not None and len(json.dumps(value)) > 2000:
                raise identity.IdentityError('INVALID_HABIT_VALUE', 400)
        token = bridge.work_token(session_hash(request))
        return bridge.request(method, '/api/design-memory/habits' + path, payload, token=token).get('data', {})
    return invoke(action)


def design_habit_path(habit_id):
    if not re.fullmatch(r'[A-Za-z0-9-]{1,36}', habit_id):
        raise HTTPException(400, 'INVALID_HABIT_ID')
    return '/' + habit_id


@router.get('/api/work/design-habits')
def design_habits(request: Request, user=Depends(auth.current)):
    return design_habits_request(request, user, 'GET')


@router.post('/api/work/design-habits')
def design_habit_create(body: DesignHabitInput, request: Request, user=Depends(auth.current)):
    return design_habits_request(request, user, 'POST', '', body.model_dump(exclude_none=True))


@router.post('/api/work/design-habits/onboarding')
def design_habits_onboarding(body: DesignHabitOnboardingInput, request: Request, user=Depends(auth.current)):
    """Habits picked during first-run setup. The work service never lets them replace what the caller set or
    changed later; such picks come back under "skipped". Submitting again what the caller removed stores it again."""
    return design_habits_request(request, user, 'POST', '/onboarding', body.model_dump(exclude_none=True))


@router.get('/api/work/design-habits/export')
def design_habits_export(request: Request, user=Depends(auth.current), cursor: str | None = None):
    """One page of the caller's data. An answer with "next" continues with ?cursor=<next>, so no single
    answer has to exceed what the bridge carries. The bridge forwards no query strings, so the position
    goes to the work service as a path segment."""
    if cursor is not None and not re.fullmatch(r'[he]\.\d{8}T\d{6}\.\d{6}\.[0-9a-f-]{36}', cursor):
        raise HTTPException(400, 'INVALID_EXPORT_CURSOR')
    return design_habits_request(request, user, 'GET', '/export' + ('/' + cursor if cursor else ''))


@router.put('/api/work/design-habits/settings')
def design_habits_settings(body: DesignHabitSettingsInput, request: Request, user=Depends(auth.current)):
    return design_habits_request(request, user, 'PUT', '/settings', body.model_dump())


@router.post('/api/work/design-habits/reset')
def design_habits_reset(body: DesignHabitResetInput, request: Request, user=Depends(auth.current)):
    if body.confirm is not True:
        raise HTTPException(400, 'CONFIRMATION_REQUIRED')
    return design_habits_request(request, user, 'POST', '/reset', {'confirm': True})


@router.patch('/api/work/design-habits/{habit_id}')
def design_habit_update(habit_id: str, body: DesignHabitPatchInput, request: Request, user=Depends(auth.current)):
    payload = body.model_dump(exclude_none=True)
    if len(payload) < 2:
        raise HTTPException(400, 'EMPTY_HABIT_PATCH')
    return design_habits_request(request, user, 'PATCH', design_habit_path(habit_id), payload)


@router.delete('/api/work/design-habits/{habit_id}')
def design_habit_delete(habit_id: str, request: Request, user=Depends(auth.current)):
    return design_habits_request(request, user, 'DELETE', design_habit_path(habit_id))


@router.get("/api/history")
def history(
    request: Request,
    mode: Literal["all", "chat", "work"] = "all",
    user=Depends(auth.current),
):
    items = []
    unavailable = []
    if mode in ("all", "chat"):
        rows = db.query(
            "SELECT id,title,updated FROM conversations WHERE user_id=? AND deleted=0 ORDER BY updated DESC",
            (user["id"],),
        )
        items.extend(
            {
                "id": r["id"],
                "mode": "chat",
                "title": r["title"],
                "updated_at": r["updated"],
                "url": runtime.public_url("/?conversation=" + r["id"]),
            }
            for r in rows
        )
    if mode in ("all", "work") and user["permissions"]["work"]:
        for principal in user["work_principals"]:
            try:
                wid = principal["work_user_id"]
                token = bridge.work_token(session_hash(request), wid)
                result = bridge.request(
                    "GET", "/api/auth/internal/identity/history", token=token
                )
                entries = result.get("items")
                if not isinstance(entries, list):
                    raise identity.IdentityError("WORK_HISTORY_INVALID", 503)
                projected = []
                for row in entries:
                    import math

                    if (
                        not isinstance(row, dict)
                        or not isinstance(row.get("id"), str)
                        or not re.fullmatch("session-[0-9a-f-]{36}", row["id"])
                        or type(row.get("updated_at")) not in (int, float)
                        or not math.isfinite(row["updated_at"])
                    ):
                        raise identity.IdentityError("WORK_HISTORY_INVALID", 503)
                    try:
                        uuid.UUID(row["id"][8:])
                    except ValueError:
                        raise identity.IdentityError(
                            "WORK_HISTORY_INVALID", 503
                        ) from None
                    projected.append(
                        {
                            "id": row["id"],
                            "mode": "work",
                            "work_user_id": wid,
                            "title": str(row.get("title", "工作对话"))[:300],
                            "updated_at": row["updated_at"],
                            "url": runtime.public_url("/work?session=" + row["id"]),
                        }
                    )
                items.extend(projected)
            except identity.IdentityError:
                if "work" not in unavailable:
                    unavailable.append("work")
    items.sort(key=lambda x: float(x["updated_at"]), reverse=True)
    return {"items": items, "unavailable_modes": unavailable}


@router.post("/api/me/link-work")
def link_work(body: WorkLinkInput, request: Request, user=Depends(auth.current)):
    return invoke(
        lambda: identity.link_legacy(
            user["id"],
            body.username,
            body.password.get_secret_value(),
            body.idempotency_key,
            bridge.verify_legacy,
            lambda wid: bridge.has_work_records(wid, session_hash(request)),
        )
    )


@router.post("/api/me/link-platform")
def link_platform(
    body: PlatformLinkInput, request: Request, user=Depends(auth.current)
):
    def action():
        if user["account_type"] != "legacy_work" or not user.get("legacy_alias"):
            raise identity.IdentityError("LEGACY_SESSION_REQUIRED", 409)
        primary = auth.login(
            body.email, body.password.get_secret_value(), request.client.host
        )
        # Current legacy session is the first ownership proof; platform password is the second.
        return identity.link_legacy(
            primary["id"],
            user["legacy_alias"],
            "session-proof",
            body.idempotency_key,
            lambda wid, password: wid == user["work_user_id"],
            lambda wid: bridge.has_work_records(wid, session_hash(request)),
        )

    return invoke(action)
