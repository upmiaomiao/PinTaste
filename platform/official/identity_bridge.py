"""Server-only, bounded and proxy-independent identity/work transport."""

import re
import json, os, stat
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from .integrated_identity import IdentityError


def key():
    path = os.environ.get("WUYU_IDENTITY_BRIDGE_KEY_PATH", "")
    if not path:
        raise IdentityError("IDENTITY_BRIDGE_UNCONFIGURED", 503)
    p = Path(path)
    if not p.is_absolute() or p.is_symlink() or not p.is_file():
        raise IdentityError("IDENTITY_BRIDGE_UNCONFIGURED", 503)
    st = p.stat()
    if os.name == "posix" and (
        st.st_uid not in (0, os.geteuid()) or stat.S_IMODE(st.st_mode) & 0o077
    ):
        raise IdentityError("IDENTITY_BRIDGE_UNCONFIGURED", 503)
    raw = p.read_bytes().strip()
    if not 32 <= len(raw) <= 256:
        raise IdentityError("IDENTITY_BRIDGE_UNCONFIGURED", 503)
    try:
        return raw.decode("ascii")
    except UnicodeError:
        raise IdentityError("IDENTITY_BRIDGE_UNCONFIGURED", 503) from None


def work_url():
    base = os.environ.get("WUYU_WORK_BACKEND_URL", "").rstrip("/")
    u = urlsplit(base)
    valid = (
        u.scheme == "http"
        and u.hostname
        in ("127.0.0.1", "host.docker.internal", "banana-agent-test-backend")
        and u.port in (8131, 5000)
        and u.path in ("", "/")
        and not u.query
        and not u.fragment
        and not u.username
        and not u.password
    )
    if not valid:
        raise IdentityError("WORK_BRIDGE_UNCONFIGURED", 503)
    return base


def request(method, path, payload=None, *, token=None, idempotency_key=None, timeout=90):
    if not path.startswith("/api/") or "?" in path or ".." in path:
        raise IdentityError("INVALID_BRIDGE_PATH", 503)
    headers = {
        "X-Wuyu-Client": "1",
        "X-Wuyu-Identity-Key": key(),
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        with httpx.Client(
            trust_env=False, follow_redirects=False, timeout=timeout
        ) as client:
            with client.stream(
                method, work_url() + path, headers=headers, json=payload
            ) as response:
                if response.status_code not in (200, 201):
                    status = 503 if response.status_code >= 500 else response.status_code
                    code, message = "WORK_BRIDGE_REJECTED", None
                    if status < 500:
                        try:
                            error = json.loads(response.read()[:65536]).get("error") or {}
                            if isinstance(error.get("code"), str) and re.fullmatch(r"[A-Z0-9_]{2,64}", error["code"]):
                                code = error["code"]
                            if isinstance(error.get("message"), str) and len(error["message"]) <= 300:
                                message = error["message"]
                        except Exception:
                            pass
                    failure = IdentityError(code, status)
                    failure.user_message = message
                    raise failure
                chunks = []
                size = 0
                for part in response.iter_bytes():
                    size += len(part)
                    if size > 1024 * 1024:
                        raise IdentityError("WORK_BRIDGE_RESPONSE_TOO_LARGE", 503)
                    chunks.append(part)
                result = json.loads(b"".join(chunks))
    except IdentityError:
        raise
    except (Exception,):
        raise IdentityError("WORK_BRIDGE_UNAVAILABLE", 503) from None
    if not isinstance(result, dict):
        raise IdentityError("WORK_BRIDGE_INVALID_RESPONSE", 503)
    return result


def verify_legacy(work_user_id, password):
    result = request(
        "POST",
        "/api/auth/internal/identity/legacy-check",
        {"work_user_id": work_user_id, "password": password},
    )
    return result.get("verified") is True


def work_token(session_hash, work_user_id=None, *, timeout=90):
    result = request(
        "POST",
        "/api/auth/internal/identity/exchange",
        {"session_hash": session_hash, "work_user_id": work_user_id}, timeout=timeout,
    )
    token = result.get("token")
    if not isinstance(token, str) or len(token) > 16384 or len(token.split(".")) != 3:
        raise IdentityError("WORK_BRIDGE_INVALID_RESPONSE", 503)
    return token


def has_work_records(work_user_id, session_hash=None):
    result = request(
        "POST",
        "/api/auth/internal/identity/principal-status",
        {"work_user_id": work_user_id, "session_hash": session_hash},
    )
    if type(result.get("has_native_records")) is not bool:
        raise IdentityError("WORK_BRIDGE_INVALID_RESPONSE", 503)
    return result["has_native_records"]
