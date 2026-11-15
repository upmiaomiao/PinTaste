"""Validated public addresses; credentials and business settings stay elsewhere."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
import re
from http.cookies import CookieError, SimpleCookie
from typing import Mapping
from urllib.parse import urlsplit


def _origin(value: str, *, public: bool = False) -> str:
    value = value.strip()
    if not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError('Origin must be a nonempty HTTP(S) address without whitespace')
    if any(char in value for char in ('\\', '?', '#')):
        raise ValueError('Origin cannot contain a query, fragment or backslash')
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError('Origin has an invalid host or port') from error
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {'', '/'} or port == 0
            or parsed.netloc.endswith(':')):
        raise ValueError('Origin must contain only a scheme, host and optional port')
    hostname = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == 'localhost'
    if public and parsed.scheme != 'https' and not loopback:
        raise ValueError('Public origin must use HTTPS; HTTP is allowed only for localhost')
    if ':' in hostname:
        hostname = '[' + hostname + ']'
    default_port = 443 if parsed.scheme == 'https' else 80
    authority = hostname + (':' + str(port) if port is not None and port != default_port else '')
    return parsed.scheme + '://' + authority


def _cookie_name(value: str) -> str:
    # RFC token characters; reject whitespace, separators and header injection.
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", value):
        raise ValueError('Cookie names must be nonempty ASCII tokens of at most 128 characters')
    try:
        cookie = SimpleCookie()
        cookie[value] = ''
    except CookieError as error:
        raise ValueError('Cookie names cannot be reserved cookie attributes') from error
    return value


def _cookie_domain(value: str, public: str, allowed_parent: str = '') -> str | None:
    if value == '':
        return None  # Omit Domain for a host-only cookie.
    domain = value.lower()
    host = urlsplit(public).hostname
    parent = allowed_parent.lower()
    explicit_parent = bool(parent and domain in (parent, '.' + parent) and host.endswith('.' + parent))
    checked = parent if explicit_parent else domain
    if (not (domain == host or explicit_parent)
            or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', checked)
            or any(not label or len(label) > 63 or label.startswith('-') or label.endswith('-') for label in checked.split('.'))
            or len(checked) > 253 or '.' not in checked):
        raise ValueError('Cookie domain must match the public hostname or its explicitly allowed parent')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return domain
    raise ValueError('IP origins must use host-only cookies')


@dataclass(frozen=True)
class RuntimeConfig:
    public_origin: str
    allowed_origins: frozenset[str]
    chat_execution_mode: str = 'combined'
    session_cookie: str = 'wuyu_identity_session'
    legacy_session_cookie: str = 'wuyu_session'
    work_session_cookie: str = 'banana_auth_token'
    cookie_domain: str | None = None
    task_queue_warn_seconds: int = 300
    login_alias: str | None = None
    login_alias_user_id: str | None = None

    @classmethod
    def from_env(cls, environment: Mapping[str, str]) -> 'RuntimeConfig':
        public = _origin(environment.get('WUYU_PUBLIC_ORIGIN', 'http://127.0.0.1:8920'), public=True)
        defaults = public
        if 'WUYU_PUBLIC_ORIGIN' not in environment:
            defaults += ',http://localhost:8920'
        raw_allowed = environment.get('WUYU_ALLOWED_ORIGINS', defaults)
        if any(not entry.strip() for entry in raw_allowed.split(',')):
            raise ValueError('Allowed origins cannot contain empty entries')
        allowed = frozenset(_origin(entry) for entry in raw_allowed.split(','))
        if public not in allowed:
            raise ValueError('WUYU_ALLOWED_ORIGINS must include WUYU_PUBLIC_ORIGIN')
        mode = environment.get('WUYU_CHAT_EXECUTION_MODE', 'combined')
        if mode not in {'combined', 'api', 'worker'}:
            raise ValueError('WUYU_CHAT_EXECUTION_MODE must be combined, api or worker')
        names = tuple(_cookie_name(environment.get(key, default)) for key, default in (
            ('WUYU_SESSION_COOKIE', 'wuyu_identity_session'),
            ('WUYU_LEGACY_SESSION_COOKIE', 'wuyu_session'),
            ('WUYU_WORK_SESSION_COOKIE', 'banana_auth_token'),
        ))
        if len(set(names)) != len(names):
            raise ValueError('Session, legacy and work cookie names must be distinct')
        domain = _cookie_domain(environment.get('WUYU_COOKIE_DOMAIN', ''), public,
                                environment.get('WUYU_COOKIE_PARENT_DOMAIN', ''))
        if domain and any(name.startswith('__Host-') for name in names):
            raise ValueError('__Host- cookies cannot include a Domain attribute')
        raw_wait = environment.get('WUYU_TASK_QUEUE_WARN_SECONDS', '300')
        if not re.fullmatch(r'[0-9]{1,5}', raw_wait) or not 30 <= int(raw_wait) <= 86400:
            raise ValueError('WUYU_TASK_QUEUE_WARN_SECONDS must be an integer from 30 to 86400')
        alias = environment.get('WUYU_LOGIN_ALIAS', '')
        alias_user_id = environment.get('WUYU_LOGIN_ALIAS_USER_ID', '')
        if (alias or alias_user_id) and (not re.fullmatch(r'[A-Za-z0-9_]{3,80}', alias)
                or not re.fullmatch(r'[0-9a-f]{32}', alias_user_id)):
            raise ValueError('Login alias and platform user ID must be configured together')
        return cls(public_origin=public, allowed_origins=allowed, chat_execution_mode=mode,
                   session_cookie=names[0], legacy_session_cookie=names[1],
                   work_session_cookie=names[2], cookie_domain=domain, task_queue_warn_seconds=int(raw_wait),
                   login_alias=alias or None, login_alias_user_id=alias_user_id or None)

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset(urlsplit(origin).netloc for origin in self.allowed_origins)

    def public_url(self, path: str) -> str:
        if (not path.startswith('/') or path.startswith('//')
                or '\\' in path or '#' in path
                or any(ord(char) <= 32 or ord(char) == 127 for char in path)):
            raise ValueError('Public URL path must be a local absolute path')
        return self.public_origin + path


settings = RuntimeConfig.from_env(os.environ)
