"""Live Wuyu account validation; product data and quotas stay in this product."""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import json
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import HTTPException

PROFILE_IDENTITIES = ('政府及事业单位', '企业', '科研院所', '高校教师', '学生', '其他')


def public_account(identity):
    """Never return the upstream user object, which may contain password hashes."""
    limits = {'name': 100, 'email': 254, 'organization': 160, 'identity': 40, 'legacy_alias': 100}
    return {name: value[:limit] if isinstance(value := identity.get(name), str) else ''
            for name, limit in limits.items()}


class WuyuIdentity:
    def __init__(self, folder: Path):
        self.enabled = os.getenv('WUYU_AUTH_MODE', 'local') == 'wuyu'
        self.authority = os.getenv('WUYU_IDENTITY_URL', 'https://test-ai.wuyullm.com').rstrip('/')
        parsed = urlsplit(self.authority)
        if self.enabled and (self.authority not in ('https://test-ai.wuyullm.com', 'http://127.0.0.1:18920')
                or parsed.username or parsed.password):
            raise ValueError('身份服务地址不在允许范围')
        self.client = httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False)
        keyfile = folder / '.identity-csrf.key'
        if not keyfile.exists():
            try:
                with keyfile.open('xb') as stream:
                    stream.write(secrets.token_bytes(32))
                keyfile.chmod(0o600)
            except FileExistsError:
                pass
        self.key = keyfile.read_bytes()

    def public(self):
        target = '/jianweirs/'
        return {'mode': 'wuyu' if self.enabled else 'local',
                'login_url': 'https://test-ai.wuyullm.com/?' + urlencode({'next': target}),
                'register_url': 'https://test-ai.wuyullm.com/?' + urlencode({'auth': 'register', 'next': target}),
                'account_url': 'https://test-ai.wuyullm.com/'}

    async def session(self, request, db):
        token = request.cookies.get('wuyu_staging_identity_session') or request.cookies.get('wuyu_staging_session', '')
        if not token:
            return None
        if len(token) > 200 or not re.fullmatch(r'[A-Za-z0-9_-]{20,200}', token):
            return None
        try:
            async with self.client.stream('GET', self.authority + '/api/me',
                    headers={'Host': 'test-ai.wuyullm.com', 'X-Wuyu-Client': '1',
                             'Cookie': 'wuyu_staging_identity_session=' + token}) as response:
                if response.status_code in (401, 403):
                    return None
                if response.status_code != 200:
                    raise HTTPException(503, '无隅 AI 账号服务暂不可用，请稍后重试')
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ValueError('oversized identity')
                identity = json.loads(raw)
            if (not isinstance(identity, dict) or not re.fullmatch(r'[a-f0-9]{32}', str(identity.get('id', '')))
                    or identity.get('active') not in (True, 1) or identity.get('role') not in ('admin', 'user')):
                raise ValueError('invalid identity')
            user = db.identity_user(identity)
            if not user['active']:
                return None
            csrf = hmac.new(self.key, ('jianweirs:' + token).encode(), hashlib.sha256).hexdigest()
            return {'user': user, 'csrf_token': csrf, 'account': public_account(identity)}
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, '无隅 AI 账号验证未完成，请稍后重试') from None

    async def _account_request(self, request, path, *, body=None):
        # Fixed, own-account endpoints only. No caller-controlled URLs or user IDs.
        if not self.enabled or path not in ('/api/work/quota', '/api/me/profile'):
            raise HTTPException(400, '账号操作不可用')
        token = request.cookies.get('wuyu_staging_identity_session') or request.cookies.get('wuyu_staging_session', '')
        if not re.fullmatch(r'[A-Za-z0-9_-]{20,200}', token):
            raise HTTPException(401, '请先登录')
        try:
            async with self.client.stream('PATCH' if body is not None else 'GET', self.authority + path,
                    headers={'Host': 'test-ai.wuyullm.com', 'X-Wuyu-Client': '1',
                             'Origin': 'https://test-ai.wuyullm.com',
                             'Cookie': 'wuyu_staging_identity_session=' + token}, json=body) as response:
                if response.status_code in (401, 403):
                    raise HTTPException(response.status_code, '账号登录状态或功能权限已变更')
                if response.status_code == 422:
                    raise HTTPException(422, '请检查姓名、机构和身份后重试')
                if response.status_code != 200:
                    raise ValueError('account service unavailable')
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise ValueError('oversized account response')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('invalid account response')
            return value
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, '平台账号信息暂时无法读取，请稍后重试') from None

    async def credits(self, request):
        if not self.enabled:
            return {'status': 'not_linked', 'message': '当前为本地账号，尚未关联无隅 AI 平台积分。'}
        try:
            value = await self._account_request(request, '/api/work/quota')
            available = value.get('available', {}).get('points')
            reserved = value.get('reserved', {}).get('points')
            if type(available) is not int or not 0 <= available <= 10**12:
                raise ValueError('invalid points')
            return {'status': 'available', 'available': available,
                    'reserved': reserved if type(reserved) is int and 0 <= reserved <= 10**12 else None,
                    'updated_at': datetime.now(timezone.utc).isoformat()}
        except HTTPException as error:
            if error.status_code == 401:
                raise
            if error.status_code == 403:
                return {'status': 'not_enabled', 'message': '此账号尚未开通平台工作积分，请联系平台管理员。'}
        except (ValueError, TypeError, AttributeError):
            pass
        return {'status': 'unavailable', 'message': '平台积分暂时未能读取，请刷新重试。'}

    async def update_profile(self, request, body, user_id):
        value = await self._account_request(request, '/api/me/profile', body=body)
        if 'wuyu_' + str(value.get('id')) != user_id:
            raise HTTPException(503, '账号资料返回异常，请刷新核对')
        return public_account(value)

    async def close(self):
        await self.client.aclose()
