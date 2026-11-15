"""New workspace admission uses only safe cgroup accounting; no real child starts."""
import pathlib
import unittest
from unittest.mock import AsyncMock, patch
import test_workspace_idle as base_tests
from test_workspace_idle import Process, router, USER, OTHER


class MemoryAdmissionTests(unittest.IsolatedAsyncioTestCase):
    kill = base_tests.IdleTests.kill
    tenant = base_tests.IdleTests.tenant

    def setUp(self):
        # Existing Linux fixtures patch killpg; provide only a mock attribute on Windows.
        if not hasattr(router.os, 'killpg'):
            self.enterContext(patch.object(router.os, 'killpg', create=True))
        base_tests.IdleTests.setUp(self)

    def enable(self):
        self.manager.workspace_memory_admission_bytes = 8_000_000_000
        self.manager.workspace_memory_reserve_bytes = 400_000_000

    def replacement(self, ident=USER):
        directory = self.manager.root / ident
        directory.mkdir(parents=True, exist_ok=True)
        return router.Tenant(ident, 'http://127.0.0.1:33100', 'http://127.0.0.1:33101',
            directory / 'actor.jwt', 'synthetic-grant', str(directory / 'workspace'),
            processes=(Process(101), Process(102)))

    async def test_default_disabled_does_not_read_cgroup_or_block_launch(self):
        expected = self.replacement()
        with patch.object(router, 'own_cgroup_memory_current', side_effect=AssertionError('must not read')):
            with patch.object(self.manager, '_launch', AsyncMock(return_value=expected)) as boot:
                actual = await self.manager.resolve({'id': USER}, 'synthetic')
        self.assertIs(actual, expected); boot.assert_awaited_once()

    async def test_exact_admission_boundary_including_reserve_is_allowed(self):
        self.enable(); expected = self.replacement()
        with patch.object(router, 'own_cgroup_memory_current', return_value=7_600_000_000):
            with patch.object(self.manager, '_launch', AsyncMock(return_value=expected)) as boot:
                actual = await self.manager.resolve({'id': USER}, 'synthetic')
        self.assertIs(actual, expected); boot.assert_awaited_once()
        self.assertEqual(self.manager.workspace_memory_last_current_bytes, 7_600_000_000)

    async def test_above_boundary_refuses_new_account_before_launch_or_files(self):
        self.enable()
        with patch.object(router, 'own_cgroup_memory_current', return_value=7_600_000_001):
            with patch.object(self.manager, '_launch', AsyncMock()) as boot:
                with self.assertRaises(router.web.HTTPServiceUnavailable) as caught:
                    await self.manager.resolve({'id': USER}, 'synthetic')
        boot.assert_not_awaited(); self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.headers['Retry-After'], '30')
        self.assertEqual(self.manager.workspace_memory_counters['blocked_capacity'], 1)
        self.assertNotIn(USER, self.manager.tenants)
        self.assertFalse((self.manager.root / USER).exists()); self.assertEqual(self.kills, [])

    async def test_unknown_cgroup_fails_closed_without_leaking_exception(self):
        self.enable()
        for error in [OSError('synthetic-secret-path'), ValueError('synthetic-secret-value'), UnicodeError('synthetic-secret-text')]:
            with patch.object(router, 'own_cgroup_memory_current', side_effect=error):
                with patch.object(self.manager, '_launch', AsyncMock()) as boot:
                    with self.assertRaises(router.web.HTTPServiceUnavailable) as caught:
                        await self.manager.resolve({'id': USER}, 'synthetic')
            boot.assert_not_awaited(); self.assertNotIn('synthetic-secret', caught.exception.text)
            self.assertEqual(caught.exception.headers['Retry-After'], '30')
        self.assertEqual(self.manager.workspace_memory_counters['blocked_unknown'], 3)

    async def test_noninteger_and_negative_current_are_unknown(self):
        self.enable()
        for value in [None, False, '0', 0.0, -1]:
            with patch.object(router, 'own_cgroup_memory_current', return_value=value):
                with self.assertRaises(router.web.HTTPServiceUnavailable):
                    await self.manager.resolve({'id': USER}, 'synthetic')
        self.assertEqual(self.manager.workspace_memory_counters['blocked_unknown'], 5)

    async def test_existing_workspace_bypasses_guard_even_when_accounting_is_unknown(self):
        self.enable(); expected = self.tenant()
        with patch.object(router, 'own_cgroup_memory_current', side_effect=AssertionError('existing work stays available')):
            actual = await self.manager.resolve({'id': USER}, 'synthetic')
        self.assertIs(actual, expected); self.assertEqual(self.kills, [])
        self.assertEqual(self.manager.workspace_memory_counters, {'blocked_capacity': 0, 'blocked_unknown': 0})

    async def test_refused_wake_preserves_cache_and_does_not_mark_woken(self):
        self.enable(); directory = self.manager.root / USER
        directory.mkdir(parents=True); cache = directory / 'idle-history.json'
        cache.write_bytes(b'synthetic-private-cache')
        with patch.object(router, 'own_cgroup_memory_current', return_value=8_000_000_000):
            with patch.object(self.manager, '_launch', AsyncMock()) as boot:
                with self.assertRaises(router.web.HTTPServiceUnavailable):
                    await self.manager.resolve({'id': USER}, 'synthetic')
        boot.assert_not_awaited(); self.assertEqual(cache.read_bytes(), b'synthetic-private-cache')
        self.assertEqual(self.manager.idle_counters['woken'], 0); self.assertEqual(self.kills, [])

    async def test_each_serial_launch_rechecks_current_and_leaves_first_owned(self):
        self.enable(); self.config['max_tenants'] = 2; expected = self.replacement()
        with patch.object(router, 'own_cgroup_memory_current', side_effect=[1_000_000_000, 7_900_000_000]) as read:
            with patch.object(self.manager, '_launch', AsyncMock(return_value=expected)) as boot:
                actual = await self.manager.resolve({'id': USER}, 'synthetic')
                with self.assertRaises(router.web.HTTPServiceUnavailable):
                    await self.manager.resolve({'id': OTHER}, 'synthetic')
        self.assertIs(actual, expected); self.assertIs(self.manager.tenants[USER], expected)
        self.assertNotIn(OTHER, self.manager.tenants); self.assertEqual(read.call_count, 2); boot.assert_awaited_once()

    async def test_activity_exposes_safe_policy_and_counters_without_new_reads(self):
        self.enable()
        self.manager.workspace_memory_counters['blocked_capacity'] = 2
        with patch.object(router, 'own_cgroup_memory_current', side_effect=AssertionError('activity does not probe launches')):
            value = await self.manager.activity(self.client, 'fixture.invalid')
        self.assertFalse(value['busy']); policy = value['memory_admission']
        self.assertEqual(policy, {'enabled': True, 'admission_bytes': 8_000_000_000,
            'reserve_bytes': 400_000_000, 'last_checked_current_bytes': None,
            'counters': {'blocked_capacity': 2, 'blocked_unknown': 0}, 'scope': 'new_workspace_launches'})
        self.assertEqual(value['idle']['seconds'], 900); self.assertEqual(value['idle']['check_seconds'], 30)

    def test_invalid_policy_does_not_silently_disable_guard(self):
        cases=[{'workspace_memory_admission_bytes': True},
            {'workspace_memory_admission_bytes': -1},
            {'workspace_memory_admission_bytes': '8000000000'},
            {'workspace_memory_admission_bytes': 1.0},
            {'workspace_memory_admission_bytes': 2**63},
            {'workspace_memory_reserve_bytes': 1},
            {'workspace_memory_admission_bytes': 100, 'workspace_memory_reserve_bytes': True},
            {'workspace_memory_admission_bytes': 100, 'workspace_memory_reserve_bytes': 101}]
        for policy in cases:
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError):router.TenantManager(self.config | policy)


class OwnCgroupReadTests(unittest.TestCase):
    def test_uses_only_own_v2_path_and_returns_charge(self):
        def read(path, *args, **kwargs):
            if str(path).replace('\\', '/')=='/proc/self/cgroup':return '0::/system.slice/self-created-fixture.service\n'
            self.assertEqual(str(path).replace('\\', '/'), '/sys/fs/cgroup/system.slice/self-created-fixture.service/memory.current')
            return '123456\n'
        with patch.object(pathlib.Path, 'read_text', read):self.assertEqual(router.own_cgroup_memory_current(), 123456)

    def test_v1_missing_duplicate_or_unsafe_group_is_unknown(self):
        for raw in ['3:memory:/fixture\n', '0::/one\n0::/two\n', '0::relative\n', '0::/one/../outside\n', '0::/one\\outside\n']:
            with self.subTest(raw=raw):
                with patch.object(pathlib.Path, 'read_text', return_value=raw) as read:
                    with self.assertRaises(ValueError):router.own_cgroup_memory_current()
                self.assertEqual(read.call_count, 1)

    def test_invalid_memory_value_is_unknown(self):
        for raw in ['-1', 'max', '', '3.14', str(2**63), 'x'*21]:
            with self.subTest(raw=raw):
                with patch.object(pathlib.Path, 'read_text', side_effect=['0::/fixture\n', raw]):
                    with self.assertRaises(ValueError):router.own_cgroup_memory_current()


if __name__ == '__main__':unittest.main()
