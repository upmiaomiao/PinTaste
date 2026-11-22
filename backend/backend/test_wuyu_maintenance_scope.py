"""Pure operational-scope tests; no app, DB connection, providers or live files."""
import ast
from contextlib import nullcontext
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import types
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

R = Path(__file__).resolve().parent
BACKEND = Path(os.environ['WUYU_TEST_BACKEND_ROOT']) if os.environ.get('WUYU_TEST_BACKEND_ROOT') else (R if (R / 'workflow_execution.py').is_file() else R / 'candidate/backend/backend')
mod = ModuleType('actual_scope_execution')
mod.__file__ = str(BACKEND / 'workflow_execution.py')
sys.modules[mod.__name__] = mod
exec(compile(Path(mod.__file__).read_bytes(), mod.__file__, 'exec'), mod.__dict__)
ONE = '10000000-0000-0000-0000-000000000001'
TWO = '20000000-0000-0000-0000-000000000002'
THREE = '30000000-0000-0000-0000-000000000003'


class TestOS:
    """Windows tests emulate only POSIX file metadata, never bypass production checks."""
    def __init__(self):
        self.overrides, self.opens = {}, 0

    def __getattr__(self, name):
        return getattr(os, name)

    def metadata(self, value):
        fields = {key: getattr(value, key) for key in
                  ('st_mode', 'st_uid', 'st_gid', 'st_nlink', 'st_size', 'st_dev', 'st_ino', 'st_mtime_ns')}
        if os.name != 'posix':
            fields.update(st_uid=0, st_gid=0, st_mode=stat.S_IFREG | 0o600)
        fields.update(self.overrides)
        return SimpleNamespace(**fields)

    def lstat(self, path):
        return self.metadata(os.lstat(path))

    def fstat(self, descriptor):
        return self.metadata(os.fstat(descriptor))

    def open(self, *args):
        self.opens += 1
        return os.open(*args)


class ScopeFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wuyu-scope-fixture-')
        self.directory = Path(self.temp.name)
        self.path = self.directory / mod.ADMISSION_SCOPE_FILENAME
        self.os = TestOS()
        self.patch = patch.object(mod, 'os', self.os)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.temp.cleanup)
        self.payload = dict(schema_version=1, allowed_job_ids=[ONE], issued_at=1000,
                            valid_until=1100, authorization_reference_sha256='a' * 64)

    def write(self, payload=None):
        self.path.write_text(json.dumps(self.payload if payload is None else payload), encoding='utf-8')
        self.path.chmod(0o600)

    def read(self, now=1050):
        return mod.load_worker_admission_scope(self.directory, now)

    def test_absent_file_preserves_normal_scheduling(self):
        scope = self.read()
        self.assertEqual(scope.status, 'unrestricted')
        self.assertTrue(scope.allows(ONE))
        self.assertTrue(scope.allows(TWO))

    def test_valid_scope_only_allows_listed_job(self):
        self.write()
        scope = self.read()
        self.assertEqual(scope.status, 'restricted')
        self.assertEqual(scope.allowed_job_ids, frozenset({ONE}))
        self.assertTrue(scope.allows(ONE))
        self.assertFalse(scope.allows(TWO))

    def test_empty_allow_list_is_valid_maintenance(self):
        self.payload['allowed_job_ids'] = []
        self.write()
        self.assertEqual(self.read().status, 'restricted')
        self.assertFalse(self.read().allows(ONE))

    def test_expiry_boundary_and_future_or_overlong_authority_fail_closed(self):
        for fields in ({'valid_until': 1050}, {'issued_at': 1051},
                       {'valid_until': 1000}, {'valid_until': 1000 + 86401},
                       {'issued_at': True}, {'valid_until': float('inf')},
                       {'issued_at': -1}):
            with self.subTest(fields=fields):
                payload = dict(self.payload, **fields)
                self.write(payload)
                self.assertEqual(self.read().status, 'hold')
                self.assertFalse(self.read().allows(ONE))

    def test_corruption_schema_and_invalid_ids_fail_closed(self):
        cases = [[], dict(self.payload, schema_version=True), dict(self.payload, schema_version=2),
                 dict(self.payload, allowed_job_ids='all'), dict(self.payload, allowed_job_ids=['*']),
                 dict(self.payload, allowed_job_ids=[ONE, ONE]),
                 dict(self.payload, authorization_reference_sha256='not-a-hash'),
                 dict(self.payload, extra='unexpected')]
        missing = dict(self.payload)
        missing.pop('issued_at')
        cases.append(missing)
        for payload in cases:
            with self.subTest(payload=payload):
                self.write(payload)
                self.assertEqual(self.read().status, 'hold')
        for text in ('{broken', '{"schema_version":1,"schema_version":1}', ' ' * 65537):
            self.path.write_text(text)
            self.assertEqual(self.read().status, 'hold')

    def test_unsafe_owner_mode_symlink_and_hardlink_metadata_fail_closed(self):
        self.write()
        for metadata in ({'st_uid': 1}, {'st_mode': stat.S_IFREG | 0o644},
                         {'st_mode': stat.S_IFLNK | 0o600}, {'st_nlink': 2}):
            with self.subTest(metadata=metadata):
                self.os.overrides = metadata
                self.assertEqual(self.read().status, 'hold')
                self.assertFalse(self.read().allows(ONE))

    def test_only_enoent_means_absence(self):
        for error in (PermissionError(errno.EACCES, 'fixture'), OSError(errno.EIO, 'fixture')):
            with self.subTest(errno=error.errno), patch.object(self.os, 'lstat', side_effect=error):
                self.assertEqual(self.read().status, 'hold')

    def test_public_projection_does_not_expose_ids_or_authorization(self):
        self.write()
        before = self.path.read_bytes()
        scope = self.read()
        public = scope.public_snapshot()
        self.assertEqual(public, {'status': 'restricted', 'observed_at': 1050})
        self.assertNotIn(ONE, json.dumps(public))
        self.assertNotIn('a' * 64, json.dumps(public))
        self.assertEqual(before, self.path.read_bytes())

    def test_deletion_or_change_is_seen_by_next_read_without_caching(self):
        self.write()
        self.assertFalse(self.read().allows(TWO))
        self.payload['allowed_job_ids'] = [TWO]
        self.write()
        self.assertTrue(self.read().allows(TWO))
        self.assertFalse(self.read().allows(ONE))
        self.path.unlink()
        self.assertEqual(self.read().status, 'unrestricted')

    def test_capacity_projection_keeps_terminal_fields_and_only_observes_font_cache(self):
        self.payload.update(issued_at=time.time() - 1, valid_until=time.time() + 60)
        self.write()
        worker_tree = ast.parse((BACKEND / 'workflow_worker.py').read_text())
        functions = [n for n in worker_tree.body if isinstance(n, ast.FunctionDef)
                     and n.name in ('write_capacity_snapshot', 'terminal_count', 'terminal_attempt_count')]
        font_source = BACKEND / 'wuyu_font_cache.py'
        font_module = ModuleType('wuyu_font_cache')
        exec(compile(font_source.read_bytes(), str(font_source), 'exec'), font_module.__dict__)
        font_before = font_module.load_font.cache_info()
        resource_module = ModuleType('services.wuyu_resource_limits')
        resource_module.memory_admission = lambda config: {'allowed': True, 'reason': 'available'}
        resource_module.resources = lambda app: SimpleNamespace(budget=SimpleNamespace(capacity=1024, used=0))
        services = ModuleType('services')
        services.__path__ = []
        namespace = dict(Path=Path, os=os, json=json, time=time, tempfile=tempfile,
                         public_worker_admission_scope=mod.public_worker_admission_scope)
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<actual capacity writer>', 'exec'), namespace)
        service = SimpleNamespace(app=SimpleNamespace(config={}), executor=SimpleNamespace(_max_workers=20),
                                  terminal_status=lambda: {'pending_terminal': 2, 'active_terminal_attempts': 1})
        private_before = self.path.read_bytes()
        with patch.dict(sys.modules, {'services': services, 'services.wuyu_resource_limits': resource_module,
                                    'wuyu_font_cache': font_module}):
            self.assertTrue(namespace['write_capacity_snapshot'](service, self.directory))
        value = json.loads((self.directory / 'workflow-capacity.json').read_text())
        self.assertEqual(value['pending_terminal'], 2)
        self.assertEqual(value['active_terminal_attempts'], 1)
        self.assertEqual(value['admission_scope']['status'], 'restricted')
        self.assertEqual(value['font_cache'], {'faces': 0, 'limit': 16, 'hits': 0, 'misses': 0})
        self.assertEqual(font_before, font_module.load_font.cache_info())
        self.assertEqual(private_before, self.path.read_bytes())
        self.assertNotIn(ONE, json.dumps(value))
        self.assertNotIn('a' * 64, json.dumps(value))


class Field:
    def in_(self, values):
        return frozenset(values)


class Query:
    def __init__(self, jobs):
        self.jobs = jobs

    def filter_by(self, **values):
        return Query([j for j in self.jobs if all(getattr(j, k) == v for k, v in values.items())])

    def filter(self, ids):
        return Query([j for j in self.jobs if j.id in ids])

    def order_by(self, *unused):
        return Query(sorted(self.jobs, key=lambda j: j.created_at))

    def limit(self, value):
        return Query(self.jobs[:value])

    def all(self):
        return list(self.jobs)

    def count(self):
        return len(self.jobs)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.jobs = [SimpleNamespace(id=ident, status='queued', actor_user_id='actor',
                     project_id='project-' + ident, created_at=i) for i, ident in enumerate((ONE, TWO, THREE))]
        self.scope = mod.WorkerAdmissionScope('restricted', 1050, frozenset({ONE}), 1000, 1100, 'a' * 64)
        self.retries = []
        terminal = SimpleNamespace(occupied=lambda: {}, retry_due=lambda: self.retries.append('retried'))
        self.service = SimpleNamespace(execution_mode='worker', terminal_owner=terminal,
            app=SimpleNamespace(config={'WUYU_WORKFLOW_WORKERS': 20, 'WUYU_WORKFLOW_PER_ACTOR': 1}),
            future_lock=nullcontext(), futures={}, executor=SimpleNamespace(_max_workers=20))
        session = SimpleNamespace(expire_all=lambda: None, get=lambda kind, ident: next(j for j in self.jobs if j.id == ident))
        self.env = dict(WuyuWorkflowJob=SimpleNamespace(query=Query(self.jobs), id=Field(), created_at=object()),
            json=json,
            load_worker_admission_scope=lambda: self.scope,
            worker_admission_block_reason=lambda ident: None if self.scope.allows(ident) else 'maintenance_hold',
            worker_queue_block_reason=lambda: 'memory_wait', memory_admission=lambda config: {'allowed': True, 'reason': 'available'},
            db=SimpleNamespace(session=session))
        tree = ast.parse((BACKEND / 'services/wuyu_workflow_lifecycle.py').read_text())
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     and n.name in ('_fair_queue', '_claim', 'queue_snapshot', '_schedule')]
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<actual scope lifecycle>', 'exec'), self.env)
        self.service._fair_queue = lambda limit=100: self.env['_fair_queue'](self.service, limit)

    def test_fair_queue_keeps_non_scope_jobs_queued_and_unchanged(self):
        before = [dict(vars(j)) for j in self.jobs]
        self.assertEqual([j.id for j in self.service._fair_queue()], [ONE])
        self.assertEqual(before, [dict(vars(j)) for j in self.jobs])

    def test_scope_filter_precedes_bounded_query(self):
        self.jobs[:] = [SimpleNamespace(id=f'held-{i}', status='queued', actor_user_id='actor',
                       project_id=f'p{i}', created_at=i) for i in range(230)] + [self.jobs[0]]
        self.jobs[-1].created_at = 999
        self.assertEqual([j.id for j in self.service._fair_queue()], [ONE])

    def test_invalid_scope_holds_every_new_job(self):
        self.scope = mod.WorkerAdmissionScope('hold', 1050)
        self.assertEqual(self.service._fair_queue(), [])
        for job in self.jobs:
            self.assertFalse(self.env['_claim'](self.service, job.id))
            self.assertEqual(job.status, 'queued')

    def test_claim_rechecks_scope_instead_of_trusting_previous_queue(self):
        selected = self.service._fair_queue()
        self.scope = mod.WorkerAdmissionScope('hold', 1051)
        self.assertFalse(self.env['_claim'](self.service, selected[0].id))
        self.assertEqual(selected[0].status, 'queued')

    def test_scope_change_just_before_compare_and_set_prevents_claim(self):
        job = self.jobs[0]
        job.action, job.payload = 'analyze_materials', '{}'
        self.service._ensure = lambda project: SimpleNamespace(active_job_id=None, body='{"paused":false}')
        calls = []
        def changing_scope(ident):
            calls.append(ident)
            return None if len(calls) == 1 else 'maintenance_hold'
        self.env['worker_admission_block_reason'] = changing_scope
        self.assertFalse(self.env['_claim'](self.service, job.id))
        self.assertEqual(calls, [job.id, job.id])
        self.assertEqual(job.status, 'queued')

    def test_api_shows_maintenance_hold_for_non_scope_job_without_ids(self):
        self.service.execution_mode = 'api'
        snapshot = self.env['queue_snapshot'](self.service, self.jobs[1])
        self.assertEqual(snapshot['block_reason'], 'maintenance_hold')
        self.assertIsNone(snapshot['position'])
        self.assertNotIn(ONE, json.dumps(snapshot))
        self.assertNotIn(TWO, json.dumps(snapshot))

    def test_allowed_job_keeps_existing_worker_memory_projection(self):
        self.service.execution_mode = 'api'
        snapshot = self.env['queue_snapshot'](self.service, self.jobs[0])
        self.assertEqual(snapshot['block_reason'], 'memory_wait')
        self.assertEqual(snapshot['position'], 1)

    def test_running_job_is_not_stopped_or_held(self):
        self.jobs[1].status = 'running'
        self.assertIsNone(self.env['queue_snapshot'](self.service, self.jobs[1]))
        self.assertEqual(self.jobs[1].status, 'running')

    def test_terminal_retries_continue_while_scope_holds_new_claims(self):
        self.scope = mod.WorkerAdmissionScope('hold', 1050)
        waits = iter((False, True))
        self.service.stop_event = SimpleNamespace(wait=lambda timeout: next(waits))
        self.service.app.app_context = lambda: nullcontext()
        self.service.app.logger = logging.getLogger('scope-scheduler-fixture')
        before = [dict(vars(j)) for j in self.jobs]
        self.env['_schedule'](self.service)
        self.assertEqual(self.retries, ['retried'])
        self.assertEqual(before, [dict(vars(j)) for j in self.jobs])


if __name__ == '__main__':
    unittest.main(verbosity=2)
