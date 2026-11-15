"""Optional central identity enforcement without changing work user UUIDs or ledgers."""

import json, os, re, stat, math, uuid
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from flask import current_app, request, has_request_context, has_app_context

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


class IdentityFailure(ValueError):
    def __init__(self, code, status=403):
        super().__init__(code)
        self.code = code
        self.status = status


def config(name, default=""):
    return (
        current_app.config.get(name, os.environ.get(name, default))
        if has_app_context()
        else os.environ.get(name, default)
    )


def enabled():
    return config("WUYU_IDENTITY_ENABLED", False) in (True, "true", "1", "yes")


def shared_key():
    p = Path(str(config("WUYU_IDENTITY_BRIDGE_KEY_PATH")))
    if not p.is_absolute() or p.is_symlink() or not p.is_file():
        raise IdentityFailure("IDENTITY_BRIDGE_UNCONFIGURED", 503)
    st = p.stat()
    if os.name == "posix" and (
        st.st_uid not in (0, os.geteuid()) or stat.S_IMODE(st.st_mode) & 0o077
    ):
        raise IdentityFailure("IDENTITY_BRIDGE_UNCONFIGURED", 503)
    raw = p.read_bytes().strip()
    if not 32 <= len(raw) <= 256:
        raise IdentityFailure("IDENTITY_BRIDGE_UNCONFIGURED", 503)
    try:
        return raw.decode("ascii")
    except UnicodeError:
        raise IdentityFailure("IDENTITY_BRIDGE_UNCONFIGURED", 503) from None


def authority_url():
    base = str(config("WUYU_IDENTITY_AUTHORITY_URL")).rstrip("/")
    u = urlsplit(base)
    if (
        u.scheme != "http"
        or u.hostname not in ("127.0.0.1", "host.docker.internal", "wuyu-platform")
        or u.port != 8920
        or u.path not in ("", "/")
        or u.query
        or u.fragment
        or u.username
        or u.password
    ):
        raise IdentityFailure("IDENTITY_AUTHORITY_UNCONFIGURED", 503)
    return base


def lookup(*, session_hash=None, work_user_id=None):
    if session_hash is None and work_user_id is None:
        raise IdentityFailure("IDENTITY_REQUIRED", 401)
    payload = {
        k: v
        for k, v in {"session_hash": session_hash, "work_user_id": work_user_id}.items()
        if v is not None
    }
    try:
        with httpx.Client(
            trust_env=False, follow_redirects=False, timeout=10
        ) as client:
            with client.stream(
                "POST",
                authority_url() + "/api/internal/identity/introspect",
                json=payload,
                headers={
                    "Host": "ai.wuyullm.com",
                    "X-Wuyu-Identity-Key": shared_key(),
                    "X-Wuyu-Client": "1",
                },
            ) as response:
                if response.status_code != 200:
                    raise IdentityFailure(
                        "IDENTITY_REJECTED",
                        (
                            401
                            if response.status_code == 401
                            else 403 if response.status_code == 403 else 503
                        ),
                    )
                raw = b""
                for part in response.iter_bytes():
                    raw += part
                    if len(raw) > 65536:
                        raise IdentityFailure("IDENTITY_RESPONSE_INVALID", 503)
                value = json.loads(raw)
    except IdentityFailure:
        raise
    except Exception:
        raise IdentityFailure("IDENTITY_AUTHORITY_UNAVAILABLE", 503) from None
    if (
        not isinstance(value, dict)
        or value.get("active") is not True
        or not isinstance(value.get("permissions"), dict)
        or set(value["permissions"]) != set(FEATURES)
        or any(type(x) is not bool for x in value["permissions"].values())
    ):
        raise IdentityFailure("IDENTITY_RESPONSE_INVALID", 503)
    try:
        if (
            not isinstance(value.get("identity_id"), str)
            or not re.fullmatch("[a-f0-9]{32}", value["identity_id"])
            or str(uuid.UUID(value.get("work_user_id"))) != value["work_user_id"]
        ):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise IdentityFailure("IDENTITY_RESPONSE_INVALID", 503) from None
    if work_user_id is not None and value.get("work_user_id") != work_user_id:
        raise IdentityFailure("IDENTITY_SCOPE_MISMATCH", 403)
    return value


class EffectiveUser:
    """Per-request permission projection; never mutates an ORM role or user row."""

    def __init__(self, user, identity):
        self._user = user
        self._verified_identity = identity

    def __getattr__(self, name):
        return getattr(self._user, name)

    @property
    def role(self):
        return (
            "admin"
            if self._verified_identity["permissions"]["work_admin"]
            else "employee"
        )

    def to_dict(self):
        return {
            **self._user.to_dict(),
            "role": self.role,
            "identity_id": self._verified_identity["identity_id"],
            "permissions": dict(self._verified_identity["permissions"]),
            "account_type": self._verified_identity.get("account_type"),
            "global_admin": self._verified_identity.get("global_admin") is True,
        }


def permissions_for(user):
    if isinstance(user, EffectiveUser):
        return user._verified_identity["permissions"]
    if not enabled():
        return {
            x: (x in BASIC_FEATURES or getattr(user, "role", None) == "admin")
            for x in FEATURES
        }
    if not user or not getattr(user, "id", None):
        raise IdentityFailure("IDENTITY_REQUIRED", 401)
    return lookup(work_user_id=user.id)["permissions"]


def has_feature(user, feature):
    if feature not in FEATURES:
        return False
    return permissions_for(user).get(feature) is True


def _require(permissions, features):
    for feature in features:
        if permissions.get(feature) is not True:
            raise IdentityFailure("FEATURE_DISABLED_" + feature.upper(), 403)


def _brief_for_request(user):
    match = re.match(r"^/api/projects/([^/]+)/workflow/action$", request.path)
    if not match:
        return {}, {}
    from models import Project
    from models.wuyu_workflow import WuyuWorkflow

    project = Project.query.get(match.group(1))
    if project and user.role != "admin" and project.user_id != user.id:
        raise IdentityFailure("FORBIDDEN", 403)
    row = WuyuWorkflow.query.get(match.group(1))
    body = json.loads(row.body) if row else {}
    return body.get("brief", {}), body.get("workflow", {})


def authorize_request(user, identity):
    permissions = identity["permissions"]
    _require(permissions, ["work"])
    path = request.path.rstrip("/")
    write = request.method not in ("GET", "HEAD", "OPTIONS")
    if (
        request.method == "POST"
        and path == "/api/admin/users"
        or request.method == "DELETE"
        and re.fullmatch(r"/api/admin/users/[^/]+", path)
    ):
        raise IdentityFailure("CENTRAL_IDENTITY_MANAGED", 409)
    required = []
    if re.fullmatch(r'/api/admin/users/[^/]+/quota(?:/adjustments)?', path) or path in {'/api/admin/work-quota/users', '/api/admin/quota/overview', '/api/admin/quota/command'}:
        required.append('work_quota_admin')
    elif path.startswith("/api/admin/") or path.startswith("/api/settings"):
        required.append("work_admin")
    if (
        write
        and ("/materials" in path or "/upload" in path or "/reference-files" in path)
        and not path.endswith("/workflow/action")
    ):
        required.append("upload")
    if path.startswith("/api/template-library"):
        if (
            path.endswith("/review")
            or path.endswith("/audit")
            or path.endswith("/tag-queue")
        ):
            required.append("template_review")
        elif write and path.endswith("/submissions"):
            required.append("template_submit")
    elif write and any(x in path for x in ("/user-templates", "/user-style-templates")):
        required += ["upload", "template_submit"]
    if path.endswith("/workflow/action") and write:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return
        action = data.get("action")
        payload = data.get("payload") or {}
        if not isinstance(action, str) or not isinstance(payload, dict):
            return
        brief, wf = _brief_for_request(user)
        incoming = payload.get("brief", payload) if isinstance(payload, dict) else {}
        if not isinstance(incoming, dict):
            return
        selected = incoming.get("image_profile", brief.get("image_profile", "default"))
        uses_image_profile = (
            action
            in {
                "generate_samples",
                "continue_from_draft",
                "regenerate_samples",
                "generate_next",
                "generate_focused",
                "generate_images",
                "confirm_template",
                "revise_page",
                "retry",
                "resume",
                "confirm_page",
                "confirm_images",
            }
            or action == "update_brief"
            and "image_profile" in incoming
        )
        if selected == "default" and uses_image_profile:
            from services.wuyu_runtime_settings import get_generation_options

            selected = get_generation_options(dict(current_app.config))[
                "default_profile"
            ]
        generation = {
            "analyze_materials",
            "revise_draft",
            "continue_from_draft",
            "exclude_materials",
            "generate_outline",
            "generate_samples",
            "regenerate_samples",
            "generate_next",
            "generate_focused",
            "generate_images",
            "confirm_template",
            "revise_page",
        }
        pages = wf.get("pages", []) if isinstance(wf, dict) else []
        all_images = bool(pages) and all(
            p.get("artifact") and p.get("generation_status") == "ready" for p in pages
        )
        missing_trace = any(
            not p.get("trace")
            or p["trace"].get("source_image_version") != p.get("image_version")
            for p in pages
        )
        output = incoming.get(
            "output_preference", brief.get("output_preference", "editable")
        )
        if action in generation:
            required.append("ppt_generate")
            text_only_actions = {"generate_outline", "analyze_materials", "revise_draft", "exclude_materials"}
            if action not in text_only_actions:
                required.append("image_gemini" if selected == "gemini" else "image_gpt")
            if action not in text_only_actions and output == "editable":
                required.append("ppt_trace")
        if action in ("retry", "resume", "confirm_page", "confirm_images"):
            if all_images:
                if output != "image" and missing_trace:
                    required.append("ppt_trace")
            else:
                required += [
                    "ppt_generate",
                    "image_gemini" if selected == "gemini" else "image_gpt",
                ]
                if output != "image":
                    required.append("ppt_trace")
        if (
            action == "start_tracing"
            or action == "export"
            and payload.get("format", "editable_pptx") == "editable_pptx"
        ):
            required.append("ppt_trace")
        if action == "update_brief" and "image_profile" in incoming:
            required.append("image_gemini" if selected == "gemini" else "image_gpt")
        if action == "update_brief" and incoming.get("output_preference") == "editable":
            required.append("ppt_trace")

    elif write and any(
        x in path for x in ("/generate", "/refine", "/edit", "/renovation")
    ):
        required += ["ppt_generate"]
    _require(permissions, required)


def authorize_user(user, payload):
    if not enabled():
        return user
    if terminal_metering():
        return user
    identity = lookup(
        session_hash=payload.get("identity_session_hash"), work_user_id=user.id
    )
    if payload.get("identity_id") and payload["identity_id"] != identity["identity_id"]:
        raise IdentityFailure("IDENTITY_SCOPE_CHANGED", 401)
    result = EffectiveUser(user, identity)
    authorize_request(result, identity)
    request.current_identity = identity
    return result


def authorize_paid(user_id, kind, model=None):
    if not enabled():
        return
    identity = lookup(work_user_id=user_id)
    required = ["work"]
    if kind == "image":
        required += [
            "ppt_generate",
            "image_gemini" if model == "gemini-nano-banana-2.1" else "image_gpt",
        ]
    elif kind == "trace":
        required += ["ppt_trace"]
    elif kind == "text":
        required += ["ppt_generate"]
    _require(identity["permissions"], required)


def validate_destination(destination):
    from urllib.parse import parse_qsl
    import uuid

    if (
        not isinstance(destination, str)
        or len(destination) > 200
        or any(ord(char) < 32 or ord(char) == 127 for char in destination)
    ):
        raise IdentityFailure("INVALID_SSO_RETURN", 400)
    value = urlsplit(destination)
    if (
        value.scheme
        or value.netloc
        or value.fragment
        or value.path not in ("/work", "/admin", "/work/admin")
        or "\\" in destination
    ):
        raise IdentityFailure("INVALID_SSO_RETURN", 400)
    pairs = parse_qsl(value.query, keep_blank_values=True)
    if value.path in ("/admin", "/work/admin") and pairs:
        raise IdentityFailure("INVALID_SSO_RETURN", 400)
    if value.path == "/work" and pairs:
        if len(pairs) != 1:
            raise IdentityFailure("INVALID_SSO_RETURN", 400)
        name, content = pairs[0]
        if name == "templates" and content == "review":
            return destination
        if name != "session" or not re.fullmatch("session-[0-9a-f-]{36}", content):
            raise IdentityFailure("INVALID_SSO_RETURN", 400)
        try:
            uuid.UUID(content[8:])
        except ValueError:
            raise IdentityFailure("INVALID_SSO_RETURN", 400) from None
    return "/work/admin" if value.path == "/admin" else destination


def terminal_metering():
    """A revoked identity can finish an existing hold; it cannot dispatch new work."""
    import hmac

    if request.path != "/api/internal/quota/command" or request.method != "POST":
        return False
    value = request.get_json(silent=True)
    if not isinstance(value, dict) or value.get("action") not in ("settle", "refund"):
        return False
    expected = config("WUYU_QUOTA_INTERNAL_KEY")
    supplied = request.headers.get("X-Wuyu-Internal-Key", "")
    return (
        isinstance(expected, str)
        and len(expected) >= 32
        and hmac.compare_digest(supplied.encode(), expected.encode())
    )


def generation_options_for(user, options):
    from copy import deepcopy

    if not enabled():
        return options
    result = deepcopy(options)
    permissions = permissions_for(user)
    for row in result.get("profiles", []):
        allowed = (
            permissions["image_gemini" if row.get("id") == "gemini" else "image_gpt"]
            and permissions["ppt_generate"]
        )
        if not allowed:
            row["available"] = False
            row["reason"] = "FEATURE_DISABLED"
    result["permissions"] = {k: permissions[k] for k in FEATURES}
    return result


def tenant_router_url():
    """The server selects one explicit internal router port; requests cannot choose it."""
    base = str(config("WUYU_TENANT_ROUTER_URL"))
    raw_port = str(config("WUYU_TENANT_ROUTER_EXPECTED_PORT", 8150))
    if (not re.fullmatch(r"[0-9]{1,5}", raw_port) or not 1 <= int(raw_port) <= 65535
            or not base or any(ord(char) <= 32 or ord(char) == 127 for char in base)
            or any(char in base for char in ("\\", "?", "#"))):
        raise IdentityFailure("WORK_HISTORY_UNCONFIGURED", 503)
    try:
        u = urlsplit(base)
        port = u.port
    except ValueError:
        raise IdentityFailure("WORK_HISTORY_UNCONFIGURED", 503) from None
    if (u.scheme != "http" or u.hostname not in ("127.0.0.1", "host.docker.internal")
            or port != int(raw_port) or u.path not in ("", "/")
            or u.query or u.fragment or u.username is not None or u.password is not None):
        raise IdentityFailure("WORK_HISTORY_UNCONFIGURED", 503)
    return base.rstrip("/")


def work_history(token, work_user_id):
    from urllib.parse import urlencode

    base = tenant_router_url()
    try:
        with httpx.Client(
            trust_env=False, follow_redirects=False, timeout=90
        ) as client:
            with client.stream(
                "GET",
                base
                + "/__internal/identity/history?"
                + urlencode({"work_user_id": work_user_id}),
                headers={
                    "Authorization": "Bearer " + token,
                    "X-Wuyu-Identity-Key": shared_key(),
                },
            ) as response:
                if response.status_code != 200:
                    raise IdentityFailure("WORK_HISTORY_UNAVAILABLE", 503)
                raw = b""
                for part in response.iter_bytes():
                    raw += part
                    if len(raw) > 4 * 1024 * 1024:
                        raise IdentityFailure("WORK_HISTORY_TOO_LARGE", 503)
                value = json.loads(raw)
    except IdentityFailure:
        raise
    except Exception:
        raise IdentityFailure("WORK_HISTORY_UNAVAILABLE", 503) from None
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        raise IdentityFailure("WORK_HISTORY_INVALID", 503)
    for row in value["items"]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not re.fullmatch("session-[a-f0-9-]{36}", row["id"])
            or type(row.get("blank")) is not bool
            or type(row.get("running")) is not bool
            or type(row.get("updated_at")) not in (int, float)
            or not math.isfinite(row["updated_at"])
        ):
            raise IdentityFailure("WORK_HISTORY_INVALID", 503)
        try:
            uuid.UUID(row["id"][8:])
        except ValueError:
            raise IdentityFailure("WORK_HISTORY_INVALID", 503) from None
    return value["items"]
