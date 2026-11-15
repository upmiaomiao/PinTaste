"""Additive identity authority. Native chat/work records and work ledgers stay in place."""

from __future__ import annotations
import hashlib, hmac, json, re, time, uuid
from . import store as db

FEATURES = (
    "chat",
    "work",
    "upload",
    "ppt_generate",
    "ppt_trace",
    "image_gpt",
    "image_gemini",
    "template_submit",
    "template_review",
    "work_admin",
    "work_quota_admin",
)
BASIC_FEATURES = tuple(x for x in FEATURES if x not in {'template_review', 'work_admin', 'work_quota_admin'})
NAMESPACE = uuid.UUID("b303fe2b-5890-4a48-9e19-f0973db573bd")


class IdentityError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


def init():
    with db.connect() as c:
        c.executescript(
            """
  CREATE TABLE IF NOT EXISTS identity_accounts(user_id TEXT PRIMARY KEY REFERENCES users(id),account_type TEXT NOT NULL CHECK(account_type IN ('platform','legacy_work')),legacy_alias TEXT UNIQUE,work_user_id TEXT NOT NULL UNIQUE,work_role TEXT NOT NULL CHECK(work_role IN ('admin','employee')),linked_to_user_id TEXT REFERENCES users(id),created REAL NOT NULL);
  CREATE TABLE IF NOT EXISTS identity_permissions(user_id TEXT NOT NULL REFERENCES users(id),feature TEXT NOT NULL,allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),updated REAL NOT NULL,actor_user_id TEXT NOT NULL REFERENCES users(id),PRIMARY KEY(user_id,feature));
  CREATE TABLE IF NOT EXISTS identity_operations(sequence INTEGER PRIMARY KEY,actor_user_id TEXT NOT NULL REFERENCES users(id),request_key TEXT NOT NULL,operation TEXT NOT NULL,fingerprint TEXT NOT NULL,result TEXT NOT NULL,created REAL NOT NULL,UNIQUE(actor_user_id,request_key));
  CREATE TABLE IF NOT EXISTS identity_link_audit(sequence INTEGER PRIMARY KEY,legacy_user_id TEXT NOT NULL REFERENCES users(id),primary_user_id TEXT NOT NULL REFERENCES users(id),actor_user_id TEXT NOT NULL REFERENCES users(id),created REAL NOT NULL);
  """
        )
        for r in c.execute("SELECT id FROM users").fetchall():
            _account(c, r["id"])


def legacy_user_id(work_user_id):
    return uuid.uuid5(NAMESPACE, "legacy:" + str(work_user_id)).hex


def _account(c, user_id):
    row = c.execute(
        "SELECT * FROM identity_accounts WHERE user_id=?", (user_id,)
    ).fetchone()
    if row:
        return dict(row)
    if not c.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
        raise IdentityError("USER_NOT_FOUND", 404)
    work_id = str(uuid.uuid5(NAMESPACE, "platform-work:" + user_id))
    c.execute(
        "INSERT INTO identity_accounts VALUES(?,'platform',NULL,?,'employee',NULL,?) ON CONFLICT(user_id) DO NOTHING",
        (user_id, work_id, time.time()),
    )
    return dict(
        c.execute(
            "SELECT * FROM identity_accounts WHERE user_id=?", (user_id,)
        ).fetchone()
    )


def _owner(c, user_id):
    account = _account(c, user_id)
    seen = {user_id}
    while account["linked_to_user_id"]:
        user_id = account["linked_to_user_id"]
        if user_id in seen:
            raise IdentityError("IDENTITY_LINK_CYCLE", 503)
        seen.add(user_id)
        account = _account(c, user_id)
    return user_id


def _public(c, user_id):
    user = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        raise IdentityError("USER_NOT_FOUND", 404)
    user = dict(user)
    account = _account(c, user_id)
    # A linked alias resolves to its explicit primary principal; display names never link.
    if account["linked_to_user_id"]:
        return _public(c, _owner(c, user_id))
    linked = [
        dict(r)
        for r in c.execute(
            "SELECT a.* FROM identity_accounts a JOIN users u ON u.id=a.user_id WHERE a.linked_to_user_id=? AND u.active=1 ORDER BY a.created,a.user_id",
            (user_id,),
        )
    ]
    work_accounts = linked + [account]
    permissions = {x: x in BASIC_FEATURES for x in FEATURES}
    if any(x["work_role"] == "admin" for x in work_accounts):
        permissions.update(work_admin=True, template_review=True)
    explicit_permissions = set()
    for row in c.execute(
        "SELECT feature,allowed FROM identity_permissions WHERE user_id=?", (user_id,)
    ):
        if row["feature"] in FEATURES:
            explicit_permissions.add(row['feature'])
            permissions[row["feature"]] = bool(row["allowed"])
    # Preserve the old quota authority of effective work administrators, never
    # resurrect a revoked legacy role before applying explicit overrides.
    if 'work_quota_admin' not in explicit_permissions:
        permissions['work_quota_admin'] = permissions['work_admin'] is True
    if user["role"] == "admin":
        permissions = {x: True for x in FEATURES}
    chosen = linked[0] if linked else account
    return {
        **user,
        "email": None if account["account_type"] == "legacy_work" else user["email"],
        "account_type": account["account_type"],
        "legacy_alias": account["legacy_alias"],
        "permissions": permissions,
        "work_admin": permissions["work_admin"],
        "work_quota_admin": permissions["work_quota_admin"],
        "work_user_id": chosen["work_user_id"],
        "work_principals": [
            {"work_user_id": x["work_user_id"], "legacy_alias": x["legacy_alias"]}
            for x in work_accounts
        ],
    }


def public_user(user_id):
    with db.connect() as c:
        return _public(c, user_id)


def require_feature(user, feature):
    if (
        feature not in FEATURES
        or not user.get("active")
        or user.get("permissions", {}).get(feature) is not True
    ):
        raise IdentityError("FEATURE_DISABLED", 403)


def _admin(c, user_id):
    user = _public(c, user_id)
    if user["role"] != "admin" or not user["active"]:
        raise IdentityError("GLOBAL_ADMIN_REQUIRED", 403)
    return user


def import_legacy_accounts(rows):
    """Operator-only metadata import; never copies passwords or rewrites native work data."""
    if not isinstance(rows, list) or len(rows) > 10000:
        raise IdentityError("INVALID_LEGACY_IMPORT")
    clean = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "work_user_id",
            "alias",
            "work_role",
        }:
            raise IdentityError("INVALID_LEGACY_IMPORT")
        try:
            wid = str(uuid.UUID(row["work_user_id"]))
        except (ValueError, TypeError, AttributeError):
            raise IdentityError("INVALID_WORK_UUID") from None
        if (
            row["work_role"] not in ("admin", "employee")
            or not isinstance(row["alias"], str)
            or not re.fullmatch("[A-Za-z0-9_]{3,80}", row["alias"])
        ):
            raise IdentityError("INVALID_LEGACY_IMPORT")
        clean.append((wid, row["alias"], row["work_role"]))
    if len({x[0] for x in clean}) != len(clean) or len({x[1] for x in clean}) != len(
        clean
    ):
        raise IdentityError("DUPLICATE_LEGACY_IMPORT")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        for wid, alias, role in clean:
            uid = legacy_user_id(wid)
            existing = c.execute(
                "SELECT * FROM identity_accounts WHERE work_user_id=? OR legacy_alias=?",
                (wid, alias),
            ).fetchall()
            if existing:
                if len(existing) != 1 or (
                    existing[0]["user_id"],
                    existing[0]["legacy_alias"],
                    existing[0]["work_role"],
                ) != (uid, alias, role):
                    raise IdentityError("LEGACY_IMPORT_CONFLICT", 409)
                continue
            now = time.time()
            c.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?,?,?)",
                (
                    uid,
                    "legacy-work-" + wid + "@accounts.invalid",
                    alias,
                    "旧工作账号",
                    "其他",
                    "user",
                    1,
                    now,
                ),
            )
            c.execute(
                "INSERT INTO identity_accounts VALUES(?,'legacy_work',?,?,?,NULL,?)",
                (uid, alias, wid, role, now),
            )
    return {
        "status": "imported",
        "legacy_accounts": len(clean),
        "native_work_records_modified": False,
        "password_hashes_copied": False,
    }


def _operation(c, actor, key, operation, data, perform):
    if not isinstance(key, str) or not re.fullmatch("[A-Za-z0-9_.:-]{1,128}", key):
        raise IdentityError("INVALID_IDEMPOTENCY_KEY")
    fingerprint = hashlib.sha256(
        json.dumps(
            {"operation": operation, "data": data},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    old = c.execute(
        "SELECT * FROM identity_operations WHERE actor_user_id=? AND request_key=?",
        (actor, key),
    ).fetchone()
    if old:
        if old["fingerprint"] != fingerprint:
            raise IdentityError("IDEMPOTENCY_CONFLICT", 409)
        return json.loads(old["result"])
    result = perform()
    now = time.time()
    c.execute(
        "INSERT INTO identity_operations(actor_user_id,request_key,operation,fingerprint,result,created)VALUES(?,?,?,?,?,?)",
        (actor, key, operation, fingerprint, json.dumps(result), now),
    )
    c.execute(
        "INSERT INTO audit(actor,action,created)VALUES(?,?,?)",
        (actor, "identity_" + operation, now),
    )
    return result


def list_users(actor):
    with db.connect() as c:
        _admin(c, actor)
        users = [
            _public(c, r["id"])
            for r in c.execute("SELECT id FROM users ORDER BY created,id").fetchall()
        ]
        # Linked records stay visible as aliases, not silently deleted from the registry.
        for i, row in enumerate(
            c.execute("SELECT id FROM users ORDER BY created,id").fetchall()
        ):
            source = _account(c, row["id"])
            if source["linked_to_user_id"]:
                users[i] = {
                    **users[i],
                    "id": row["id"],
                    "account_type": source["account_type"],
                    "legacy_alias": source["legacy_alias"],
                    "work_user_id": source["work_user_id"],
                    "email": None,
                    "name": source["legacy_alias"],
                    "role": c.execute(
                        "SELECT role FROM users WHERE id=?", (row["id"],)
                    ).fetchone()[0],
                    "active": c.execute(
                        "SELECT active FROM users WHERE id=?", (row["id"],)
                    ).fetchone()[0],
                    "linked_to_user_id": source["linked_to_user_id"],
                }
        revision = c.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM identity_operations"
        ).fetchone()[0]
        return {"users": users, "revision": revision, "features": list(FEATURES)}


def quota_target(user_id):
    """A selected alias credits its original ledger; a primary uses its default."""
    with db.connect() as c:
        source = _account(c, user_id)
        active = c.execute("SELECT active FROM users WHERE id=?", (user_id,)).fetchone()
        if not active or not active[0]:
            raise IdentityError("IDENTITY_DISABLED", 403)
        projected = _public(c, user_id)
        if not projected["active"]:
            raise IdentityError("IDENTITY_DISABLED", 403)
        return (
            source["work_user_id"]
            if source["account_type"] == "legacy_work"
            else projected["work_user_id"]
        )


def quota_users(actor):
    """Quota operators receive only the user directory needed for granting credits."""
    with db.connect() as c:
        manager = _public(c, actor)
        require_feature(manager, 'work_quota_admin')
        result, seen = [], set()
        for row in c.execute('SELECT id FROM users ORDER BY created,id').fetchall():
            user = _public(c, row['id'])
            if user['id'] in seen:
                continue
            seen.add(user['id'])
            result.append({key: user.get(key) for key in ('id', 'name', 'email', 'legacy_alias',
                           'organization', 'active', 'work_user_id')})
        return {'users': result, 'unit': 'points'}


def change_permissions(actor, user_ids, patch, idempotency_key):
    if (
        not isinstance(user_ids, list)
        or not 1 <= len(user_ids) <= 500
        or any(not isinstance(x, str) for x in user_ids)
        or len(set(user_ids)) != len(user_ids)
    ):
        raise IdentityError("INVALID_USER_IDS")
    if (
        not isinstance(patch, dict)
        or not patch
        or set(patch) - set(FEATURES)
        or any(type(v) is not bool for v in patch.values())
    ):
        raise IdentityError("INVALID_FEATURE_PATCH")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _admin(c, actor)
        for uid in user_ids:
            if not c.execute("SELECT id FROM users WHERE id=?", (uid,)).fetchone():
                raise IdentityError("USER_NOT_FOUND", 404)
        principals = sorted({_owner(c, uid) for uid in user_ids})

        def perform():
            now = time.time()
            for uid in principals:
                for feature, value in patch.items():
                    c.execute(
                        "INSERT INTO identity_permissions VALUES(?,?,?,?,?)ON CONFLICT(user_id,feature)DO UPDATE SET allowed=excluded.allowed,updated=excluded.updated,actor_user_id=excluded.actor_user_id",
                        (uid, feature, int(value), now, actor),
                    )
            return {
                "status": "updated",
                "user_ids": principals,
                "requested_user_ids": sorted(user_ids),
                "permissions_patch": patch,
            }

        return _operation(
            c,
            actor,
            idempotency_key,
            "permissions",
            {"user_ids": sorted(user_ids), "patch": patch},
            perform,
        )


def change_role(actor, user_id, role, idempotency_key):
    if role not in ("admin", "user"):
        raise IdentityError("INVALID_ROLE")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _admin(c, actor)
        target = c.execute(
            "SELECT role,active FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if not target:
            raise IdentityError("USER_NOT_FOUND", 404)
        if _account(c, user_id)["linked_to_user_id"]:
            raise IdentityError("LINKED_ALIAS_ROLE_USE_PRIMARY", 409)

        def perform():
            if (
                target["role"] == "admin"
                and role != "admin"
                and target["active"]
                and c.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin'AND active=1"
                ).fetchone()[0]
                <= 1
            ):
                raise IdentityError("LAST_ADMIN_REQUIRED", 409)
            c.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            return {"status": "updated", "user_id": user_id, "role": role}

        return _operation(
            c,
            actor,
            idempotency_key,
            "role",
            {"user_id": user_id, "role": role},
            perform,
        )


def introspect(*, session_hash=None, work_user_id=None):
    with db.connect() as c:
        if session_hash is not None:
            if not isinstance(session_hash, str) or not re.fullmatch(
                "[a-f0-9]{64}", session_hash
            ):
                raise IdentityError("INVALID_SESSION", 401)
            row = c.execute(
                "SELECT s.user_id FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>? AND u.active=1",
                (session_hash, time.time()),
            ).fetchone()
            if not row:
                raise IdentityError("SESSION_EXPIRED_OR_REVOKED", 401)
            user = _public(c, row["user_id"])
        elif work_user_id is not None:
            row = c.execute(
                "SELECT a.user_id FROM identity_accounts a JOIN users u ON u.id=a.user_id WHERE a.work_user_id=? AND u.active=1",
                (work_user_id,),
            ).fetchone()
            if not row:
                raise IdentityError("WORK_IDENTITY_NOT_MAPPED", 401)
            user = _public(c, row["user_id"])
        else:
            raise IdentityError("IDENTITY_LOOKUP_REQUIRED", 401)
        if not user["active"]:
            raise IdentityError("IDENTITY_DISABLED", 403)
        if (
            work_user_id is not None
            and not c.execute(
                "SELECT 1 FROM identity_accounts a JOIN users u ON u.id=a.user_id WHERE a.work_user_id=? AND u.active=1",
                (work_user_id,),
            ).fetchone()
        ):
            raise IdentityError("WORK_IDENTITY_DISABLED", 403)
        if work_user_id is not None and work_user_id not in {
            x["work_user_id"] for x in user["work_principals"]
        }:
            raise IdentityError("WORK_IDENTITY_MISMATCH", 403)
        return {
            "identity_id": user["id"],
            "work_user_id": work_user_id or user["work_user_id"],
            "permissions": user["permissions"],
            "active": True,
            "global_admin": user["role"] == "admin",
            "work_admin": user["work_admin"],
            "account_type": user["account_type"],
        }


def legacy_login(alias, password, verify):
    if (
        not isinstance(alias, str)
        or not isinstance(password, str)
        or not 1 <= len(password.encode("utf-8")) <= 72
    ):
        raise IdentityError("INVALID_CREDENTIALS", 401)
    with db.connect() as c:
        row = c.execute(
            "SELECT * FROM identity_accounts WHERE account_type='legacy_work'AND legacy_alias=?",
            (alias,),
        ).fetchone()
        if not row:
            raise IdentityError("INVALID_CREDENTIALS", 401)
        row = dict(row)
        original = c.execute(
            "SELECT active FROM users WHERE id=?", (row["user_id"],)
        ).fetchone()
        if not original or not original["active"]:
            raise IdentityError("IDENTITY_DISABLED", 403)
    # Password verification stays in the original work backend, never in browser code.
    if verify(row["work_user_id"], password) is not True:
        raise IdentityError("INVALID_CREDENTIALS", 401)
    user = public_user(row["user_id"])
    if not user["active"]:
        raise IdentityError("IDENTITY_DISABLED", 403)
    return user


def link_legacy(actor, alias, password, idempotency_key, verify, has_work_records=None):
    legacy = legacy_login(alias, password, verify)
    primary_before = public_user(actor)
    if has_work_records is None:
        raise IdentityError("WORKSPACE_STATUS_CHECK_REQUIRED", 503)
    with db.connect() as check:
        target = check.execute(
            "SELECT work_user_id FROM identity_accounts WHERE legacy_alias=?", (alias,)
        ).fetchone()
    if not target:
        raise IdentityError("LEGACY_ACCOUNT_NOT_FOUND", 404)
    if primary_before["work_user_id"] != target["work_user_id"] and has_work_records(
        primary_before["work_user_id"]
    ):
        raise IdentityError("ACCOUNT_WORKSPACE_CONFLICT", 409)
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        primary = _public(c, actor)
        if not primary["active"]:
            raise IdentityError("IDENTITY_DISABLED", 403)
        row = c.execute(
            "SELECT * FROM identity_accounts WHERE legacy_alias=? AND account_type='legacy_work'",
            (alias,),
        ).fetchone()
        if not row:
            raise IdentityError("LEGACY_ACCOUNT_NOT_FOUND", 404)
        if row["user_id"] == actor:
            raise IdentityError("ALREADY_THIS_IDENTITY", 409)
        if row["linked_to_user_id"] and row["linked_to_user_id"] != actor:
            raise IdentityError("LEGACY_ALREADY_LINKED", 409)
        if _account(c, actor)["linked_to_user_id"]:
            raise IdentityError("PRIMARY_ALREADY_LINKED", 409)
        if (
            not row["linked_to_user_id"]
            and c.execute(
                "SELECT 1 FROM conversations WHERE user_id=? LIMIT 1", (row["user_id"],)
            ).fetchone()
        ):
            raise IdentityError("ACCOUNT_WORKSPACE_CONFLICT", 409)

        def perform():
            c.execute(
                "UPDATE identity_accounts SET linked_to_user_id=? WHERE user_id=?",
                (actor, row["user_id"]),
            )
            c.execute(
                "INSERT INTO identity_link_audit(legacy_user_id,primary_user_id,actor_user_id,created)VALUES(?,?,?,?)",
                (row["user_id"], actor, actor, time.time()),
            )
            return {
                "status": "linked",
                "primary_user_id": actor,
                "legacy_user_id": row["user_id"],
                "native_records_rewritten": False,
                "global_role_unchanged": True,
            }

        return _operation(
            c,
            actor,
            idempotency_key,
            "link",
            {"legacy_user_id": row["user_id"]},
            perform,
        )


def _platform_request_features(request):
    """Gate new native chat/tool/upload work; reading or cancelling own history stays possible."""
    path = request.url.path
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if (
        path.startswith("/api/auth/")
        or path.startswith("/api/internal/")
        or path.startswith("/api/admin/")
        or path.startswith("/api/work/")
        or path.startswith("/api/me/")
        or path.startswith("/api/identity/")
    ):
        return
    if path.startswith("/api/jobs/") and path.endswith("/cancel"):
        return
    features = []
    if path.startswith(
        ("/api/jobs", "/api/conversations", "/api/voice", "/api/tools", "/api/files")
    ):
        features.append("chat")
    if path == "/api/files":
        features.append("upload")
    return features


def enforce_platform_request(request):
    features = _platform_request_features(request)
    if features:
        from . import auth
        user = auth.current(request)
        for feature in features:
            require_feature(user, feature)


async def enforce_platform_request_async(request):
    # Static/read/admin/internal fast paths need no DB worker token. In
    # particular identity callbacks keep their independent authority lane.
    if _platform_request_features(request):
        await db.run_sync(enforce_platform_request, request)
