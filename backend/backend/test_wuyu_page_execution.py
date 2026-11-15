"""Offline execution-policy/shared-I/O/trace RAM regression with synthetic data."""
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
import ast
import binascii
import json
import struct
import tempfile
import threading
import time
import unittest
import zlib

from services import wuyu_page_execution as execution
from services import wuyu_resource_limits as resources
from services.wuyu_errors import PipelineError

SERVICES = Path(execution.__file__).parent
MiB = 1024**2
EVIDENCE = {}


def await_condition(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(.005)
    raise AssertionError('Synthetic concurrency condition did not occur')


def load_model():
    tree = ast.parse((SERVICES / 'wuyu_workflow_models.py').read_text(encoding='utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ModelExecution')
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {'__init__', '_model_workers', '_model_batch'}]
    class Cancelled(Exception):
        pass
    class QuotaFailure(Exception):
        code = 'quota_exceeded'
    current = SimpleNamespace(progress='{}')
    db = SimpleNamespace(session=SimpleNamespace(get=lambda *_: current, commit=lambda: None, rollback=lambda: None))
    import concurrent.futures
    namespace = {'json': json, 'threading': threading, 'time': time, 'db': db, 'WuyuWorkflowJob': object, 'ModelExecutionPorts': object,
                 'FIRST_COMPLETED': concurrent.futures.FIRST_COMPLETED, 'page_slot': resources.page_slot,
                 'page_executor': execution.page_executor, 'validate_execution_policy': execution.validate_execution_policy,
                 'PipelineError': PipelineError, 'QuotaError': QuotaFailure, 'CancelledWork': Cancelled,
                 'WorkflowError': type('WorkflowError', (Exception,), {}),
                 'WorkflowAPIError': type('WorkflowAPIError', (Exception,), {})}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), '<actual-candidate-model-methods>', 'exec'), namespace)
    return namespace['ModelExecution'], Cancelled


ModelExecution, CancelledWork = load_model()


class ThreadCase(unittest.TestCase):
    def setUp(self):
        self.threads, self.events, self.apps, self.errors = [], [], [], []

    def tearDown(self):
        for event in self.events:
            event.set()
        for thread in self.threads:
            thread.join(8)
        for app in self.apps:
            execution.shutdown_page_executor(app)
        self.assertFalse(any(t.is_alive() for t in self.threads), 'Synthetic owner did not drain')
        self.assertEqual(self.errors, [])

    def event(self):
        event = threading.Event()
        self.events.append(event)
        return event

    def launch(self, function):
        def run():
            try:
                function()
            except BaseException as error:
                self.errors.append(error)
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread

    def app(self, capacity=200, budget=2*1024**3):
        app = SimpleNamespace(config={'WUYU_PAGE_IO_WORKERS': capacity, 'WUYU_PAGE_RESOURCE_BYTES': budget,
                                      'WUYU_LOCAL_EXPORT_WORKERS': 2, 'UPLOAD_FOLDER': '.'}, extensions={})
        self.apps.append(app)
        return app


class PolicyTests(unittest.TestCase):
    def _enqueue(self, config, retry_of=None):
        tree = ast.parse((SERVICES / 'wuyu_workflow_jobs.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'JobExecution')
        cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in {'__init__', '_new_job_record'}]
        snapshot = {'settings_version': 2, 'trace_workers': 1, 'image_workers': 8, 'fingerprint': 'fake-frozen'}
        predecessor = SimpleNamespace(project_id='project', action='export', payload=json.dumps({
            '__runtime_snapshot': snapshot, '__template_catalog_binding': {'id': 'fake-template'}}))
        original_predecessor = predecessor.payload
        saved = []
        class Record:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)
                self.id = 'fake-new-record'
            def set_progress(self, value):
                self.progress = value
        namespace = {'deepcopy': __import__('copy').deepcopy, 'json': json, 'Task': Record, 'WuyuWorkflowJob': Record,
                     'JobExecutionPorts': object, '_UNCAPTURED_CATALOG': object(), 'POLICY_KEY': execution.POLICY_KEY,
                     'bind_execution_policy': execution.bind_execution_policy,
                     'db': SimpleNamespace(session=SimpleNamespace(get=lambda *_: predecessor, add=saved.append, flush=lambda: None))}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), '<actual-candidate-new-job-record>', 'exec'), namespace)
        app = SimpleNamespace(config={**config, 'TESTING': True, 'WUYU_PIPELINE_FACTORY': object()}, extensions={})
        ports = SimpleNamespace(app=lambda: app, _material_management_guard=lambda *_: None,
            _reading_validate_job=lambda *_: None, _research_validate=lambda *_, **__: None,
            hook_public_template_choice=lambda *_: {}, _job_catalog_binding=lambda *_: {'id': 'fake-template'},
            hook__json=json.dumps, hook_initiating_user=lambda: 'fake-actor')
        model = namespace['JobExecution'](ports)
        payload = {'format': 'editable_pptx', '__runtime_snapshot': snapshot,
                   '__execution_policy': {'schema_version': 1, 'page_workers': 1}}
        model._new_job_record('project', 'export', payload, {'brief': {'page_count': 16}, 'epoch': 2}, retry_of=retry_of)
        self.assertEqual(predecessor.payload, original_predecessor)
        return saved[-1], json.loads(saved[-1].payload), snapshot

    def test_actual_enqueue_replaces_injected_policy_and_preserves_retry_model_reference(self):
        row, payload, old_snapshot = self._enqueue({'WUYU_EXECUTION_PAGE_WORKERS': 200, 'WUYU_PAGE_IO_WORKERS': 200}, retry_of='old-job')
        self.assertEqual(payload[execution.POLICY_KEY], {'schema_version': 1, 'page_workers': 200})
        self.assertEqual(payload['__runtime_snapshot'], old_snapshot)
        self.assertEqual(row.retry_of, 'old-job')

    def test_actual_enqueue_disabled_removes_client_policy(self):
        _, payload, old_snapshot = self._enqueue({})
        self.assertNotIn(execution.POLICY_KEY, payload)
        self.assertEqual(payload['__runtime_snapshot'], old_snapshot)

    def test_disabled_compatibility_and_untrusted_copy_removed(self):
        payload = {'__execution_policy': {'schema_version': 1, 'page_workers': 200}, 'format': 'editable_pptx'}
        execution.bind_execution_policy(payload, {})
        self.assertNotIn(execution.POLICY_KEY, payload)
        self.assertEqual(payload['format'], 'editable_pptx')

    def test_server_freezes_new_policy_without_changing_historical_model_snapshot(self):
        old = {'settings_version': 2, 'trace_workers': 1, 'image_workers': 8, 'fingerprint': 'synthetic'}
        before = json.dumps(old, sort_keys=True)
        payload = {'__runtime_snapshot': old, '__execution_policy': {'page_workers': 999}}
        execution.bind_execution_policy(payload, {'WUYU_EXECUTION_PAGE_WORKERS': 200, 'WUYU_PAGE_IO_WORKERS': 200})
        self.assertEqual(payload[execution.POLICY_KEY], {'schema_version': 1, 'page_workers': 200})
        self.assertEqual(json.dumps(old, sort_keys=True), before)

    def test_enabled_policy_without_shared_pool_fails_closed(self):
        with self.assertRaises(PipelineError):
            execution.freeze_execution_policy({'WUYU_EXECUTION_PAGE_WORKERS': 200})

    def test_invalid_config_is_not_coerced_or_clamped(self):
        for value in [-1, 201, True, '200', 1.5, None]:
            with self.subTest(value=value), self.assertRaises(PipelineError):
                execution.freeze_execution_policy({'WUYU_EXECUTION_PAGE_WORKERS': value, 'WUYU_PAGE_IO_WORKERS': 200})

    def test_unknown_or_extra_policy_fields_rejected(self):
        for value in [None, {}, {'schema_version': 2, 'page_workers': 200},
                      {'schema_version': 1, 'page_workers': True}, {'schema_version': 1, 'page_workers': 201},
                      {'schema_version': 1, 'page_workers': 200, 'api_key': 'not-allowed'}]:
            with self.subTest(value=value), self.assertRaises(PipelineError):
                execution.validate_execution_policy(value)

    def test_public_policy_never_exposes_unrecognized_input(self):
        self.assertIsNone(execution.public_execution_policy({'schema_version': 1, 'page_workers': 2, 'secret': 'fake'}))
        self.assertEqual(execution.public_execution_policy({'schema_version': 1, 'page_workers': 200}),
                         {'schema_version': 1, 'page_workers': 200})

    def test_old_job_uses_trace_one_new_job_uses_policy_200(self):
        model = ModelExecution(SimpleNamespace(app=lambda: SimpleNamespace(config={'WUYU_PAGE_IO_WORKERS': 200})))
        job = SimpleNamespace(_runtime_config={'WUYU_TRACE_WORKERS': 1, 'WUYU_IMAGE_WORKERS': 8})
        self.assertEqual(model._model_workers(job, 'trace'), 1)
        job._execution_policy = {'schema_version': 1, 'page_workers': 200}
        self.assertEqual(model._model_workers(job, 'trace'), 200)
        self.assertEqual(job._runtime_config['WUYU_TRACE_WORKERS'], 1)

    def test_server_scrubs_before_bind_and_run_validates_before_pipeline(self):
        tree = ast.parse((SERVICES / 'wuyu_workflow_jobs.py').read_text(encoding='utf-8'))
        new = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_new_job_record')
        text = ast.unparse(new)
        self.assertLess(text.index('payload.pop(POLICY_KEY'), text.index('bind_execution_policy(payload'))
        wrapper = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_run')
        delegates = [n for n in ast.walk(wrapper) if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute) and n.func.attr == '_run_execution']
        self.assertEqual(len(delegates), 1, 'Owner wrapper must delegate business execution exactly once')
        self.assertFalse(any(isinstance(n, ast.Attribute) and n.attr == '_pipeline' for n in ast.walk(wrapper)))
        run = ast.unparse(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_run_execution'))
        self.assertLess(run.index('validate_execution_policy'), run.index('pipeline ='))


class PoolTests(ThreadCase):
    def test_singleton_one_executor_and_16_real_threads_can_dispatch(self):
        app = self.app()
        calls, began, release = [], [], self.event()
        def factory(**kwargs):
            calls.append(kwargs)
            return ThreadPoolExecutor(**kwargs)
        with execution.page_executor(app, factory, 200, lambda: None) as batch:
            futures = []
            for number in range(16):
                permit = batch.try_reserve()
                self.assertIsNotNone(permit)
                with permit:
                    futures.append(permit.submit(lambda n=number: (began.append(n), release.wait(5))))
            await_condition(lambda: len(began) == 16)
            self.assertEqual(app.extensions['wuyu_page_io_pool'].outstanding, 16)
            release.set()
            wait(futures)
        with execution.page_executor(app, factory, 200, lambda: None):
            pass
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['max_workers'], 200)

    def test_20_registered_actors_share_200_reservations_and_reclaim_fair_share(self):
        pool = execution.SharedPagePool(200, ThreadPoolExecutor)
        try:
            with ExitStack() as stack:
                batches = [stack.enter_context(pool.batch(200)) for _ in range(20)]
                self.assertEqual([b.window for b in batches], [10]*20)
                permits = [[b.try_reserve() for _ in range(10)] for b in batches]
                self.assertEqual(pool.outstanding, 200)
                self.assertTrue(all(b.try_reserve() is None for b in batches))
                for actor in permits:
                    for permit in actor:
                        permit.__exit__(None, None, None)
                self.assertEqual(pool.outstanding, 0)
                self.assertEqual(pool.peak, 200)
                EVIDENCE['fair_reservations'] = {'registered_actors': 20, 'window_each': 10, 'outstanding_peak': pool.peak, 'remaining': pool.outstanding}
        finally:
            pool.shutdown()

    def test_reservation_exception_returns_capacity(self):
        app = self.app(1)
        with execution.page_executor(app, ThreadPoolExecutor, 200, lambda: None) as batch:
            with self.assertRaises(ValueError):
                with batch.try_reserve():
                    raise ValueError('synthetic prepare failure')
            self.assertEqual(app.extensions['wuyu_page_io_pool'].outstanding, 0)
            with batch.try_reserve():
                pass

    def test_late_batch_restricts_new_refill_without_preempting_existing(self):
        pool = execution.SharedPagePool(4, ThreadPoolExecutor)
        try:
            with pool.batch(200) as older:
                permits = [older.try_reserve() for _ in range(3)]
                with pool.batch(200) as newer:
                    self.assertEqual(older.window, 2)
                    self.assertIsNone(older.try_reserve())
                    with newer.try_reserve():
                        self.assertEqual(pool.outstanding, 4)
                for permit in permits:
                    permit.__exit__(None, None, None)
        finally:
            pool.shutdown()


class ModelBatchTests(ThreadCase):
    def owner(self, app, cancel=None):
        accepted, refunds, settled, timings = [], [], [], []
        owner_thread = threading.get_ident()
        def check(_job):
            if cancel is not None and cancel.is_set():
                raise CancelledWork()
        class Charge:
            def reserve(self):
                return self
            def accepting(self, **_):
                return nullcontext()
            def settle(self):
                settled.append(True)
            def refund(self, *_):
                refunds.append(True)
        ports = SimpleNamespace(app=lambda: app, hook_ThreadPoolExecutor=ThreadPoolExecutor,
                                hook_WorkflowCharge=lambda *_: Charge(), _check_cancel=check,
                                _pipeline=lambda config: SimpleNamespace(config=config), hook_wait=wait,
                                hook__safe_failure_code=lambda value: value, hook__failure_message=lambda value: value,
                                _record_batch_timing=lambda *args: timings.append(args), hook__json=json.dumps)
        model = ModelExecution(ports)
        job = SimpleNamespace(id='fake-job', _runtime_config={})
        def batch(plans, invoke, **kwargs):
            return model._model_batch(job, plans, lambda x: x, invoke,
                                      lambda plan, output: accepted.append((plan, output)),
                                      lambda *_: None, 200, resource_stage='trace', **kwargs)
        return batch, accepted, refunds, settled, timings

    def test_16_trace_pages_enter_together_under_2GiB_and_accept_all(self):
        app, release, began = self.app(), self.event(), []
        batch, accepted, refunds, settled, timings = self.owner(app)
        def invoke(plan, _pipeline, cancel):
            self.assertFalse(cancel())
            began.append(plan['number'])
            release.wait(5)
            return {'page': plan['number']}
        thread = self.launch(lambda: batch([{'number': i} for i in range(16)], invoke))
        await_condition(lambda: len(began) == 16)
        self.assertEqual(resources.resources(app).budget.used, 16*64*MiB)
        release.set()
        thread.join(5)
        self.assertEqual(len(accepted), 16)
        self.assertEqual(len(settled), 16)
        self.assertEqual(len(timings), 16)
        self.assertEqual(refunds, [])
        self.assertEqual(resources.resources(app).budget.used, 0)
        EVIDENCE['trace_16'] = {'concurrent_invocations': len(began), 'accepted': len(accepted), 'settled': len(settled),
                                'shared_pool_peak': app.extensions['wuyu_page_io_pool'].peak,
                                'resource_peak_bytes': resources.resources(app).budget.peak}

    def test_shared_pool_full_does_not_block_cancelled_owner_with_no_pending(self):
        app, cancel = self.app(1), self.event()
        batch, accepted, refunds, _, _ = self.owner(app, cancel)
        cancelled = []
        with execution.page_executor(app, ThreadPoolExecutor, 200, lambda: None) as holding:
            with holding.try_reserve():
                def run():
                    try:
                        batch([{'number': 1}], lambda *_: self.fail('New work must not dispatch'))
                    except CancelledWork:
                        cancelled.append(True)
                thread = self.launch(run)
                await_condition(lambda: len(app.extensions['wuyu_page_io_pool'].batches) == 2)
                cancel.set()
                thread.join(2)
                self.assertEqual(cancelled, [True])
                self.assertEqual(accepted, [])
                self.assertEqual(refunds, [])

    def test_owner_accepts_completed_result_while_other_batch_holds_capacity(self):
        app, accepted_event = self.app(2), self.event()
        batch, accepted, _, _, _ = self.owner(app)
        with execution.page_executor(app, ThreadPoolExecutor, 200, lambda: None) as holding:
            with holding.try_reserve():
                thread = self.launch(lambda: batch([{'number': 1}, {'number': 2}], lambda *_: {'ok': True}))
                await_condition(lambda: len(accepted) == 2)
                self.assertEqual(app.extensions['wuyu_page_io_pool'].outstanding, 1)
                thread.join(2)

    def test_20_concurrent_actors_finish_without_20_thread_pools_or_starvation(self):
        app, start = self.app(), threading.Barrier(20)
        all_accepted = []
        lock = threading.Lock()
        def actor(actor_number):
            batch, accepted, refunds, _, _ = self.owner(app)
            start.wait(5)
            def invoke(plan, *_):
                threading.Event().wait(.005)
                return {'actor': actor_number, 'page': plan['number']}
            batch([{'number': n} for n in range(20)], invoke)
            self.assertFalse(refunds)
            self.assertEqual(len(accepted), 20)
            self.assertTrue(all(out['actor'] == actor_number for _, out in accepted))
            with lock:
                all_accepted.append(actor_number)
        for actor_number in range(20):
            self.launch(lambda n=actor_number: actor(n))
        await_condition(lambda: len(all_accepted) == 20, timeout=10)
        pool = app.extensions['wuyu_page_io_pool']
        self.assertLessEqual(pool.peak, 200)
        self.assertEqual(pool.outstanding, 0)
        self.assertLessEqual(len(pool.executor._threads), 200)
        self.assertLessEqual(resources.resources(app).budget.peak, 2*1024**3)
        EVIDENCE['actors_20_pages_400'] = {'actors_completed': len(all_accepted), 'pages_completed': 400,
                                          'shared_pool_peak': pool.peak, 'physical_page_threads': len(pool.executor._threads),
                                          'resource_peak_bytes': resources.resources(app).budget.peak,
                                          'remaining': pool.outstanding}

    def test_two_local_exports_share_budget_with_trace_pages(self):
        app, release, began = self.app(), self.event(), []
        batch, accepted, _, _, _ = self.owner(app)
        with resources.export_slot(app, lambda: None), resources.export_slot(app, lambda: None):
            thread = self.launch(lambda: batch([{'number': i} for i in range(16)],
                lambda plan, *_: (began.append(plan['number']), release.wait(5), {'ok': True})[-1]))
            await_condition(lambda: len(began) == 8)
            self.assertEqual(resources.resources(app).budget.used, 2*768*MiB + 8*64*MiB)
        release.set()
        thread.join(5)
        self.assertEqual(len(accepted), 16)
        self.assertEqual(resources.resources(app).budget.used, 0)

    def test_cancel_drains_paid_output_but_never_accepts_newer_epoch(self):
        app, cancel, release, began = self.app(2), self.event(), self.event(), []
        batch, accepted, refunds, _, timings = self.owner(app, cancel)
        paid_outputs, cancelled = [], []
        def invoke(plan, _pipeline, cancel_callback):
            began.append(plan['number'])
            release.wait(5)
            self.assertFalse(cancel_callback())
            paid_outputs.append(plan['number'])
            return {'saved_output': True}
        def run():
            try:
                batch([{'number': i} for i in range(5)], invoke)
            except CancelledWork:
                cancelled.append(True)
        thread = self.launch(run)
        await_condition(lambda: len(began) == 2)
        cancel.set()
        threading.Event().wait(.15)
        release.set()
        thread.join(5)
        self.assertEqual(sorted(paid_outputs), [0, 1])
        self.assertEqual(accepted, [])
        self.assertEqual(len(refunds), 2)
        self.assertEqual(len(timings), 2)
        self.assertEqual(cancelled, [True])


def png(path, width, height):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', binascii.crc32(kind+data) & 0xffffffff)
    codec = zlib.compressobj()
    row = b'\0' + b'\x10\x20\x30\xff'*width
    blocks = [codec.compress(row) for _ in range(height)] + [codec.flush()]
    data = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)) + chunk(b'IDAT', b''.join(blocks)) + chunk(b'IEND', b'')
    path.write_bytes(data)
    return path


class ResourceStageTests(unittest.TestCase):
    def test_stage_is_explicit_and_cannot_come_from_untrusted_plan(self):
        self.assertEqual(resources.page_weight({'stage': 'trace'}, '.'), 384*MiB)
        with self.assertRaises(ValueError):
            resources.page_weight({}, '.', stage='untrusted')

    def test_common_1080p_16_pages_fit_2GiB_while_image_reserve_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            image = png(Path(folder)/'self-created.png', 1920, 1080)
            plan = {'source': str(image.resolve())}
            trace = resources.page_weight(plan, folder, stage='trace')
            original = resources.page_weight(plan, folder)
            self.assertEqual(original-trace, (384-64)*MiB)
            self.assertEqual(trace, 64*MiB + 6*image.stat().st_size + 8*1920*1080)
            self.assertLess(16*trace, 2*1024**3)

    def test_trace_counts_all_controlled_inputs_not_only_largest_nine(self):
        with tempfile.TemporaryDirectory() as folder:
            images = [png(Path(folder)/f'{i}.png', 8, 8) for i in range(10)]
            plan = {'assets': [{'path': str(p.resolve())} for p in images]}
            trace = resources.page_weight(plan, folder, stage='trace')
            self.assertEqual(trace, 64*MiB + sum(6*p.stat().st_size+8*8*8 for p in images))


if __name__ == '__main__':
    unittest.main()
