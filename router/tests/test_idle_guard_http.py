"""Actual candidate TenantManager on synthetic files and mocked HTTP/Host IO."""
import ast
import asyncio
from contextlib import closing
import http.client
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / 'wuyu_tenant_router.py'

tree = ast.parse(CANDIDATE.read_bytes())
manager_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'TenantManager')
namespace = dict(Path=Path, asyncio=asyncio, closing=closing, http=http, sqlite3=sqlite3, time=time,
                 json=json, os=os, math=math, urlsplit=urlsplit,
                 web=types.SimpleNamespace(HTTPException=RuntimeError), ClientError=RuntimeError,
                 private_write=lambda *args: None)
exec(compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), manager_node],
                        type_ignores=[])), '<candidate-TenantManager>', 'exec'), namespace)
Manager = namespace['TenantManager']


class RouterIdleHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='router-http94-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'seed').mkdir()
        self.database = self.root / 'sealed.sqlite3'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript('CREATE TABLE tasks(status TEXT);CREATE TABLE wuyu_workflow_jobs(status TEXT);')
        self.config = {'tenant_root': str(self.root / 'tenants'), 'seed_dir': str(self.root / 'seed'),
                       'max_tenants': 20, 'idle_seconds': 900, 'idle_check_seconds': 30,
                       'idle_database_path': str(self.database), 'public_origin': 'https://fixture.invalid'}

    def manager(self, **delta):
        return Manager({**self.config, 'idle_guard_backend': 'http',
                        'idle_probe_url': 'http://127.0.0.1:18131/health/idle', **delta})

    def connection(self, body=b'{"schema_version":1,"idle":true}', *, status=200, content_type='application/json', length=None):
        response = MagicMock()
        response.status = status
        response.getheader.side_effect = lambda name, default=None: content_type if name == 'Content-Type' else length
        response.read.return_value = body
        connection = MagicMock()
        connection.getresponse.return_value = response
        return connection, response

    def test_legacy_selector_defaults_to_SQLite_and_retains_terminal_semantics(self):
        manager = Manager(self.config)
        self.assertEqual(manager.idle_guard_backend, 'sqlite')
        self.assertTrue(manager.backend_idle())
        with closing(sqlite3.connect(self.database)) as db, db: db.execute('INSERT INTO tasks VALUES(NULL)')
        self.assertFalse(manager.backend_idle())

    def test_http_selector_needs_no_SQLite_path_and_preserves_900_30_20(self):
        config = {k: v for k, v in self.config.items() if k != 'idle_database_path'}
        manager = Manager({**config, 'idle_guard_backend': 'http', 'idle_probe_url': 'http://127.0.0.1:18131/health/idle'})
        self.assertIsNone(manager.idle_database)
        self.assertEqual((manager.idle_seconds, manager.idle_check_seconds, manager.config['max_tenants']), (900, 30, 20))
        # Current staging has 3 slots; the HTTP guard must preserve that exact limit too.
        manager = self.manager(max_tenants=3)
        self.assertEqual((manager.idle_seconds, manager.idle_check_seconds, manager.config['max_tenants']), (900, 30, 3))

    def test_explicit_http_missing_or_changed_URL_never_falls_back(self):
        invalid = [None, '', 'http://127.0.0.1:18131/health/idle/', 'http://localhost:18131/health/idle',
                   'http://127.0.0.1:8131/health/idle', 'https://127.0.0.1:18131/health/idle',
                   'http://127.0.0.1:18131/health/idle?x=1', 'http://user@127.0.0.1:18131/health/idle']
        for url in invalid:
            with self.assertRaises(ValueError): self.manager(idle_probe_url=url)

    def test_conflicting_or_unknown_selector_rejected(self):
        for selector in ['sqlite', 'unknown', True]:
            with self.assertRaises(ValueError): self.manager(idle_guard_backend=selector)

    def test_idle_and_busy_use_only_fixed_http_no_credentials_no_SQLite(self):
        for value in [True, False]:
            connection, response = self.connection(json.dumps({'schema_version': 1, 'idle': value}).encode())
            with patch.object(http.client, 'HTTPConnection', return_value=connection) as factory, \
                    patch.object(sqlite3, 'connect', side_effect=AssertionError('SQLite must not be read')):
                self.assertIs(self.manager().backend_idle(), value)
            factory.assert_called_once_with('127.0.0.1', 18131, timeout=2)
            connection.request.assert_called_once_with('GET', '/health/idle', headers={'Accept': 'application/json'})
            connection.close.assert_called_once()

    def test_non200_redirects_and_503_are_unknown_no_follow(self):
        for status in [201, 301, 302, 401, 403, 404, 500, 503]:
            connection, response = self.connection(status=status)
            with patch.object(http.client, 'HTTPConnection', return_value=connection):
                self.assertIsNone(self.manager().backend_idle())
            self.assertEqual(connection.request.call_count, 1)
            response.read.assert_not_called()

    def test_transport_failures_are_unknown(self):
        for error in [TimeoutError(), ConnectionRefusedError(), http.client.HTTPException()]:
            connection, _ = self.connection(); connection.getresponse.side_effect = error
            with patch.object(http.client, 'HTTPConnection', return_value=connection):
                self.assertIsNone(self.manager().backend_idle())
            connection.close.assert_called_once()

    def test_exact_schema_version_and_boolean_required(self):
        invalid = [{}, [], {'schema_version': True, 'idle': True}, {'schema_version': 2, 'idle': True},
                   {'schema_version': 1, 'idle': 1}, {'schema_version': 1, 'idle': 'true'},
                   {'schema_version': 1, 'idle': True, 'other': False}]
        for value in invalid:
            connection, _ = self.connection(json.dumps(value).encode())
            with patch.object(http.client, 'HTTPConnection', return_value=connection):
                self.assertIsNone(self.manager().backend_idle())

    def test_duplicate_fields_invalid_UTF8_and_broken_JSON_are_unknown(self):
        for body in [b'{"schema_version":1,"idle":false,"idle":true}', b'\xff', b'{', b'']:
            connection, _ = self.connection(body)
            with patch.object(http.client, 'HTTPConnection', return_value=connection):
                self.assertIsNone(self.manager().backend_idle())

    def test_content_type_and_size_limits(self):
        variants = [(b'{}', 'text/html', None), (b'{}', 'application/json', '1025'),
                    (b'{}', 'application/json', 'invalid'), (b'x' * 1025, 'application/json', None)]
        for body, content_type, length in variants:
            connection, _ = self.connection(body, content_type=content_type, length=length)
            with patch.object(http.client, 'HTTPConnection', return_value=connection):
                self.assertIsNone(self.manager().backend_idle())

    async def test_busy_unknown_and_second_probe_busy_block_reap(self):
        for outcomes in [[False], [None], [True, False], [True, None]]:
            manager = self.manager()
            tenant = types.SimpleNamespace(legacy=False, leases=0, last_activity=time.monotonic() - 1000,
                                           retiring=False, processes=[], user_id='synthetic')
            manager.tenants = {'synthetic': tenant}
            manager.backend_idle = MagicMock(side_effect=outcomes)
            manager._finish_idle_stop = AsyncMock()
            with patch.dict(namespace, host_running=AsyncMock(return_value=0), host_history=AsyncMock(return_value=[])):
                await manager._reap_locked(None)
            manager._finish_idle_stop.assert_not_called()
            self.assertFalse(tenant.retiring)

    async def test_authoritative_idle_can_reap_only_idle_unleased_Host(self):
        manager = self.manager()
        tenant = types.SimpleNamespace(legacy=False, leases=0, last_activity=time.monotonic() - 1000,
                                       retiring=False, processes=[], user_id='synthetic')
        manager.tenants = {'synthetic': tenant}; manager.backend_idle = MagicMock(return_value=True)
        manager._finish_idle_stop = AsyncMock()
        with patch.dict(namespace, host_running=AsyncMock(return_value=0), host_history=AsyncMock(return_value=[])):
            await manager._reap_locked(None)
        manager._finish_idle_stop.assert_awaited_once_with(tenant)
        self.assertEqual(manager.backend_idle.call_count, 2)

    async def test_active_lease_or_native_running_never_stops(self):
        for leased, native_running in [(1, 0), (0, 1)]:
            manager = self.manager()
            tenant = types.SimpleNamespace(legacy=False, leases=leased, last_activity=time.monotonic() - 1000,
                                           retiring=False, processes=[], user_id='synthetic')
            manager.tenants = {'synthetic': tenant}; manager.backend_idle = MagicMock(return_value=True)
            manager._finish_idle_stop = AsyncMock()
            with patch.dict(namespace, host_running=AsyncMock(return_value=native_running), host_history=AsyncMock(return_value=[])):
                await manager._reap_locked(None)
            manager._finish_idle_stop.assert_not_called()



if __name__ == '__main__':
    unittest.main()
