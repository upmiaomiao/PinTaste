"""Personal design-habit proxy routes: own work principal only, validated before the bridge is used."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from . import auth, integrated_identity as identity, integrated_identity_api as api

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:  # Route functions stay testable where only the model layer is installed.
    TestClient = None

HABIT_ID = '0b0f6f4e-6c59-4a55-9d0e-3c5d6f1f2a10'


def member(work=True):
    return {'id': 'u1', 'active': True, 'role': 'user', 'permissions': {'work': work, 'chat': True}}


class Bridge:
    """Records what would be sent to the work service."""

    def __init__(self, failure=None):
        self.calls, self.sessions, self.failure = [], [], failure

    def work_token(self, session_hash, work_user_id=None, **_):
        self.sessions.append((session_hash, work_user_id))
        return 'header.payload.signature'

    def request(self, method, path, payload=None, *, token=None, **_):
        # Same path rule as the real identity_bridge.request, so a route it would refuse fails here too.
        if not path.startswith('/api/') or '?' in path or '..' in path:
            raise identity.IdentityError('INVALID_BRIDGE_PATH', 503)
        self.calls.append((method, path, payload, token))
        if self.failure:
            raise self.failure
        return {'data': {'echo': path}}


class DesignHabitProxyTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge()
        self.patches = [patch.object(api.bridge, 'work_token', self.bridge.work_token),
                        patch.object(api.bridge, 'request', self.bridge.request)]
        for item in self.patches:
            item.start()
        self.request = SimpleNamespace(cookies={auth.COOKIE: 'session-token'})

    def tearDown(self):
        for item in self.patches:
            item.stop()

    def test_routes_map_to_the_callers_own_habits(self):
        user, request = member(), self.request
        create = api.DesignHabitInput(category='layout', slot='palette', rule='主色用深蓝', scope={'purpose': 'research'})
        update = api.DesignHabitPatchInput(version=3, paused=True)
        api.design_habits(request, user)
        api.design_habit_create(create, request, user)
        api.design_habit_update(HABIT_ID, update, request, user)
        api.design_habit_delete(HABIT_ID, request, user)
        api.design_habits_settings(api.DesignHabitSettingsInput(paused=True), request, user)
        api.design_habits_reset(api.DesignHabitResetInput(confirm=True), request, user)
        result = api.design_habits_export(request, user)
        base = '/api/design-memory/habits'
        self.assertEqual([call[:3] for call in self.bridge.calls], [
            ('GET', base, None),
            ('POST', base, {'category': 'layout', 'slot': 'palette', 'rule': '主色用深蓝', 'scope': {'purpose': 'research'}}),
            ('PATCH', base + '/' + HABIT_ID, {'version': 3, 'paused': True}),
            ('DELETE', base + '/' + HABIT_ID, None),
            ('PUT', base + '/settings', {'paused': True}),
            ('POST', base + '/reset', {'confirm': True}),
            ('GET', base + '/export', None),
        ])
        self.assertEqual(result, {'echo': base + '/export'})
        # Every call uses a token exchanged for the caller's own session, never a chosen principal.
        self.assertEqual({call[3] for call in self.bridge.calls}, {'header.payload.signature'})
        self.assertEqual(set(self.bridge.sessions), {(api.db.digest('session-token'), None)})

    def test_work_feature_is_required_before_any_bridge_call(self):
        actions = [
            lambda user: api.design_habits(self.request, user),
            lambda user: api.design_habits_export(self.request, user),
            lambda user: api.design_habit_delete(HABIT_ID, self.request, user),
            lambda user: api.design_habits_reset(api.DesignHabitResetInput(confirm=True), self.request, user),
        ]
        for user in (member(work=False), member() | {'active': False}):
            for action in actions:
                with self.assertRaises(HTTPException) as caught:
                    action(user)
                self.assertEqual((caught.exception.status_code, caught.exception.detail), (403, 'FEATURE_DISABLED'))
        self.assertEqual((self.bridge.calls, self.bridge.sessions), ([], []))

    def test_invalid_ids_and_bodies_never_reach_the_work_service(self):
        patch_body = api.DesignHabitPatchInput(version=1, pinned=True)
        for habit_id in ('', 'a' * 37, '../admin', 'a/b', 'a?b', 'settings\n'):
            for action in (lambda: api.design_habit_delete(habit_id, self.request, member()),
                           lambda: api.design_habit_update(habit_id, patch_body, self.request, member())):
                with self.assertRaises(HTTPException) as caught:
                    action()
                self.assertEqual((caught.exception.status_code, caught.exception.detail), (400, 'INVALID_HABIT_ID'))
        with self.assertRaises(HTTPException) as caught:
            api.design_habit_update(HABIT_ID, api.DesignHabitPatchInput(version=1), self.request, member())
        self.assertEqual(caught.exception.detail, 'EMPTY_HABIT_PATCH')
        with self.assertRaises(HTTPException) as caught:
            api.design_habits_reset(api.DesignHabitResetInput(confirm=False), self.request, member())
        self.assertEqual(caught.exception.detail, 'CONFIRMATION_REQUIRED')
        for body, code in ((api.DesignHabitInput(category='layout', slot='palette', rule='规则', scope={'purpose': ' '}), 'INVALID_HABIT_SCOPE'),
                           (api.DesignHabitInput(category='layout', slot='palette', rule='规则', scope={'purpose': '长' * 61}), 'INVALID_HABIT_SCOPE'),
                           (api.DesignHabitInput(category='layout', slot='palette', rule='规则', value={'text': '长' * 2000}), 'INVALID_HABIT_VALUE'),
                           (api.DesignHabitInput(category='layout', slot='palette', rule='规则', value={'text': '长' * 400}), 'INVALID_HABIT_VALUE')):
            with self.assertRaises(HTTPException) as caught:
                api.design_habit_create(body, self.request, member())
            self.assertEqual((caught.exception.status_code, caught.exception.detail), (400, code))
        self.assertEqual((self.bridge.calls, self.bridge.sessions), ([], []))

    def test_export_is_fetched_page_by_page(self):
        # One bridge answer is limited to 1 MiB; the work service pages the export and the proxy passes the position on.
        cursor = 'e.20261118T101112.123456.' + HABIT_ID
        api.design_habits_export(self.request, member())
        api.design_habits_export(self.request, member(), cursor)
        api.design_habits_export(self.request, member(), 'h.99991231T235959.999999.' + 'f' * 8 + '-ffff-ffff-ffff-' + 'f' * 12)
        base = '/api/design-memory/habits/export'
        self.assertEqual([call[:3] for call in self.bridge.calls], [('GET', base, None), ('GET', base + '/' + cursor, None),
            ('GET', base + '/h.99991231T235959.999999.ffffffff-ffff-ffff-ffff-ffffffffffff', None)])
        sent = len(self.bridge.calls)
        for bad in ('', 'e', cursor + '&owner_id=u2', cursor + '/../admin', 'x.' + cursor[2:], cursor + '\n', '../settings'):
            with self.assertRaises(HTTPException) as caught:
                api.design_habits_export(self.request, member(), bad)
            self.assertEqual((caught.exception.status_code, caught.exception.detail), (400, 'INVALID_EXPORT_CURSOR'))
        self.assertEqual(len(self.bridge.calls), sent)

    def test_models_reject_unknown_or_out_of_range_fields(self):
        valid = {'category': 'expression', 'slot': 'real_photos', 'rule': '配图尽量使用真实照片'}
        api.DesignHabitInput(**valid)
        for extra in ({'category': 'system'}, {'slot': 'Bad-Slot'}, {'slot': 'a' * 61}, {'rule': ''}, {'rule': '长' * 301},
                      {'scope': {'owner_id': 'u2'}}, {'scope': {'purpose': 3}}, {'pinned': 'yes'}, {'owner_id': 'u2'},
                      {'origin': 'inferred'}, {'status': 'active'}, {'layer': 'system'}):
            with self.assertRaises(ValidationError, msg=extra):
                api.DesignHabitInput(**(valid | extra))
        api.DesignHabitPatchInput(version=2, status='revoked', scope={})
        for body in ({}, {'version': '2'}, {'version': 0}, {'version': 2, 'status': 'candidate'}, {'version': 2, 'rule': ''},
                     {'version': 2, 'paused': 1}, {'version': 2, 'owner_id': 'u2'}, {'version': 2, 'category': 'layout'}):
            with self.assertRaises(ValidationError, msg=body):
                api.DesignHabitPatchInput(**body)
        for model, body in ((api.DesignHabitSettingsInput, {}), (api.DesignHabitSettingsInput, {'paused': 'true'}),
                            (api.DesignHabitSettingsInput, {'paused': True, 'owner_id': 'u2'}),
                            (api.DesignHabitResetInput, {}), (api.DesignHabitResetInput, {'confirm': 'yes'})):
            with self.assertRaises(ValidationError, msg=body):
                model(**body)

    def test_work_service_reason_is_forwarded(self):
        failure = identity.IdentityError('REVISION_CONFLICT', 409)
        failure.user_message = '这条习惯已更新，请刷新。'
        self.bridge.failure = failure
        with self.assertRaises(HTTPException) as caught:
            api.design_habit_update(HABIT_ID, api.DesignHabitPatchInput(version=1, pinned=True), self.request, member())
        error = caught.exception
        self.assertEqual((error.status_code, error.detail), (409, 'REVISION_CONFLICT'))
        self.assertEqual(api.parse_qsl('m=' + error.headers['X-Wuyu-Message'])[0][1], failure.user_message)
        # An un-migrated work service answers 503; the bridge reports it without a reason.
        self.bridge.failure = identity.IdentityError('WORK_BRIDGE_REJECTED', 503)
        with self.assertRaises(HTTPException) as caught:
            api.design_habits(self.request, member())
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (503, 'WORK_BRIDGE_REJECTED'))


@unittest.skipIf(TestClient is None, 'fastapi.testclient is not installed')
class DesignHabitRoutingTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(api.router)
        self.app, self.bridge = app, Bridge()
        self.patches = [patch.object(api.bridge, 'work_token', self.bridge.work_token),
                        patch.object(api.bridge, 'request', self.bridge.request)]
        for item in self.patches:
            item.start()
        self.client = TestClient(app, base_url='http://127.0.0.1:8920')
        self.headers = {'Cookie': auth.COOKIE + '=session-token'}

    def tearDown(self):
        for item in self.patches:
            item.stop()

    def test_http_methods_paths_and_validation(self):
        self.app.dependency_overrides[auth.current] = member
        base, send = '/api/work/design-habits', self.client.request
        ok = [('GET', base, None), ('GET', base + '/export', None), ('PUT', base + '/settings', {'paused': False}),
              ('POST', base + '/reset', {'confirm': True}), ('DELETE', base + '/' + HABIT_ID, None),
              ('POST', base, {'category': 'integration', 'slot': 'title_style', 'rule': '标题写成结论'}),
              ('PATCH', base + '/' + HABIT_ID, {'version': 1, 'status': 'active'})]
        for method, path, body in ok:
            response = send(method, path, json=body, headers=self.headers)
            self.assertEqual(response.status_code, 200, (method, path, response.text))
        self.assertEqual([call[1] for call in self.bridge.calls], [
            '/api/design-memory/habits', '/api/design-memory/habits/export', '/api/design-memory/habits/settings',
            '/api/design-memory/habits/reset', '/api/design-memory/habits/' + HABIT_ID, '/api/design-memory/habits',
            '/api/design-memory/habits/' + HABIT_ID])
        cursor = 'e.20261118T101112.123456.' + HABIT_ID
        self.assertEqual(send('GET', base + '/export?cursor=' + cursor, headers=self.headers).status_code, 200)
        self.assertEqual(self.bridge.calls[-1][1], '/api/design-memory/habits/export/' + cursor)
        sent = len(self.bridge.calls)
        for method, path, body, status in [('POST', base, {'category': 'layout', 'slot': 'palette'}, 422),
                                           ('GET', base + '/export?cursor=..%2Fsettings', None, 400),
                                           ('POST', base, {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'owner_id': 'u2'}, 422),
                                           ('PATCH', base + '/' + HABIT_ID, {'pinned': True}, 422),
                                           ('PATCH', base + '/' + HABIT_ID, {'version': 1}, 400),
                                           ('PUT', base + '/settings', {'paused': 'no'}, 422),
                                           ('POST', base + '/reset', {}, 422),
                                           ('POST', base + '/reset', {'confirm': False}, 400),
                                           ('DELETE', base + '/' + 'a' * 37, None, 400)]:
            response = send(method, path, json=body, headers=self.headers)
            self.assertEqual(response.status_code, status, (method, path, response.text))
        self.assertEqual(len(self.bridge.calls), sent)

    def test_login_is_required(self):
        def anonymous():
            raise HTTPException(401, '请先登录。')
        self.app.dependency_overrides[auth.current] = anonymous
        base = '/api/work/design-habits'
        for method, path, body in [('GET', base, None), ('GET', base + '/export', None), ('DELETE', base + '/' + HABIT_ID, None),
                                   ('POST', base + '/reset', {'confirm': True}), ('PUT', base + '/settings', {'paused': True})]:
            self.assertEqual(self.client.request(method, path, json=body).status_code, 401, (method, path))
        self.assertEqual(self.bridge.calls, [])


if __name__ == '__main__':
    unittest.main()
