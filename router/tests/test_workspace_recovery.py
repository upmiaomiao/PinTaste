"""Failed-Agent retirement never restarts a task or guesses a dead Host is idle."""
import asyncio
import json
import pathlib
import unittest
from unittest.mock import AsyncMock, patch
import test_workspace_idle as base_tests
from test_workspace_idle import Process, router, USER, OTHER, LEGACY, ITEM


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    setUp = base_tests.IdleTests.setUp
    kill = base_tests.IdleTests.kill
    tenant = base_tests.IdleTests.tenant

    def failed(self):
        tenant = self.tenant()
        tenant.processes[0].returncode = 1
        return tenant

    async def test_failed_agent_retires_only_after_two_idle_and_native_proofs(self):
        t = self.failed()
        with patch.object(self.manager, 'backend_idle', side_effect=[True, True]) as idle:
            await self.manager.reap_idle(self.client)
        self.assertNotIn(USER, self.manager.tenants)
        self.assertEqual(self.kills, [(102, router.signal.SIGTERM)])
        self.assertEqual(idle.call_count, 2)
        self.assertEqual(self.manager.idle_counters['recovered'], 1)
        self.assertEqual((pathlib.Path(t.workspace) / 'retained.png').read_bytes(), b'unchanged-image')
        self.assertEqual(self.manager.cached_history({'id': USER}), [ITEM])

    async def test_background_sweep_never_launches_or_replays_a_turn(self):
        self.failed()
        with patch.object(self.manager, '_launch', AsyncMock()) as boot:
            await self.manager.reap_idle(self.client)
        boot.assert_not_awaited()

    async def test_failed_agent_keeps_a_full_scan_interval_of_quiet_time(self):
        t = self.failed(); t.last_activity = router.time.monotonic() - 29
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])
        t.last_activity -= 2
        await self.manager.reap_idle(self.client)
        self.assertNotIn(USER, self.manager.tenants)

    async def test_healthy_workspace_still_requires_900_seconds(self):
        t = self.tenant(); t.last_activity = router.time.monotonic() - 31
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])
        self.assertEqual(self.running.await_count, 0)

    async def test_connected_failed_workspace_is_never_stopped(self):
        t = self.failed(); self.manager.acquire(t)
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, []); self.assertEqual(self.running.await_count, 0)
        self.manager.release(t)

    async def test_native_turn_blocks_failed_agent_retirement(self):
        self.failed(); self.running.return_value = 1
        await self.manager.reap_idle(self.client); self.assertEqual(self.kills, [])

    async def test_unknown_and_non_integer_native_counts_are_not_zero(self):
        self.failed()
        for value in [None, False, True, '0', 0.0]:
            self.running.return_value = value
            await self.manager.reap_idle(self.client)
            self.assertEqual(self.kills, [])

    async def test_running_history_blocks_even_after_zero_native_count(self):
        self.failed(); self.history.return_value = [ITEM | {'running': True}]
        await self.manager.reap_idle(self.client); self.assertEqual(self.kills, [])

    async def test_unknown_history_never_writes_cache_or_stops(self):
        self.failed(); self.history.side_effect = router.web.HTTPServiceUnavailable()
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, []); self.assertFalse((self.manager.root / USER / 'idle-history.json').exists())

    async def test_persistent_busy_and_unknown_block_before_native_probe(self):
        self.failed()
        for idle in [False, None]:
            with patch.object(self.manager, 'backend_idle', return_value=idle):
                await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, []); self.assertEqual(self.running.await_count, 0)

    async def test_persistent_job_arriving_during_probe_wins(self):
        self.failed()
        with patch.object(self.manager, 'backend_idle', side_effect=[True, False]):
            await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_persistent_unknown_on_second_check_wins(self):
        self.failed()
        with patch.object(self.manager, 'backend_idle', side_effect=[True, None]):
            await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_lease_arriving_during_history_probe_wins(self):
        t = self.failed()
        async def history(*args): self.manager.acquire(t); return [ITEM]
        self.history.side_effect = history
        await self.manager.reap_idle(self.client); self.assertEqual(self.kills, [])
        self.manager.release(t)

    async def test_recent_activity_arriving_during_probe_wins(self):
        t = self.failed()
        async def history(*args): t.last_activity = router.time.monotonic(); return [ITEM]
        self.history.side_effect = history
        await self.manager.reap_idle(self.client); self.assertEqual(self.kills, [])

    async def test_host_dying_during_probe_stays_unknown(self):
        t = self.failed()
        async def history(*args): t.processes[1].returncode = 1; return [ITEM]
        self.history.side_effect = history
        await self.manager.reap_idle(self.client); self.assertEqual(self.kills, [])

    async def test_mapping_change_during_probe_cannot_stop_another_workspace(self):
        t = self.failed()
        replacement = router.Tenant(USER, t.host_url, t.agent_url, t.actor_path, t.launch_token, t.workspace)
        async def history(*args): self.manager.tenants[USER] = replacement; return [ITEM]
        self.history.side_effect = history
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, []); self.assertIs(self.manager.tenants[USER], replacement)

    async def test_dead_host_requires_operator(self):
        t = self.tenant(); t.processes[1].returncode = 1
        await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, []); self.assertEqual(self.running.await_count, 0)

    async def test_both_dead_require_operator(self):
        t = self.failed(); t.processes[1].returncode = 1
        await self.manager.reap_idle(self.client); self.assertEqual(self.kills, [])

    async def test_unknown_process_layout_requires_operator(self):
        t = self.failed(); t.processes = (*t.processes, Process(103))
        await self.manager.reap_idle(self.client); self.assertEqual(self.kills, [])

    async def test_legacy_shared_workspace_never_auto_recovers(self):
        t = self.tenant(LEGACY, legacy=True); t.processes[0].returncode = 1
        await self.manager.reap_idle(self.client); self.assertEqual(self.kills, [])

    async def test_cache_write_failure_does_not_stop_processes(self):
        self.failed()
        with patch.object(router, 'private_write', side_effect=OSError('synthetic')):
            await self.manager.reap_idle(self.client)
        self.assertEqual(self.kills, [])

    async def test_port_conflict_retains_slot_and_retry_counts_recovery_once(self):
        t = self.failed()
        with patch.object(router, 'probe_workspace_port', side_effect=OSError('synthetic')):
            await self.manager.reap_idle(self.client)
        self.assertIs(self.manager.tenants[USER], t); self.assertTrue(t.retiring)
        self.assertEqual(self.manager.idle_counters['recovered'], 0)
        await self.manager.reap_idle(self.client)
        self.assertNotIn(USER, self.manager.tenants); self.assertEqual(self.manager.idle_counters['recovered'], 1)
        await self.manager.reap_idle(self.client); self.assertEqual(self.manager.idle_counters['recovered'], 1)

    async def test_partial_stop_never_sigkills_and_finishes_without_900_second_wait(self):
        t = self.failed(); t.last_activity = router.time.monotonic() - 31
        t.processes[1].wait = AsyncMock(side_effect=[TimeoutError, 0])
        await self.manager.reap_idle(self.client)
        self.assertTrue(t.retiring); self.assertIs(self.manager.tenants[USER], t)
        await self.manager.reap_idle(self.client)
        self.assertNotIn(USER, self.manager.tenants)
        self.assertTrue(all(sig == router.signal.SIGTERM for _, sig in self.kills))
        self.assertEqual(self.manager.idle_counters['recovered'], 1)

    async def test_failed_wake_preserves_history_and_original_files(self):
        t = self.failed(); await self.manager.reap_idle(self.client)
        path = self.manager.root / USER / 'idle-history.json'; before = path.read_bytes()
        with patch.object(self.manager, '_launch', AsyncMock(side_effect=router.web.HTTPServiceUnavailable())):
            with self.assertRaises(router.web.HTTPServiceUnavailable): await self.manager.resolve({'id': USER}, 'synthetic')
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((pathlib.Path(t.workspace) / 'retained.png').read_bytes(), b'unchanged-image')

    async def test_normal_reopen_after_recovery_keeps_home_and_launches_once(self):
        t = self.failed(); await self.manager.reap_idle(self.client)
        async def boot(ident, token):
            await asyncio.sleep(.01)
            return router.Tenant(ident, t.host_url, t.agent_url, t.actor_path, t.launch_token, t.workspace)
        with patch.object(self.manager, '_launch', AsyncMock(side_effect=boot)) as launch:
            a, b = await asyncio.gather(self.manager.resolve({'id': USER}, 'synthetic'), self.manager.resolve({'id': USER}, 'synthetic'))
        self.assertIs(a, b); self.assertEqual(launch.await_count, 1)
        self.assertFalse((self.manager.root / USER / 'idle-history.json').exists())

    async def test_private_activity_reports_failed_agent_without_claiming_idle(self):
        self.failed(); data = await self.manager.activity(self.client, 'fixture.invalid')
        self.assertTrue(data['busy']); item = data['tenants'][0]
        self.assertFalse(item['available']); self.assertIsNone(item['running'])
        self.assertEqual(item['failure'], 'agent_exited'); self.assertEqual(item['recovery'], 'await_disconnected_verified_idle')

    async def test_private_activity_reports_dead_host_requires_operator(self):
        t = self.tenant(); t.processes[1].returncode = 1
        data = await self.manager.activity(self.client, 'fixture.invalid')
        self.assertEqual(data['tenants'][0]['recovery'], 'operator_required'); self.assertTrue(data['busy'])

    async def test_another_account_cannot_wake_or_read_retired_owner_files(self):
        t = self.failed(); await self.manager.reap_idle(self.client)
        self.assertEqual(self.manager.cached_history({'id': OTHER}), [])
        self.assertEqual(self.manager.cached_history({'id': USER}), [ITEM])


    async def test_successful_wake_remains_owned_when_cache_cleanup_fails(self):
        t = self.failed(); await self.manager.reap_idle(self.client)
        cache = self.manager.root / USER / 'idle-history.json'
        replacement = router.Tenant(USER, t.host_url, t.agent_url, t.actor_path, t.launch_token, t.workspace)
        original_unlink = pathlib.Path.unlink
        def unlink(path, *args, **kwargs):
            if path == cache: raise OSError('synthetic cleanup failure')
            return original_unlink(path, *args, **kwargs)
        with patch.object(self.manager, '_launch', AsyncMock(return_value=replacement)) as boot, patch.object(pathlib.Path, 'unlink', unlink):
            actual = await self.manager.resolve({'id': USER}, 'synthetic')
            again = await self.manager.resolve({'id': USER}, 'synthetic')
        self.assertIs(actual, replacement); self.assertIs(again, replacement)
        self.assertIs(self.manager.tenants[USER], replacement); self.assertEqual(boot.await_count, 1)
        self.assertEqual(self.manager.idle_counters['cache_cleanup_failed'], 1)
        self.assertTrue(cache.exists()); self.assertIsNone(self.manager.cached_history({'id': USER}))

    async def test_wake_tolerates_cache_disappearing_after_successful_launch(self):
        t = self.failed(); await self.manager.reap_idle(self.client)
        cache = self.manager.root / USER / 'idle-history.json'
        replacement = router.Tenant(USER, t.host_url, t.agent_url, t.actor_path, t.launch_token, t.workspace)
        async def boot(*unused): cache.unlink(); return replacement
        with patch.object(self.manager, '_launch', AsyncMock(side_effect=boot)):
            actual = await self.manager.resolve({'id': USER}, 'synthetic')
        self.assertIs(actual, replacement); self.assertIs(self.manager.tenants[USER], replacement)
        self.assertEqual(self.manager.idle_counters['cache_cleanup_failed'], 0)


if __name__ == '__main__': unittest.main()
