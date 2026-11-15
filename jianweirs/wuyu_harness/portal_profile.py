"""Own-account profile, read-only central points and independent product preferences."""
from fastapi import HTTPException, Request

from .identity import PROFILE_IDENTITIES


def install_profile_routes(app, *, current, runtime, object_body):
    def preferences(rt, user):
        return rt.limits.settings(user['id'], rt.config.read()['appearance']['default_font_size'])

    @app.get('/portal/api/profile')
    async def profile(request: Request):
        user, rt = current(request), runtime(request)
        shared = rt.identity.enabled
        account = (getattr(request.state, 'identity_session', None) or {}).get('account', {}) if shared else {}
        return {'user': user, 'account': {'name': user['display_name'], **account},
                'shared_account': shared, 'identity_options': list(PROFILE_IDENTITIES) if shared else [],
                'preferences': preferences(rt, user), 'account_url': rt.identity.public()['account_url'] if shared else None}

    @app.get('/portal/api/profile/credits')
    async def credits(request: Request):
        current(request)
        return await runtime(request).identity.credits(request)

    @app.patch('/portal/api/profile/preferences')
    async def update_preferences(request: Request):
        user, rt = current(request), runtime(request)
        return rt.limits.update_preferences(user['id'], await object_body(request),
                                           rt.config.read()['appearance']['default_font_size'])

    @app.patch('/portal/api/profile')
    async def update_profile(request: Request):
        user, rt = current(request), runtime(request)
        body = await object_body(request)
        required = {'name', 'organization', 'identity'} if rt.identity.enabled else {'name'}
        if set(body) != required or any(not isinstance(v, str) for v in body.values()):
            raise HTTPException(422, '个人资料字段无效')
        body = {k: v.strip() for k, v in body.items()}
        if not 1 <= len(body['name']) <= 60:
            raise HTTPException(422, '姓名需为1至60个字符')
        if rt.identity.enabled:
            if not 1 <= len(body['organization']) <= 160 or body['identity'] not in PROFILE_IDENTITIES:
                raise HTTPException(422, '请填写所在机构并选择身份')
            account = await rt.identity.update_profile(request, body, user['id'])
        else:
            account = {'name': body['name']}
        updated = rt.db.update_user(user['id'], {'display_name': account['name']})
        return {'user': updated, 'account': account}
