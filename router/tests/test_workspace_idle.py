"""Disconnected workspace retirement; all data and model services are synthetic."""
import asyncio
import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from unittest.mock import AsyncMock, patch
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

spec = importlib.util.spec_from_file_location('idle_router', pathlib.Path(__file__).resolve().parents[1] / 'wuyu_tenant_router.py')
router = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = router
spec.loader.exec_module(router)
USER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
LEGACY = '33333333-3333-4333-8333-333333333333'
ITEM = {'id': 'session-44444444-4444-4444-8444-444444444444', 'title': 'Fixture',
    'updated_at': 1700000000, 'blank': False, 'running': False}


class Process:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None

    async def wait(self):
        return self.returncode


class IdleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        (self.root / 'seed').mkdir()
        self.dbpath = self.root / 'jobs.db'
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.executescript('CREATE TABLE tasks(status TEXT); CREATE TABLE wuyu_workflow_jobs(status TEXT);')
        self.config = {'tenant_root': str(self.root / 'accounts'), 'seed_dir': str(self.root / 'seed'),
            'legacy_user_id': LEGACY, 'max_tenants': 1, 'port_start': 33100,
            'public_origin': 'https://fixture.invalid', 'idle_seconds': 900,
            'idle_check_seconds': 30, 'idle_database_path': str(self.dbpath)}
        self.manager = router.TenantManager(self.config)
        self.client = object()
        self.running = self.enterContext(patch.object(router, 'host_running', AsyncMock(return_value=0)))
        self.history = self.enterContext(patch.object(router, 'host_history', AsyncMock(return_value=[dict(ITEM)])))
        self.enterContext(patch.object(router, 'probe_workspace_port'))
        self.kills = []
        self.enterContext(patch.object(router.os, 'killpg', self.kill))

    def kill(self, pid, signum):
        self.kills.append((pid, signum))
        for tenant in self.manager.tenants.values():
            for p in tenant.processes:
                if p.pid == pid:
                    p.returncode = 0

    def tenant(self, ident=USER, *, legacy=False):
        directory = self.manager.root / ident
        (directory / 'home').mkdir(parents=True, exist_ok=True)
        (directory / 'workspace').mkdir(exist_ok=True)
        (directory / 'workspace' / 'retained.png').write_bytes(b'unchanged-image')
        t = router.Tenant(ident, 'http://127.0.0.1:33100', 'http://127.0.0.1:33101',
            directory / 'actor.jwt', 'fixture-grant', str(directory / 'workspace'),
            legacy=legacy, processes=(Process(101), Process(102)), last_activity=0)
        self.manager.tenants[ident] = t
        return t

    async def test_idle_stops_both_processes_frees_slot_and_preserves_files(self):
        t = self.tenant()
        await self.manager.reap_idle(self.client)
        self.assertNotIn(USER, self.manager.tenants)
        self.assertEqual(len(self.kills), 2)
        self.assertEqual((pathlib.Path(t.workspace) / 'retained.png').read_bytes(), b'unchanged-image')
        self.assertEqual(self.manager.cached_history({'id': USER}), [ITEM])
        self.assertEqual(self.manager.idle_counters['retired'], 1)

    async def test_connected_workspace_is_pinned(self):
        t = self.tenant()
        self.manager.acquire(t)
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])
        self.assertEqual(self.running.await_count, 0)
        self.manager.release(t)
        await self.manager.reap_idle(self.client)
        self.assertNotIn(USER, self.manager.tenants)

    async def test_recent_activity_is_not_reaped(self):
        t = self.tenant()
        t.last_activity = router.time.monotonic()
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_legacy_shared_host_is_never_reaped(self):
        self.tenant(LEGACY, legacy=True)
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_native_running_and_unknown_status_block_retirement(self):
        self.tenant()
        for status in (1, None):
            self.running.return_value = status
            await self.manager.reap_idle(self.client)
            self.assertIn(USER, self.manager.tenants)
        self.assertEqual(self.kills, [])

    async def test_running_history_after_zero_count_blocks_retirement(self):
        self.tenant()
        self.history.return_value = [ITEM | {'running': True}]
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_unknown_history_response_does_not_stop_or_write_cache(self):
        self.tenant()
        self.history.side_effect = router.web.HTTPServiceUnavailable()
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])
        self.assertFalse((self.manager.root / USER / 'idle-history.json').exists())

    async def test_queued_running_null_and_unknown_backend_states_block(self):
        self.tenant()
        for table in ('tasks', 'wuyu_workflow_jobs'):
            for state in ('queued', 'RUNNING', None, 'unrecognized'):
                with closing(sqlite3.connect(self.dbpath)) as db, db:
                    db.execute(f'INSERT INTO {table} VALUES (?)', (state,))
                await self.manager.reap_idle(self.client)
                self.assertEqual(self.kills, [])
                with closing(sqlite3.connect(self.dbpath)) as db, db:
                    db.execute(f'DELETE FROM {table}')

    async def test_all_terminal_backend_states_are_safe(self):
        self.tenant()
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            for table in ('tasks', 'wuyu_workflow_jobs'):
                db.executemany(f'INSERT INTO {table} VALUES (?)', [(s,) for s in
                    ('COMPLETED', 'failed', 'cancelled', 'canceled', 'interrupted')])
        await self.manager.reap_idle(self.client)
        self.assertNotIn(USER, self.manager.tenants)

    async def test_backend_job_arriving_during_host_probe_blocks_retirement(self):
        self.tenant()
        with patch.object(self.manager, 'backend_idle', side_effect=[True, False]):
            await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_partial_is_terminal_only_for_legacy_tasks(self):
        self.tenant()
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute('INSERT INTO tasks VALUES (?)', ('PARTIAL',))
        self.assertIs(self.manager.backend_idle(), True)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute('INSERT INTO wuyu_workflow_jobs VALUES (?)', ('PARTIAL',))
        self.assertIs(self.manager.backend_idle(), False)
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_missing_locked_or_wrong_schema_database_is_unknown(self):
        self.tenant()
        self.manager.idle_database = self.root / 'missing.db'
        await self.manager.reap_idle(self.client)
        self.assertFalse(self.manager.idle_database.exists())
        self.assertEqual(self.kills, [])
        self.manager.idle_database = self.dbpath
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute('BEGIN EXCLUSIVE')
            await self.manager.reap_idle(self.client)
        with closing(sqlite3.connect(self.dbpath)) as db, db:
            db.execute('DROP TABLE tasks')
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_lease_arriving_during_status_probe_wins(self):
        t = self.tenant()
        async def history(*args):
            self.manager.acquire(t)
            return [ITEM]
        self.history.side_effect = history
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])
        self.manager.release(t)

    async def test_activity_arriving_during_status_probe_wins(self):
        t = self.tenant()
        async def history(*args):
            await self.manager.resolve({'id': USER}, 'fixture-new-grant')
            return [ITEM]
        self.history.side_effect = history
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_failed_cache_write_preserves_live_workspace(self):
        self.tenant()
        with patch.object(router, 'private_write', side_effect=OSError('fixture')):
            await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_occupied_port_keeps_slot_reserved_and_can_retry(self):
        t = self.tenant()
        with patch.object(router, 'probe_workspace_port', side_effect=OSError('fixture')):
            await self.manager.reap_idle(self.client)
        self.assertIs(self.manager.tenants[USER], t)
        self.assertTrue(t.retiring)
        with self.assertRaises(router.web.HTTPServiceUnavailable):
            await self.manager.resolve({'id': USER}, 'fixture')
        await self.manager.reap_idle(self.client)
        self.assertNotIn(USER, self.manager.tenants)

    async def test_stop_timeout_keeps_slot_without_sigkill(self):
        t = self.tenant()
        t.processes[0].wait = AsyncMock(side_effect=TimeoutError)
        await self.manager.reap_idle(self.client)
        self.assertIn(USER, self.manager.tenants)
        self.assertTrue(all(sig == router.signal.SIGTERM for _, sig in self.kills))

    async def test_dead_host_is_unknown_and_not_silently_replaced(self):
        t = self.tenant()
        t.processes[1].returncode = 1
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])
        with self.assertRaises(router.web.HTTPServiceUnavailable):
            await self.manager.resolve({'id': USER}, 'fixture')

    async def test_concurrent_wake_launches_once_and_reuses_retained_home(self):
        t = self.tenant()
        await self.manager.reap_idle(self.client)
        async def launch(ident, token):
            await asyncio.sleep(.01)
            self.assertTrue((pathlib.Path(t.workspace) / 'retained.png').exists())
            return router.Tenant(ident, t.host_url, t.agent_url, t.actor_path, t.launch_token, t.workspace)
        with patch.object(self.manager, '_launch', AsyncMock(side_effect=launch)) as boot:
            a, b = await asyncio.gather(*[self.manager.resolve({'id': USER}, 'fixture') for _ in range(2)])
        self.assertIs(a, b)
        self.assertEqual(boot.await_count, 1)
        self.assertEqual(self.manager.idle_counters['woken'], 1)
        self.assertFalse((self.manager.root / USER / 'idle-history.json').exists())

    async def test_capacity_pressure_reaps_only_eligible_space(self):
        t = self.tenant()
        self.manager.idle_client = self.client
        async def launch(ident, token):
            directory = self.manager.root / ident
            directory.mkdir()
            return router.Tenant(ident, t.host_url, t.agent_url, directory / 'actor.jwt', 'fixture', str(directory))
        with patch.object(self.manager, '_launch', side_effect=launch):
            result = await self.manager.resolve({'id': OTHER}, 'fixture')
        self.assertEqual(result.user_id, OTHER)
        self.assertEqual(len(self.manager.tenants), 1)

    async def test_full_busy_capacity_refuses_new_space_without_stopping_old(self):
        self.tenant()
        self.manager.idle_client = self.client
        self.running.return_value = 1
        with self.assertRaises(router.web.HTTPServiceUnavailable):
            await self.manager.resolve({'id': OTHER}, 'fixture')
        self.assertEqual(self.kills, [])

    async def test_history_poll_does_not_refresh_idle_clock(self):
        t = self.tenant()
        await self.manager.resolve({'id': USER}, 'fixture', touch=False)
        self.assertEqual(t.last_activity, 0)
        await self.manager.reap_idle(self.client)
        for _ in range(3):
            self.assertEqual(self.manager.cached_history({'id': USER}), [ITEM])
        self.assertEqual(self.manager.tenants, {})

    async def test_new_account_history_is_empty_without_launching(self):
        self.assertEqual(self.manager.cached_history({'id': OTHER}), [])
        self.assertEqual(self.manager.tenants, {})

    async def test_cached_history_ownership_corruption_and_symlink_fail_closed(self):
        t = self.tenant()
        await self.manager.reap_idle(self.client)
        path = self.manager.root / USER / 'idle-history.json'
        for value in ([ITEM], {'user_id': OTHER, 'items': [ITEM]}, {'user_id': USER, 'items': [ITEM | {'running': True}]}):
            path.write_text(json.dumps(value))
            with self.assertRaises(router.web.HTTPServiceUnavailable):
                self.manager.cached_history({'id': USER})
        path.unlink()
        path.symlink_to(pathlib.Path(t.workspace) / 'retained.png')
        with self.assertRaises(router.web.HTTPServiceUnavailable):
            self.manager.cached_history({'id': USER})

    async def test_invalid_and_revoked_legacy_identities_cannot_read_cache(self):
        for value in ('../outside', None, USER.upper()):
            # This numeric UUID has no letters; use an actually noncanonical ID.
            if value == USER:
                continue
            with self.assertRaises(router.web.HTTPForbidden):
                self.manager.cached_history({'id': value})
        with self.assertRaises(router.web.HTTPForbidden):
            self.manager.cached_history({'id': LEGACY, 'role': 'user'})

    async def test_disabled_policy_and_invalid_configuration(self):
        manager = router.TenantManager(self.config | {'idle_seconds': 0})
        with patch.object(manager, '_reap_locked', AsyncMock()) as sweep:
            await manager.reap_idle(self.client)
        sweep.assert_not_awaited()
        for value in (True, -1, 10, 1.5, 86401):
            with self.assertRaises(ValueError):
                router.TenantManager(self.config | {'idle_seconds': value})
        with self.assertRaises(ValueError):
            router.TenantManager(self.config | {'idle_database_path': 'relative.db'})


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    setUp = IdleTests.setUp
    kill = IdleTests.kill
    tenant = IdleTests.tenant

    async def asyncSetUp(self):
        self.download_gate = asyncio.Event()
        async def auth(request):
            if request.headers.get('Authorization') != 'Bearer fixture-auth':
                return web.json_response({'error': {'code': 'TOKEN_INVALID'}}, status=401)
            return web.json_response({'success': True, 'data': {'id': USER, 'role': 'user'}})
        backend = web.Application()
        backend.router.add_get('/api/auth/me', auth)
        self.auth_server = TestServer(backend)
        await self.auth_server.start_server()
        self.addAsyncCleanup(self.auth_server.close)
        async def home(request):
            result = web.Response(text='fixture-host')
            result.set_cookie('fixture', 'only')
            return result
        async def download(request):
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b'begin')
            await self.download_gate.wait()
            await response.write_eof(b'end')
            return response
        async def mux(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            async for message in socket:
                if message.type == router.WSMsgType.TEXT:
                    await socket.send_str(message.data)
            return socket
        host = web.Application()
        host.router.add_get('/', home)
        host.router.add_get('/slow.png', download)
        host.router.add_get('/api/remote.mux', mux)
        self.host_server = TestServer(host)
        await self.host_server.start_server()
        self.addAsyncCleanup(self.host_server.close)
        async def launch(ident, token):
            self.boots += 1
            t = self.tenant(ident)
            t.host_url = t.agent_url = str(self.host_server.make_url('')).rstrip('/')
            return t
        self.boots = 0
        self.enterContext(patch.object(self.manager, '_launch', side_effect=launch))
        identity = self.root / 'identity.key'
        identity.write_text('f' * 64)
        self.config.update(backend_url=str(self.auth_server.make_url('')).rstrip('/'), max_body=1024 * 1024,
            identity_key_path=str(identity), testing=True)
        self.manager.idle_check_seconds = 1
        self.http = TestClient(TestServer(router.create_app(self.config, resolver=self.manager)))
        await self.http.start_server()
        self.addAsyncCleanup(self.http.close)
        self.headers = {'Authorization': 'Bearer fixture-auth'}

    async def test_periodic_sweep_retires_disconnected_workspace(self):
        self.assertEqual((await self.http.get('/', headers=self.headers)).status, 200)
        self.manager.tenants[USER].last_activity = 0
        for _ in range(30):
            if USER not in self.manager.tenants:
                break
            await asyncio.sleep(.05)
        self.assertNotIn(USER, self.manager.tenants)

    async def test_http_lease_spans_entire_streaming_download(self):
        response = await self.http.get('/slow.png', headers=self.headers)
        self.assertEqual(await response.content.readexactly(5), b'begin')
        t = self.manager.tenants[USER]
        t.last_activity = 0
        self.assertEqual(t.leases, 1)
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])
        self.download_gate.set()
        self.assertEqual(await response.read(), b'end')
        await asyncio.sleep(.01)
        self.assertEqual(t.leases, 0)
        self.assertGreater(t.last_activity, 0)
        t.last_activity = 0
        await self.manager.reap_idle(self.client)
        self.assertNotIn(USER, self.manager.tenants)

    async def test_open_websocket_is_pinned_and_close_releases_lease(self):
        socket = await self.http.ws_connect('/api/remote.mux', headers=self.headers)
        t = self.manager.tenants[USER]
        t.last_activity = 0
        self.assertEqual(t.leases, 1)
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])
        await socket.close()
        for _ in range(20):
            if t.leases == 0:
                break
            await asyncio.sleep(.01)
        self.assertEqual(t.leases, 0)
        self.assertGreater(t.last_activity, 0)
        t.last_activity = 0
        await self.manager.reap_idle(self.client)
        self.assertNotIn(USER, self.manager.tenants)

    async def test_sleeping_history_does_not_wake_or_cross_accounts(self):
        self.assertEqual((await self.http.get('/', headers=self.headers)).status, 200)
        self.manager.tenants[USER].last_activity = 0
        await self.manager.reap_idle(self.client)
        headers = self.headers | {'X-Wuyu-Identity-Key': 'f' * 64}
        response = await self.http.get('/__internal/identity/history?work_user_id=' + USER, headers=headers)
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['items'], [ITEM])
        self.assertEqual(self.boots, 1)
        self.assertEqual(self.manager.tenants, {})
        response = await self.http.get('/__internal/identity/history?work_user_id=' + OTHER, headers=headers)
        self.assertEqual(response.status, 403)
        self.assertEqual((await self.http.get('/', headers=self.headers)).status, 200)
        self.assertEqual(self.boots, 2)

    async def test_anonymous_cross_origin_and_rpc_denial_do_not_leak_leases(self):
        self.assertEqual((await self.http.get('/api/session/list')).status, 401)
        self.assertEqual((await self.http.get('/', headers=self.headers | {'Origin': 'https://other.invalid'})).status, 403)
        self.assertEqual(self.boots, 0)
        self.assertEqual((await self.http.post('/api/commands/list', headers=self.headers, json={})).status, 403)
        self.assertEqual(self.manager.tenants[USER].leases, 0)


if __name__ == '__main__':
    unittest.main()
