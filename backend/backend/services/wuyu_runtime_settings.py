"""Private versioned image settings; queued jobs contain only immutable references.

The file belongs to the backend instance, never the public uploads directory.
Old revisions retain credentials so changing the active profile cannot redirect
an already queued job. Public views expose credential presence only.
"""
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid
from urllib.parse import urlsplit
from .wuyu_research_policy import validate_research_policy


PROFILE_MODELS = {'gpt': 'gpt-image-2.5-sunburst-ssvip', 'gemini': 'gemini-nano-banana-2.1'}
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class SettingsConflict(ValueError):
    """The administrator edited a stale settings revision."""


def _path(config):
    value = config.get('WUYU_RUNTIME_SETTINGS_PATH')
    return Path(value) if value else Path(__file__).resolve().parents[1] / 'instance' / 'wuyu-runtime-settings.json'


@contextlib.contextmanager
def _locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(str(path.resolve()), threading.RLock())
    with lock:
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'r+b') as stream:
            if os.name == 'nt':
                import msvcrt
                if stream.seek(0, 2) == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == 'nt':
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _write(path, store):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w', encoding='utf-8') as stream:
            json.dump(store, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _setting(config, *keys):
    return next((config.get(key) or os.getenv(key) for key in keys if config.get(key) or os.getenv(key)), None)


def _gateway_origin(config):
    from .wuyu_providers import validated_model_gateway_origin, model_gateway_expected_port
    from .wuyu_errors import PipelineError
    try:
        return validated_model_gateway_origin(config.get('WUYU_MODEL_GATEWAY_ORIGIN', os.getenv('WUYU_MODEL_GATEWAY_ORIGIN')), model_gateway_expected_port(config))
    except PipelineError as error:
        raise ValueError(str(error)) from None


def _validate_base(value, testing=False, gateway_origin=None, expected_port=8140):
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError('模型地址必须为有效的 HTTPS URL。')
    parsed = urlsplit(value)
    from .wuyu_providers import is_trusted_model_gateway_base
    from .wuyu_errors import PipelineError
    try:
        trusted = is_trusted_model_gateway_base(value, gateway_origin, expected_port)
    except PipelineError as error:
        raise ValueError(str(error)) from None
    if (not trusted and parsed.scheme not in (('https', 'http') if testing else ('https',))
            or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('模型地址须为 HTTPS，且不能包含账号、密钥参数或片段。')
    return value.rstrip('/')


def _authentication_header(name):
    identity = ''.join(char for char in name.lower() if char.isalnum())
    return identity.endswith('key') or any(part in identity for part in
        ('authorization', 'cookie', 'apikey', 'token', 'secret', 'credential')) or identity in {'auth', 'xauth'}


def _validate_profile_protocol(name, profile, testing=False, gateway_origin=None, expected_port=8140):
    if profile['protocol'] == 'gemini':
        if name != 'gemini':
            raise ValueError('Gemini原生协议只能为Gemini生图模型配置。')
        from .wuyu_providers import gemini_native_base_url
        from .wuyu_errors import PipelineError
        try:
            gemini_native_base_url(profile['base_url'], testing, gateway_origin, expected_port)
        except PipelineError as error:
            raise ValueError(str(error)) from None


def _initial(config):
    gateway_origin = _gateway_origin(config)
    from .wuyu_providers import model_gateway_expected_port
    expected_port = model_gateway_expected_port(config)
    base = _setting(config, 'WUYU_IMAGE_BASE_URL', 'WUYU_IMAGE_API_BASE', 'IMAGE_API_BASE', 'OPENAI_API_BASE') or 'https://www.dmxapi.cn/v1'
    key = _setting(config, 'WUYU_IMAGE_API_KEY', 'WUYU_IMAGE_APIKEY', 'IMAGE_API_KEY', 'OPENAI_API_KEY') or ''
    headers = _setting(config, 'WUYU_IMAGE_DEFAULT_HEADERS', 'WUYU_DEFAULT_HEADERS') or {}
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except ValueError:
            raise ValueError('模型附加 HTTP 头配置无效。') from None
    if not isinstance(headers, dict) or len(headers) > 16 or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
        raise ValueError('模型附加 HTTP 头配置无效。')
    model = _setting(config, 'WUYU_IMAGE_MODEL') or PROFILE_MODELS['gpt']
    selected = next((k for k, v in PROFILE_MODELS.items() if v == model), None)
    protocol = _setting(config, 'WUYU_IMAGE_API_PROTOCOL', 'IMAGE_API_PROTOCOL') or 'images'
    if selected is None or protocol not in {'images', 'chat', 'gemini'} or protocol == 'gemini' and selected != 'gemini':
        raise ValueError('当前生图模型或协议未在两种已授权配置中。')
    profile = {'base_url': _validate_base(base, config.get('TESTING'), gateway_origin, expected_port), 'api_key': key, 'default_headers': headers, 'protocol': protocol}
    revision = {'profile_id': selected, 'image_workers': 8, 'trace_workers': 8,
                'research_policy': validate_research_policy(config.get('WUYU_RESEARCH_POLICY', {})),
                'profiles': {name: dict(copy.deepcopy(profile), model=identity) for name, identity in PROFILE_MODELS.items()}}
    if gateway_origin:
        revision['model_gateway_origin'] = gateway_origin
    if protocol == 'gemini':
        revision['profiles']['gpt']['protocol'] = 'images'
    for name, configured in revision['profiles'].items():
        _validate_profile_protocol(name, configured, config.get('TESTING'), gateway_origin, expected_port)
    return {'schema_version': 1, 'store_id': uuid.uuid4().hex,
            'current_revision': 1, 'revisions': {'1': revision}}


def _read(path, config):
    if not path.is_file():
        store = _initial(config)
        _write(path, store)
        return store
    try:
        store = json.loads(path.read_text(encoding='utf-8'))
        if store['schema_version'] != 1 or str(store['current_revision']) not in store['revisions']:
            raise ValueError()
        identity = store.get('store_id')
        if not isinstance(identity, str) or len(identity) != 32 or uuid.UUID(identity).hex != identity:
            # A fresh identity must never be silently assigned to saved revisions:
            # queued jobs need the original store, not another revision numbered 1.
            raise ValueError()
        return store
    except (ValueError, KeyError, TypeError):
        raise ValueError('运行配置文件损坏，请由管理员恢复；任务不会自动切换模型。') from None


def _snapshot(version, revision, store_id, profile_id=None):
    selected = revision['profile_id'] if profile_id in (None, 'default') else profile_id
    if not isinstance(selected, str) or selected not in PROFILE_MODELS:
        raise ValueError('请选择已授权的 GPT 或 Gemini 生图模型。')
    profile = revision['profiles'][selected]
    result = {'schema_version': 1, 'store_id': store_id,
              'settings_version': version, 'profile_id': selected,
              'image_model': profile['model'], 'image_protocol': profile['protocol'],
              'image_workers': revision['image_workers'], 'trace_workers': revision['trace_workers']}
    if revision.get('model_gateway_origin'):
        result['model_gateway_origin'] = revision['model_gateway_origin']
    result['fingerprint'] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return result


def _public(store):
    version = store['current_revision']
    revision = store['revisions'][str(version)]
    view = _snapshot(version, revision, store['store_id'])
    view['research_policy'] = validate_research_policy(revision.get('research_policy', {}))
    view['profiles'] = [{'id': name, 'model': p['model'], 'protocol': p['protocol'],
                         'base_url': p['base_url'], 'api_key_configured': bool(p['api_key'])}
                        for name in PROFILE_MODELS for p in [revision['profiles'][name]]]
    return view


def get_runtime_settings(config):
    """Return administrator-safe settings without keys or private request headers."""
    path = _path(config)
    with _locked(path):
        return _public(_read(path, config))


def get_research_policy(config, runtime_snapshot=None):
    """Resolve an immutable admin revision; never alter old image-job references."""
    path = _path(config)
    with _locked(path):
        store = _read(path, config)
        version = store['current_revision']
        if runtime_snapshot is not None:
            try:
                version = runtime_snapshot['settings_version']
                if type(version) is not int:
                    raise ValueError()
                revision = store['revisions'][str(version)]
                if runtime_snapshot != _snapshot(version, revision, store['store_id'], runtime_snapshot['profile_id']):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise ValueError('材料阅读绑定的管理员配置版本无效。') from None
        return validate_research_policy(store['revisions'][str(version)].get('research_policy', {}))


def freeze_runtime_config(config, profile_id=None):
    """Persist the current private revision and return a key-free queued-job reference."""
    path = _path(config)
    with _locked(path):
        store = _read(path, config)
        version = store['current_revision']
        return _snapshot(version, store['revisions'][str(version)], store['store_id'], profile_id)


def get_generation_options(config):
    """Authenticated users see model choices, never private connection settings."""
    path = _path(config)
    with _locked(path):
        store = _read(path, config)
        revision = store['revisions'][str(store['current_revision'])]
        return {'default_profile': revision['profile_id'],
                'profiles': [{'id': name, 'label': 'GPT Image' if name == 'gpt' else 'Gemini',
                              'model': revision['profiles'][name]['model'],
                              'available': bool(revision['profiles'][name]['api_key'])}
                             for name in PROFILE_MODELS]}


def resolve_runtime_snapshot(config, snapshot):
    """Resolve the exact historical revision into a worker-local configuration copy."""
    path = _path(config)
    with _locked(path):
        if not path.is_file():
            raise ValueError('任务的私有模型配置版本已丢失，请由管理员恢复后重试。')
        store = _read(path, config)
        try:
            version = snapshot['settings_version']
            if type(version) is not int:
                raise ValueError()
            revision = store['revisions'][str(version)]
            if snapshot != _snapshot(version, revision, store['store_id'], snapshot['profile_id']):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ValueError('任务的模型配置版本无效或已丢失，不能切换到当前配置重试。') from None
        profile = revision['profiles'][snapshot['profile_id']]
        if not profile['api_key']:
            raise ValueError('任务提交时没有配置生图密钥，请由管理员设置后重新提交任务。')
        result = dict(config)
        result.update(WUYU_IMAGE_MODEL=profile['model'], WUYU_IMAGE_PROVIDER='openai',
                      WUYU_IMAGE_BASE_URL=profile['base_url'], WUYU_IMAGE_API_KEY=profile['api_key'],
                      WUYU_IMAGE_DEFAULT_HEADERS=copy.deepcopy(profile['default_headers']),
                      WUYU_IMAGE_API_PROTOCOL=profile['protocol'],
                      WUYU_IMAGE_WORKERS=revision['image_workers'], WUYU_TRACE_WORKERS=revision['trace_workers'],
                      WUYU_MODEL_GATEWAY_ORIGIN=revision.get('model_gateway_origin') or '',
                      WUYU_RUNTIME_SNAPSHOT=copy.deepcopy(snapshot))
        return result


def update_runtime_settings(config, payload):
    """Validate an admin update, then append a private immutable configuration revision."""
    if not isinstance(payload, dict) or set(payload) - {'expected_revision', 'profile_id', 'image_workers', 'trace_workers', 'profiles', 'research_policy'}:
        raise ValueError('包含不支持的运行配置字段。')
    if type(payload.get('expected_revision')) is not int:
        raise ValueError('请提供当前配置版本后保存。')
    path = _path(config)
    with _locked(path):
        store = _read(path, config)
        current = store['current_revision']
        if payload['expected_revision'] != current:
            raise SettingsConflict('配置已被其他管理员更新，请刷新后保存。')
        revision = copy.deepcopy(store['revisions'][str(current)])
        if 'research_policy' in payload:
            revision['research_policy'] = validate_research_policy(payload['research_policy'], revision.get('research_policy', {}))
        from .wuyu_providers import model_gateway_expected_port
        expected_port = model_gateway_expected_port(config)
        gateway_origin = _gateway_origin(config) or revision.get('model_gateway_origin')
        if gateway_origin:
            revision['model_gateway_origin'] = gateway_origin
        for field in ('image_workers', 'trace_workers'):
            if field in payload:
                if type(payload[field]) is not int or not 1 <= payload[field] <= 16:
                    raise ValueError('生图和描摹并发数必须为 1–16 的整数。')
                revision[field] = payload[field]
        if 'profile_id' in payload:
            if not isinstance(payload['profile_id'], str) or payload['profile_id'] not in PROFILE_MODELS:
                raise ValueError('请选择已授权的 GPT 或 Gemini 生图模型。')
            revision['profile_id'] = payload['profile_id']
        profiles = payload.get('profiles', {})
        if not isinstance(profiles, dict) or set(profiles) - PROFILE_MODELS.keys():
            raise ValueError('生图模型配置无效。')
        for name, changes in profiles.items():
            if not isinstance(changes, dict) or set(changes) - {'base_url', 'api_key', 'protocol'}:
                raise ValueError('生图模型配置字段无效。')
            original = revision['profiles'][name]
            previous_connection = (original['base_url'], original['api_key'])
            if 'protocol' in changes:
                if not isinstance(changes['protocol'], str) or changes['protocol'] not in {'images', 'chat', 'gemini'}:
                    raise ValueError('协议仅支持显式 images、chat 或 gemini，不会自动回退。')
                revision['profiles'][name]['protocol'] = changes['protocol']
            if 'base_url' in changes:
                revision['profiles'][name]['base_url'] = _validate_base(changes['base_url'], config.get('TESTING'), gateway_origin, expected_port)
            if 'api_key' in changes:
                key = changes['api_key']
                if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c in key for c in '\r\n'):
                    raise ValueError('新密钥不能为空且不能包含换行；不填写将保留原密钥。')
                revision['profiles'][name]['api_key'] = key.strip()
            profile = revision['profiles'][name]
            _validate_profile_protocol(name, profile, config.get('TESTING'), gateway_origin, expected_port)
            if (profile['base_url'], profile['api_key']) != previous_connection:
                # SDK default_headers override its api_key-derived Authorization.
                # Carrying old credentials across this change could authenticate
                # with the wrong account or send a secret to a new endpoint.
                # Only this new revision changes; historical jobs retain theirs.
                profile['default_headers'] = {key: value for key, value in profile['default_headers'].items()
                                              if not _authentication_header(key)}
        version = current + 1
        store['revisions'][str(version)] = revision
        store['current_revision'] = version
        _write(path, store)
        return _public(store)
