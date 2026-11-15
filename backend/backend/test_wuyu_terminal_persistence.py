"""172 terminal contract: pure state/ownership cases and isolated PostgreSQL fault injection.

Pure classes run without Flask/SQLAlchemy. PostgreSQLTerminalTests requires an explicit
terminal172_fixture database on loopback; never loads the product app or live config.
"""
from __future__ import annotations
import ast
from contextlib import nullcontext, contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace, ModuleType, MethodType
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

terminal = sys.modules.get('services.wuyu_workflow_terminal') or load_file('services.wuyu_workflow_terminal', ROOT / 'services/wuyu_workflow_terminal.py')
execution = sys.modules.get('workflow_execution') or load_file('workflow_execution', ROOT / 'workflow_execution.py')
FINISHED = datetime(2026, 11, 12, 12, 0, 0, 123456)

def fixture(action='_prepare_relayout'):
    job = SimpleNamespace(id='job172', project_id='project172', task_id='task172', actor_user_id='actor172',
        action=action, input_epoch=7, worker_pid=412, started_at=FINISHED-timedelta(seconds=30),
        idempotency_key='test172-request', payload='{}', status='running', error_code=None,
        progress='{"quota_receipts":{"reservation172":{"actual_points":13}},"stage":"done"}', completed_at=None)
    task = SimpleNamespace(id='task172', project_id='project172', task_type='WUYU_WORKFLOW', status='PROCESSING',
        error_message=None, completed_at=None, progress='{"completed":1}')
    body = {'epoch':7,'material_revision':2,'reading':{'_owner_job_id':job.id,'source_revision':2,
        'status':'reading','total_segments':3,'completed_segments':1,'issues':[]},
        'relayout':{'status':'preparing'},'research':{'status':'running'},'workflow':{'events':[]}}
    workflow = SimpleNamespace(project_id=job.project_id, revision=4, body=terminal.encoded(body), active_job_id=job.id,
        updated_at=job.started_at)
    return job, task, workflow

class MemoryStore:
    """Only an IO boundary: execute the actual pure prepare/reconcile code."""
    def __init__(self, job, task, workflow):
        self.job, self.task, self.workflow = job, task, workflow
        self.calls = self.writes = self.cleanup_calls = self.fee_calls = self.fee_commits = 0
        self.fail_before = self.fail_after = self.fee_ack_unknown = False
        self.logs = []
    def close(self, body, message):
        self.cleanup_calls += 1
        body['workflow']['events'].append({'id':'frozen-event-'+str(self.cleanup_calls),'message':message})
    def recover(self, app):
        self.fee_calls += 1
        if not self.fee_commits:
            self.fee_commits = 1
            if self.fee_ack_unknown:
                self.fee_ack_unknown = False
                raise OSError('PRIVATE-CREDENTIAL-DO-NOT-LOG')
    def persist(self, record, ports):
        self.calls += 1
        if record.target is None:
            record.target = terminal.prepare_target(record, self.job, self.task, self.workflow, ports)
        changes = terminal.reconcile_target(record, self.job, self.task, self.workflow)
        if self.fail_before:
            self.fail_before = False
            raise OSError('PRIVATE-BODY-DO-NOT-LOG')
        if changes:
            self.writes += 1
            for name, values in changes.items():
                for key, value in values.items():
                    setattr(getattr(self, name), key, deepcopy(value))
        if self.fail_after:
            self.fail_after = False
            raise OSError('PRIVATE-ACK-DO-NOT-LOG')

def owner_fixture(status='completed', action='_prepare_relayout'):
    job, task, workflow = fixture(action)
    store = MemoryStore(job, task, workflow)
    app = SimpleNamespace(config={}, logger=SimpleNamespace(warning=lambda *args:store.logs.append(args)))
    clock = [0.0]
    owner = terminal.TerminalPersistence(terminal.TerminalPorts(lambda:app, store.close, store.recover), clock=lambda:clock[0])
    owner.register_claim(job)
    assert owner.begin_execution(job.id)
    assert owner.bind_execution(job)
    owner.freeze_outcome(job.id, status, None if status=='completed'else'CANCELLED_OR_INPUT_CHANGED'if status=='cancelled'else'FIXED_PROVIDER_ERROR',
        None if status=='completed'else{'code':'FIXED_PROVIDER_ERROR','diagnostics':{}},completed_at=FINISHED)
    return owner, store, clock

def lifecycle_function(name):
    tree = ast.parse((ROOT/'services/wuyu_workflow_lifecycle.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name==name)
    return compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])), 'actual-lifecycle172-'+name, 'exec')

class Query:
    def __init__(self, rows):self.rows=list(rows)
    def filter_by(self, **values):return Query([row for row in self.rows if all(getattr(row,k)==v for k,v in values.items())])
    def order_by(self, *args):return self
    def limit(self, n):return Query(self.rows[:n])
    def all(self):return self.rows

class TargetContractTests(unittest.TestCase):
    def test_three_outcomes_share_same_atomic_target(self):
        for status in ['completed','failed','cancelled']:
            with self.subTest(status=status):
                owner, store, _=owner_fixture(status)
                with patch.object(terminal,'persist_terminal',store.persist):self.assertTrue(owner.attempt(store.job.id))
                self.assertEqual(store.job.status,status);self.assertEqual(store.task.status,'COMPLETED'if status=='completed'else'FAILED')
                self.assertEqual(store.job.completed_at,FINISHED);self.assertEqual(store.task.completed_at,FINISHED)
                self.assertIsNone(store.workflow.active_job_id);self.assertEqual(owner.snapshot()['pending_terminal'],0)
                self.assertEqual(json.loads(store.job.progress)['quota_receipts']['reservation172']['actual_points'],13)
    def test_freeze_details_and_completion_time_once(self):
        job,_,_=fixture();clock=[0];app=SimpleNamespace(logger=SimpleNamespace(warning=lambda *a:None))
        owner=terminal.TerminalPersistence(terminal.TerminalPorts(lambda:app,lambda *a:None,lambda *a:None),clock=lambda:clock[0]);owner.register_claim(job)
        details={'code':'safe','diagnostics':{'count':1}};owner.freeze_outcome(job.id,'failed','safe',details,completed_at=FINISHED);details['diagnostics']['count']=99
        self.assertEqual(json.loads(owner._records[job.id].outcome.error_details_json)['diagnostics']['count'],1)
        with self.assertRaises(terminal.TerminalConflict):owner.freeze_outcome(job.id,'completed',None)
        self.assertEqual(owner._records[job.id].outcome.completed_at,FINISHED)
    def test_failure_cleanup_frozen_once_after_precommit_failure(self):
        owner,store,clock=owner_fixture('failed');store.fail_before=True
        with patch.object(terminal,'persist_terminal',store.persist):
            self.assertFalse(owner.attempt(store.job.id));frozen=deepcopy(owner._records[store.job.id].target)
            self.assertEqual(store.job.status,'running');clock[0]=1;self.assertTrue(owner.attempt(store.job.id))
        self.assertEqual(store.cleanup_calls,1);self.assertEqual(store.workflow.body,frozen.workflow_after['body']);self.assertEqual(store.workflow.revision,5)
    def test_commit_ack_unknown_exact_readback_never_rewrites(self):
        owner,store,clock=owner_fixture('failed');store.fail_after=True
        with patch.object(terminal,'persist_terminal',store.persist):
            self.assertFalse(owner.attempt(store.job.id));after=deepcopy(vars(store.workflow));clock[0]=1
            self.assertTrue(owner.attempt(store.job.id));self.assertTrue(owner.attempt(store.job.id))
        self.assertEqual(store.writes,1);self.assertEqual(vars(store.workflow),after);self.assertEqual(store.cleanup_calls,1);self.assertEqual(store.fee_commits,1)
    def test_ack_after_new_revision_owner_and_body_writes_nothing(self):
        owner,store,clock=owner_fixture('failed');store.fail_after=True
        with patch.object(terminal,'persist_terminal',store.persist):
            self.assertFalse(owner.attempt(store.job.id));store.workflow.body='{"epoch":8,"human":"keep"}';store.workflow.revision=15;store.workflow.active_job_id='new-job'
            new=deepcopy(vars(store.workflow));clock[0]=1;self.assertTrue(owner.attempt(store.job.id))
        self.assertEqual(vars(store.workflow),new);self.assertEqual(store.writes,1)
    def test_partial_matching_components_only_fill_missing(self):
        owner,store,_=owner_fixture('failed');record=owner._records[store.job.id]
        record.target=terminal.prepare_target(record,store.job,store.task,store.workflow,owner.ports)
        for key,value in record.target.job_after.items():setattr(store.job,key,deepcopy(value))
        with patch.object(terminal,'persist_terminal',store.persist):self.assertTrue(owner.attempt(store.job.id))
        self.assertEqual(store.task.status,'FAILED');self.assertEqual(store.workflow.revision,5);self.assertEqual(store.cleanup_calls,1)
    def test_partial_terminal_with_new_owner_is_not_overwritten(self):
        owner,store,_=owner_fixture('failed');record=owner._records[store.job.id];record.target=terminal.prepare_target(record,store.job,store.task,store.workflow,owner.ports)
        for key,value in record.target.job_after.items():setattr(store.job,key,value)
        store.workflow.active_job_id='new-job';before=deepcopy(vars(store.workflow))
        with patch.object(terminal,'persist_terminal',store.persist):self.assertFalse(owner.attempt(store.job.id))
        self.assertEqual(vars(store.workflow),before);self.assertEqual(store.task.status,'PROCESSING');self.assertEqual(owner.snapshot()['pending_terminal_blocked'],1)
    def test_claim_identity_payload_and_terminal_conflicts_retain_owner(self):
        cases={'actor_user_id':'other','worker_pid':999,'started_at':FINISHED,'project_id':'other','task_id':'other','action':'other','input_epoch':8,'idempotency_key':'other','payload':'{"changed":true}'}
        for key,value in cases.items():
            with self.subTest(key=key):
                owner,store,_=owner_fixture();setattr(store.job,key,value)
                with patch.object(terminal,'persist_terminal',store.persist):self.assertFalse(owner.attempt(store.job.id))
                self.assertEqual(owner.snapshot()['pending_terminal'],1);self.assertEqual(store.task.status,'PROCESSING')
    def test_foreign_task_terminal_never_overwritten(self):
        owner,store,_=owner_fixture();store.task.status='FAILED';store.task.completed_at=FINISHED-timedelta(days=1)
        with patch.object(terminal,'persist_terminal',store.persist):self.assertFalse(owner.attempt(store.job.id))
        self.assertEqual(store.job.status,'running');self.assertEqual(store.task.completed_at,FINISHED-timedelta(days=1))
    def test_failed_old_epoch_does_not_close_new_epoch(self):
        owner,store,_=owner_fixture('cancelled');store.workflow.body='{"epoch":8,"human":"keep"}';store.workflow.revision=99
        with patch.object(terminal,'persist_terminal',store.persist):self.assertTrue(owner.attempt(store.job.id))
        self.assertEqual(store.workflow.body,'{"epoch":8,"human":"keep"}');self.assertEqual(store.workflow.revision,99);self.assertEqual(store.cleanup_calls,0)
    def test_changed_body_after_frozen_uncommitted_target_is_conflict(self):
        owner,store,clock=owner_fixture('failed');store.fail_before=True
        with patch.object(terminal,'persist_terminal',store.persist):
            self.assertFalse(owner.attempt(store.job.id));store.workflow.body='{"epoch":8,"human":"new"}';store.workflow.revision=99;clock[0]=1
            self.assertFalse(owner.attempt(store.job.id))
        self.assertEqual(store.job.status,'running');self.assertEqual(store.workflow.revision,99);self.assertEqual(owner.snapshot()['pending_terminal_blocked'],1)
    def test_reading_failure_uses_original_owner_source_and_status_guard(self):
        job,_,workflow=fixture('analyze_materials');body=json.loads(workflow.body)
        self.assertFalse(terminal.apply_reading_failure(body,'foreign',7,'cancelled'));self.assertEqual(body['reading']['status'],'reading')
        self.assertTrue(terminal.apply_reading_failure(body,job.id,6,'CANCELLED_OR_INPUT_CHANGED'))
        self.assertEqual(body['reading']['interruption']['reason'],'inputs_changed');self.assertEqual(body['reading']['failed_segments'],2)
        once=deepcopy(body);self.assertFalse(terminal.apply_reading_failure(body,job.id,6,'x'));self.assertEqual(body,once)
    def test_reading_failure_source_revision_mismatch_keeps_new_reader(self):
        _,_,workflow=fixture('analyze_materials');body=json.loads(workflow.body);body['material_revision']=3;before=deepcopy(body)
        self.assertFalse(terminal.apply_reading_failure(body,'job172',7,'x'));self.assertEqual(body,before)
    def test_relayout_and_research_cleanup_are_same_transaction_targets(self):
        for action,key in [('_prepare_relayout','relayout'),('run_research','research')]:
            owner,store,_=owner_fixture('failed',action)
            with patch.object(terminal,'persist_terminal',store.persist):self.assertTrue(owner.attempt(store.job.id))
            self.assertEqual(json.loads(store.workflow.body)[key]['status'],'failed')
    def test_known_own_payload_changes_are_exact_not_arbitrary(self):
        job,task,workflow=fixture();owner,store,_=owner_fixture();record=owner._records['job172']
        with self.assertRaises(terminal.TerminalConflict):owner.allow_own_payload('job172','{"late":1}')
        record.outcome=None;owner.allow_own_payload('job172','{"__runtime_snapshot":"own"}');store.job.payload='{"__runtime_snapshot":"own"}'
        terminal.check_identity(record,store.job,store.task,store.workflow)
        store.job.payload='{"__runtime_snapshot":"foreign"}'
        with self.assertRaises(terminal.TerminalConflict):terminal.check_identity(record,store.job,store.task,store.workflow)

class OwnerLifecycleTests(unittest.TestCase):
    def test_fee_ack_unknown_keeps_terminal_time_and_charges_once(self):
        owner,store,clock=owner_fixture();store.fee_ack_unknown=True
        with patch.object(terminal,'persist_terminal',store.persist):
            self.assertFalse(owner.attempt(store.job.id));self.assertEqual(store.job.status,'completed');self.assertEqual(owner.snapshot()['pending_terminal'],1)
            self.assertEqual(owner._records[store.job.id].stage,'charge_pending');clock[0]=1;self.assertTrue(owner.attempt(store.job.id))
        self.assertEqual(store.fee_calls,2);self.assertEqual(store.fee_commits,1);self.assertEqual(store.job.completed_at,FINISHED);self.assertEqual(store.writes,1);self.assertEqual(store.calls,1)
    def test_budget_exhaustion_retains_pending_and_does_not_retry_forever(self):
        owner,store,clock=owner_fixture();calls=[]
        def unavailable(*args):calls.append(1);raise OSError('private')
        with patch.object(terminal,'persist_terminal',unavailable):
            for now in [0,1,3,7,15,23,1000]:clock[0]=now;owner.retry_due()
        self.assertEqual(len(calls),5);self.assertEqual(owner.snapshot()['pending_terminal'],1);self.assertEqual(owner.snapshot()['pending_terminal_blocked'],1)
    def test_window_is_attempt_admission_not_discard(self):
        owner,store,clock=owner_fixture();clock[0]=91
        with patch.object(terminal,'persist_terminal',store.persist):self.assertFalse(owner.attempt(store.job.id))
        self.assertEqual(store.calls,0);self.assertEqual(owner.snapshot()['pending_terminal'],1)
    def test_stop_repeated_does_not_extend_persistence_admission(self):
        owner,store,clock=owner_fixture();owner.stop();clock[0]=4;owner.stop();clock[0]=5.1
        with patch.object(terminal,'persist_terminal',store.persist):self.assertFalse(owner.attempt(store.job.id))
        self.assertEqual(store.calls,0);self.assertEqual(owner.snapshot()['pending_terminal'],1)
    def test_stop_still_saves_already_frozen_result_within_window(self):
        owner,store,clock=owner_fixture();owner.stop();clock[0]=4
        with patch.object(terminal,'persist_terminal',store.persist):self.assertTrue(owner.attempt(store.job.id))
        self.assertEqual(store.job.status,'completed')
    def test_started_db_attempt_drains_even_if_stop_window_elapses(self):
        owner,store,clock=owner_fixture();entered=threading.Event();release=threading.Event()
        def slow(record,ports):entered.set();release.wait(2);store.persist(record,ports)
        with patch.object(terminal,'persist_terminal',slow):
            thread=threading.Thread(target=lambda:owner.attempt(store.job.id));thread.start();self.assertTrue(entered.wait(1));owner.stop();clock[0]=100;release.set();thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(store.job.status,'completed');self.assertEqual(owner.snapshot()['pending_terminal'],0)
    def test_two_reconciliation_callers_are_single_writer(self):
        owner,store,_=owner_fixture('failed');entered=threading.Event();release=threading.Event()
        def slow(record,ports):entered.set();release.wait(2);store.persist(record,ports)
        with patch.object(terminal,'persist_terminal',slow):
            thread=threading.Thread(target=lambda:owner.attempt(store.job.id));thread.start();self.assertTrue(entered.wait(1));self.assertFalse(owner.attempt(store.job.id));release.set();thread.join(2)
        self.assertEqual(store.writes,1);self.assertEqual(store.cleanup_calls,1);self.assertEqual(store.fee_commits,1)
    def test_unknown_claim_or_preparation_is_not_released_by_done_future(self):
        owner,store,_=owner_fixture();owner._records['job172'].outcome=None;owner.mark_unknown('job172','TERMINAL_PREPARE_UNKNOWN')
        owner.retry_due();self.assertEqual(store.calls,0);self.assertEqual(owner.snapshot()['pending_terminal_unknown'],1);self.assertIn('job172',owner.occupied())
    def test_duplicate_execution_entry_never_dispatches_twice(self):
        owner,store,_=owner_fixture();self.assertFalse(owner.begin_execution('job172'));self.assertEqual(store.calls,0)
    def test_safe_diagnostics_do_not_contain_upstream_text(self):
        owner,store,_=owner_fixture();store.fail_before=True
        with patch.object(terminal,'persist_terminal',store.persist):owner.attempt(store.job.id)
        self.assertNotIn('PRIVATE',str(store.logs));self.assertEqual(str(terminal.TerminalConflict('PRIVATE-RAW')),'TERMINAL_CONFLICT')
    def test_pending_terminal_database_row_still_blocks_actor_and_project(self):
        owner,store,_=owner_fixture();store.job.status='completed'
        a=SimpleNamespace(id='new-a',actor_user_id='actor172',project_id='new-project',status='queued',created_at=1)
        b=SimpleNamespace(id='new-b',actor_user_id='other',project_id='other-project',status='queued',created_at=2)
        c=SimpleNamespace(id='same-project',actor_user_id='other',project_id='project172',status='queued',created_at=0)
        model=SimpleNamespace(query=Query([a,b,c,store.job]),created_at='created_at')
        env={'WuyuWorkflowJob':model};exec(lifecycle_function('_fair_queue'),env)
        service=SimpleNamespace(app=SimpleNamespace(config={'WUYU_WORKFLOW_PER_ACTOR':1}),terminal_owner=owner)
        self.assertEqual([j.id for j in env['_fair_queue'](service)],['new-b'])
    def test_actor_capacity_counts_db_and_local_job_once(self):
        owner,store,_=owner_fixture();a=SimpleNamespace(id='new-a',actor_user_id='actor172',project_id='new-project',status='queued',created_at=1)
        model=SimpleNamespace(query=Query([a,store.job]),created_at='created_at');env={'WuyuWorkflowJob':model};exec(lifecycle_function('_fair_queue'),env)
        service=SimpleNamespace(app=SimpleNamespace(config={'WUYU_WORKFLOW_PER_ACTOR':2}),terminal_owner=owner)
        self.assertEqual([j.id for j in env['_fair_queue'](service)],['new-a'])
    def test_memory_highwater_drains_terminal_before_admission(self):
        owner,store,_=owner_fixture();waits=iter([False,True]);future=SimpleNamespace(done=lambda:True)
        service=SimpleNamespace(app=SimpleNamespace(config={},app_context=lambda:nullcontext(),logger=SimpleNamespace(warning=lambda *a:None)),terminal_owner=owner,stop_event=SimpleNamespace(wait=lambda _:next(waits)),future_lock=threading.Lock(),futures={'job172':future},executor=SimpleNamespace(_max_workers=20))
        env={'memory_admission':lambda _:{'allowed':False}};exec(lifecycle_function('_schedule'),env)
        with patch.object(terminal,'persist_terminal',store.persist):env['_schedule'](service)
        self.assertEqual(store.job.status,'completed');self.assertEqual(service.futures,{});self.assertEqual(store.fee_commits,1)
    def test_memory_highwater_keeps_unclassified_done_future(self):
        owner,store,_=owner_fixture();owner._records['job172'].outcome=None;waits=iter([False,True]);future=SimpleNamespace(done=lambda:True)
        service=SimpleNamespace(app=SimpleNamespace(config={},app_context=lambda:nullcontext(),logger=SimpleNamespace(warning=lambda *a:None)),terminal_owner=owner,stop_event=SimpleNamespace(wait=lambda _:next(waits)),future_lock=threading.Lock(),futures={'job172':future},executor=SimpleNamespace(_max_workers=20))
        env={'memory_admission':lambda _:{'allowed':False}};exec(lifecycle_function('_schedule'),env);env['_schedule'](service)
        self.assertEqual(len(service.futures),1);self.assertEqual(owner.snapshot()['pending_terminal_unknown'],1)
    def test_heartbeat_rejects_missing_boolean_negative_pending(self):
        with tempfile.TemporaryDirectory(prefix='terminal172-heartbeat-')as folder:
            directory=Path(folder);execution.write_heartbeat('ready',directory,pending_terminal=1);self.assertTrue(execution.heartbeat_ready(directory))
            body=json.loads((directory/'workflow-worker.json').read_text());self.assertEqual(body['pending_terminal'],1)
            for invalid in [None,True,-1,1.0]:
                body['pending_terminal']=invalid;(directory/'workflow-worker.json').write_text(json.dumps(body));self.assertFalse(execution.heartbeat_ready(directory))
            body.pop('pending_terminal');(directory/'workflow-worker.json').write_text(json.dumps(body));self.assertFalse(execution.heartbeat_ready(directory))
    def test_heartbeat_writer_requires_strict_integer(self):
        with tempfile.TemporaryDirectory(prefix='terminal172-heartbeat-')as folder:
            for invalid in [True,-1,1.0]:
                with self.assertRaises(ValueError):execution.write_heartbeat('ready',Path(folder),pending_terminal=invalid)


class ExecutionBoundaryTests(unittest.TestCase):
    """Execute the actual candidate _run/_run_execution AST, replacing IO ports only."""
    def setup_execution(self, *, work_error=None, fetch_error=False, start_error=False, join_error=False, reset_error=False, rollback_error=False):
        owner,store,clock=owner_fixture();record=owner._records['job172'];record.outcome=None;record.execution_entered=False;record.stage='claimed';record.first_pending=None
        state=SimpleNamespace(work_calls=0,done_event=None,rollback_calls=0)
        class Cancelled(Exception):pass
        class Pipeline(Exception):pass
        class Contract(Exception):pass
        class FakeContext:
            def __init__(self,name,fail=False):self.inner=ContextVar(name,default=None);self.fail=fail
            def set(self,value):return self.inner.set(value)
            def reset(self,token):
                if self.fail:raise OSError('PRIVATE-CONTEXT')
                self.inner.reset(token)
        class Session:
            def get(self,kind,key):
                if fetch_error:raise OSError('PRIVATE-FETCH')
                return store.job
            def rollback(self):
                state.rollback_calls+=1
                if rollback_error and state.rollback_calls==1:raise OSError('PRIVATE-ROLLBACK')
        class Heartbeat:
            def __init__(self,**kwargs):state.done_event=kwargs['args'][1]
            def start(self):
                if start_error:raise OSError('PRIVATE-HEARTBEAT-START')
            def join(self,timeout):
                if join_error:raise OSError('PRIVATE-HEARTBEAT-JOIN')
        def work(job):
            state.work_calls+=1
            if work_error=='cancel':raise Cancelled()
            if work_error:raise RuntimeError('PRIVATE-PROVIDER-MESSAGE')
        app=SimpleNamespace(config={'TESTING':True,'WUYU_PIPELINE_FACTORY':True},logger=SimpleNamespace(warning=lambda *args:store.logs.append(args)))
        owner.ports=terminal.TerminalPorts(lambda:app,store.close,store.recover)
        ports=SimpleNamespace(_terminal_owner=lambda:owner,app=lambda:app,_heartbeat=lambda *args:None,
            _check_cancel=lambda job:None,_read=lambda project:(4,json.loads(store.workflow.body)),
            _relayout_validate_action=lambda *args:None,_reading_validate_job=lambda *args,**kwargs:None,
            _research_validate=lambda *args,**kwargs:None,_prepare_relayout=work,
            hook__safe_failure_code=lambda code:'FIXED_ERROR',hook__failure_message=lambda code:'Safe fixed failure',
            hook__failure_diagnostics=lambda value:{},hook__failure_site=lambda exc:'safe-site',hook__json=terminal.encoded)
        design=ModuleType('services.wuyu_design_memory');design.current_design_context=FakeContext('design172')
        env={'db':SimpleNamespace(session=Session()),'WuyuWorkflowJob':object,'current_workflow_actor':FakeContext('actor172',reset_error),
             'json':json,'threading':SimpleNamespace(Event=threading.Event,Thread=Heartbeat),
             'CancelledWork':Cancelled,'PipelineError':Pipeline,'QuotaError':Contract,'WorkflowError':Contract,'WorkflowAPIError':Contract,
             'USER_RETRYABLE_TRACE_CODES':set(),'POLICY_KEY':'unused-policy','page_retry':SimpleNamespace(PAGE_RETRY_SCOPE_KEY='unused-retry')}
        tree=ast.parse((ROOT/'services/wuyu_workflow_jobs.py').read_text(encoding='utf-8'));klass=next(n for n in tree.body if isinstance(n,ast.ClassDef)and n.name=='JobExecution')
        nodes=[n for n in klass.body if isinstance(n,ast.FunctionDef)and n.name in{'_run','_run_execution'}]
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'actual-job-execution172','exec'),env)
        host=SimpleNamespace(ports=ports);host._run_execution=MethodType(env['_run_execution'],host)
        def run():
            with patch.dict(sys.modules,{'services.wuyu_design_memory':design}):env['_run'](host,'job172')
        return owner,store,clock,state,run
    def test_unclaimed_queued_job_never_dispatches_business(self):
        owner,store,clock,state,run=self.setup_execution();owner._records.clear();store.job.status='queued'
        with patch.object(terminal,'persist_terminal',store.persist):run()
        self.assertEqual(state.work_calls,0);self.assertEqual(store.calls,0);self.assertEqual(store.writes,0)
        self.assertEqual(store.job.status,'queued');self.assertFalse(owner.occupied())
    def test_actual_execution_runs_business_once_after_terminal_commit_failure(self):
        owner,store,clock,state,run=self.setup_execution();store.fail_before=True
        with patch.object(terminal,'persist_terminal',store.persist):run();self.assertEqual(owner.snapshot()['pending_terminal'],1);clock[0]=1;owner.retry_due()
        self.assertEqual(state.work_calls,1);self.assertEqual(store.job.status,'completed');self.assertEqual(owner.snapshot()['pending_terminal'],0)
    def test_actual_failure_and_cancel_keep_original_outcome(self):
        for failure,status,code in [('provider','failed','PROVIDER_OPERATION_FAILED'),('cancel','cancelled','CANCELLED_OR_INPUT_CHANGED')]:
            owner,store,clock,state,run=self.setup_execution(work_error=failure);store.fail_before=True
            with patch.object(terminal,'persist_terminal',store.persist):run();outcome=owner._records['job172'].outcome;clock[0]=1;owner.retry_due()
            self.assertEqual(state.work_calls,1);self.assertEqual(store.job.status,status);self.assertEqual(store.job.error_code,code);self.assertEqual(store.job.completed_at,outcome.completed_at);self.assertNotIn('PRIVATE',str(store.logs))
    def test_initial_fetch_failure_keeps_unknown_claim_without_business(self):
        owner,store,clock,state,run=self.setup_execution(fetch_error=True)
        with self.assertRaises(OSError):run()
        self.assertEqual(state.work_calls,0);self.assertEqual(owner.snapshot()['pending_terminal_unknown'],1);self.assertEqual(store.job.status,'running')
    def test_initial_payload_context_failure_keeps_unknown_without_business(self):
        owner,store,clock,state,run=self.setup_execution();store.job.payload='bad-json';owner._records['job172'].payload_digests.add(terminal.payload_digest('bad-json'))
        with self.assertRaises(json.JSONDecodeError):run()
        self.assertEqual(state.work_calls,0);self.assertEqual(owner.snapshot()['pending_terminal_unknown'],1)
    def test_heartbeat_start_failure_does_not_discard_claim(self):
        owner,store,clock,state,run=self.setup_execution(start_error=True)
        with self.assertRaises(OSError):run()
        self.assertEqual(state.work_calls,0);self.assertEqual(owner.snapshot()['pending_terminal_unknown'],1)
    def test_context_reset_failure_still_stops_heartbeat_and_saves_frozen_result(self):
        owner,store,clock,state,run=self.setup_execution(reset_error=True)
        with patch.object(terminal,'persist_terminal',store.persist):
            with self.assertRaises(OSError):run()
        self.assertEqual(state.work_calls,1);self.assertTrue(state.done_event.is_set());self.assertEqual(store.job.status,'completed');self.assertEqual(owner.snapshot()['pending_terminal'],0)
    def test_heartbeat_join_failure_preserves_outcome_for_storage_retry(self):
        owner,store,clock,state,run=self.setup_execution(join_error=True);store.fail_before=True
        with patch.object(terminal,'persist_terminal',store.persist):
            with self.assertRaises(OSError):run()
            self.assertTrue(state.done_event.is_set());self.assertEqual(owner.snapshot()['pending_terminal'],1);self.assertEqual(owner._records['job172'].outcome.status,'completed');clock[0]=1;owner.retry_due()
        self.assertEqual(state.work_calls,1);self.assertEqual(store.job.status,'completed')
    def test_rollback_failure_after_execution_cannot_turn_result_into_provider_error(self):
        owner,store,clock,state,run=self.setup_execution(rollback_error=True);store.fail_before=True
        with patch.object(terminal,'persist_terminal',store.persist):
            with self.assertRaises(OSError):run()
            self.assertEqual(owner._records['job172'].outcome.status,'completed');clock[0]=1;owner.retry_due()
        self.assertEqual(state.work_calls,1);self.assertEqual(store.job.status,'completed');self.assertIsNone(store.job.error_code)


class ServeShutdownTests(unittest.TestCase):
    def serve_function(self, service, owner, clock, heartbeat_rows, release=None):
        stopped=threading.Event();locked=[False];exits=[]
        @contextmanager
        def worker_lock(directory):
            locked[0]=True
            try:yield
            finally:locked[0]=False
        def start():stopped.set()
        service.start_worker=start;service.execution_mode='worker';service.scheduler=SimpleNamespace(is_alive=lambda:True)
        service.shutdown=owner.stop;service.terminal_status=owner.snapshot;service.terminal_owner=owner
        service.future_lock=threading.Lock();service.futures={'job172':SimpleNamespace(done=lambda:True)}
        app=SimpleNamespace(extensions={'wuyu_workflow':service},app_context=lambda:nullcontext())
        tick=[0]
        def monotonic():tick[0]+=1;return 0 if tick[0]==1 else 31
        def sleep(_):
            self.assertTrue(locked[0]);self.assertFalse(exits)
            if release:release()
        def exit_now(code):
            self.assertTrue(locked[0]);exits.append(code);raise SystemExit(code)
        env={'threading':SimpleNamespace(Event=lambda:stopped),'signal':SimpleNamespace(SIGTERM=15,SIGINT=2,signal=lambda *a:None),
             'os':SimpleNamespace(environ={'WUYU_WORKFLOW_EXECUTION_MODE':'worker'},_exit=exit_now),
             'time':SimpleNamespace(monotonic=monotonic,sleep=sleep),'execution_mode':lambda _: 'worker',
             'state_directory':lambda:Path('inert-no-files'), 'worker_owner':worker_lock,
             'write_heartbeat':lambda status,directory,**values:heartbeat_rows.append({'status':status,**values}),
             'write_capacity_snapshot':lambda *args:None}
        tree=ast.parse((ROOT/'workflow_worker.py').read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name in{'terminal_count','terminal_attempt_count','active_jobs','serve'}]
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'actual-worker-serve172','exec'),env)
        return lambda:env['serve'](lambda:app,drain_seconds=30),exits
    def test_real_serve_deadline_does_not_kill_started_terminal_attempt(self):
        owner,store,clock=owner_fixture();entered=threading.Event();released=threading.Event();rows=[]
        def slow(record,ports):entered.set();released.wait(3);store.persist(record,ports)
        with patch.object(terminal,'persist_terminal',slow):
            thread=threading.Thread(target=lambda:owner.attempt('job172'));thread.start();self.assertTrue(entered.wait(1))
            def release():released.set();thread.join(2);self.assertFalse(thread.is_alive())
            service=SimpleNamespace();run,exits=self.serve_function(service,owner,clock,rows,release)
            run()
        self.assertFalse(exits);self.assertEqual(store.job.status,'completed');self.assertTrue(any(row['status']=='stopping'and row['active_terminal_attempts']==1 for row in rows));self.assertEqual(rows[-1]['pending_terminal'],0)
    def test_real_serve_unresolved_pending_is_never_reported_zero(self):
        owner,store,clock=owner_fixture();owner._records['job172'].blocked=True;rows=[]
        run,exits=self.serve_function(SimpleNamespace(),owner,clock,rows)
        with self.assertRaises(SystemExit):run()
        self.assertEqual(exits,[0]);self.assertEqual(rows[-1]['status'],'stopped');self.assertEqual(rows[-1]['pending_terminal'],1);self.assertIn('job172',owner.occupied())


class PostgreSQLTerminalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn=os.environ.get('WUYU_TERMINAL_TEST_DATABASE_URL','');parsed=urlparse(dsn)
        if parsed.hostname not in {'127.0.0.1','localhost'} or parsed.path!='/terminal172_fixture' or not parsed.scheme.startswith('postgresql'):
            raise RuntimeError('An explicit loopback terminal172_fixture PostgreSQL DSN is required; no live fallback')
        from flask import Flask
        from models import db,Project,Task,User
        from models.wuyu_workflow import WuyuWorkflow,WuyuWorkflowJob
        cls.db,cls.Project,cls.Task,cls.User=db,Project,Task,User;cls.Workflow,cls.Job=WuyuWorkflow,WuyuWorkflowJob
        cls.app=Flask('terminal172-isolated-PG')
        cls.app.config.update(SQLALCHEMY_DATABASE_URI=dsn,SQLALCHEMY_ENGINE_OPTIONS={'connect_args':{},'pool_size':2,'max_overflow':0},TESTING=True,WUYU_QUOTA_ENABLED=False)
        db.init_app(cls.app);cls.context=cls.app.app_context();cls.context.push()
        from sqlalchemy import text
        if db.engine.dialect.name!='postgresql'or db.session.execute(text('select current_database()')).scalar_one()!='terminal172_fixture':raise RuntimeError('Wrong isolated database')
        db.session.rollback();db.create_all()
    @classmethod
    def tearDownClass(cls):
        cls.db.session.remove();cls.context.pop()
    def setUp(self):
        self.ident=__import__('uuid').uuid4().hex;db=self.db
        user=self.User(id='u'+self.ident,username='fixture-'+self.ident,password_hash='inert-not-login');db.session.add(user);db.session.flush()
        project=self.Project(id='p'+self.ident,user_id=user.id,project_title='Synthetic terminal172');db.session.add(project);db.session.flush()
        task=self.Task(id='t'+self.ident,project_id=project.id,task_type='WUYU_WORKFLOW',status='PROCESSING',progress='{}');db.session.add(task);db.session.flush()
        job=self.Job(id='j'+self.ident,project_id=project.id,task_id=task.id,actor_user_id=user.id,action='_prepare_relayout',input_epoch=7,status='running',worker_pid=412,started_at=FINISHED-timedelta(seconds=30),payload='{}',progress='{"quota_receipts":{}}');db.session.add(job);db.session.flush()
        body={'epoch':7,'paused':False,'relayout':{'status':'preparing'},'workflow':{'events':[]}}
        workflow=self.Workflow(project_id=project.id,revision=4,body=terminal.encoded(body),active_job_id=job.id);db.session.add(workflow);db.session.commit()
        self.job_id,self.project_id,self.task_id=job.id,project.id,task.id;self.clock=[0.0];self.cleanup_calls=0;self.fee_calls=0
        def close(body,message):self.cleanup_calls+=1;body['workflow']['events'].append({'id':'fixed172-'+str(self.cleanup_calls),'message':message})
        def fees(app):self.fee_calls+=1
        self.owner=terminal.TerminalPersistence(terminal.TerminalPorts(lambda:self.app,close,fees),clock=lambda:self.clock[0]);self.owner.register_claim(job);self.owner.begin_execution(job.id);self.owner.bind_execution(job)
        db.session.rollback()  # A different thread's row-lock test needs the second pooled connection.
    def tearDown(self):
        self.db.session.remove()
    def freeze(self,status='failed'):
        self.owner.freeze_outcome(self.job_id,status,None if status=='completed'else'FIXED_FAILURE',None if status=='completed'else{'code':'FIXED_FAILURE'},completed_at=FINISHED)
    def values(self):
        self.db.session.rollback();self.db.session.expire_all();job=self.db.session.get(self.Job,self.job_id);task=self.db.session.get(self.Task,self.task_id);workflow=self.db.session.get(self.Workflow,self.project_id)
        return job_fields_copy(job),task_fields_copy(task),dict(body=workflow.body,revision=workflow.revision,active_job_id=workflow.active_job_id)
    def test_atomic_normal_completed_failed_cancelled(self):
        self.freeze();self.assertTrue(self.owner.attempt(self.job_id));job,task,wf=self.values();self.assertEqual(job['status'],'failed');self.assertEqual(task['status'],'FAILED');self.assertIsNone(wf['active_job_id']);self.assertEqual(wf['revision'],5)
    def test_real_rollback_before_commit_preserves_all_three_rows(self):
        self.freeze();before=self.values();session=self.db.session();real=session.commit
        with patch.object(session,'commit',side_effect=OSError('injected terminal precommit')):self.assertFalse(self.owner.attempt(self.job_id))
        self.assertEqual(self.values(),before);self.clock[0]=1;self.assertTrue(self.owner.attempt(self.job_id));self.assertEqual(self.cleanup_calls,1)
    def test_real_commit_then_ack_unknown_is_exact_ack_without_revision_change(self):
        self.freeze();session=self.db.session();real=session.commit
        def commit_unknown():real();raise OSError('injected committed receipt loss')
        with patch.object(session,'commit',side_effect=commit_unknown):self.assertFalse(self.owner.attempt(self.job_id))
        after=self.values();self.clock[0]=1;self.assertTrue(self.owner.attempt(self.job_id));self.assertEqual(self.values(),after);self.assertEqual(self.cleanup_calls,1)
    def test_actual_nth_statement_rollback_cannot_publish_partial_terminal(self):
        from sqlalchemy import event
        self.freeze();before=self.values();engine=self.db.engine;counter=[0]
        def reject(conn,cursor,statement,params,ctx,many):
            if statement.lstrip().upper().startswith('UPDATE'):
                counter[0]+=1
                if counter[0]==2:raise OSError('injected second UPDATE')
        event.listen(engine,'before_cursor_execute',reject)
        try:self.assertFalse(self.owner.attempt(self.job_id))
        finally:event.remove(engine,'before_cursor_execute',reject)
        self.assertGreaterEqual(counter[0],2);self.assertEqual(self.values(),before);self.clock[0]=1;self.assertTrue(self.owner.attempt(self.job_id));self.assertEqual(self.cleanup_calls,1)
    def test_ack_unknown_then_legitimate_new_project_body_is_preserved(self):
        self.freeze();session=self.db.session();real=session.commit
        def lost():real();raise OSError('injected committed receipt loss')
        with patch.object(session,'commit',side_effect=lost):self.assertFalse(self.owner.attempt(self.job_id))
        wf=self.db.session.get(self.Workflow,self.project_id);wf.body='{"epoch":8,"human":"keep"}';wf.revision=19;wf.active_job_id='synthetic-successor';self.db.session.commit();after=self.values();self.clock[0]=1
        self.assertTrue(self.owner.attempt(self.job_id));self.assertEqual(self.values(),after)
    def test_new_owner_before_terminal_save_is_not_cleared(self):
        self.freeze();wf=self.db.session.get(self.Workflow,self.project_id);wf.active_job_id='synthetic-successor';self.db.session.commit()
        self.assertFalse(self.owner.attempt(self.job_id));self.assertEqual(self.values()[2]['active_job_id'],'synthetic-successor');self.assertEqual(self.owner.snapshot()['pending_terminal'],1)
    def test_completed_and_cancelled_real_transactions_keep_task_mapping(self):
        self.freeze('cancelled');self.assertTrue(self.owner.attempt(self.job_id));job,task,wf=self.values();self.assertEqual(job['status'],'cancelled');self.assertEqual(task['status'],'FAILED');self.assertIsNone(wf['active_job_id'])
    def test_postgresql_transaction_timeout_is_local_and_never_loosened(self):
        from sqlalchemy import text
        self.db.session.execute(text("SET LOCAL lock_timeout='125ms'"));self.db.session.execute(text("SET LOCAL statement_timeout='700ms'"))
        terminal.configure_terminal_transaction(self.db)
        self.assertEqual(self.db.session.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one(),'125')
        self.assertEqual(self.db.session.execute(text("SELECT setting FROM pg_settings WHERE name='statement_timeout'")).scalar_one(),'700')
        self.db.session.rollback()
    def test_workflow_row_lock_times_out_keeps_owner_then_saves_after_release(self):
        from sqlalchemy import text
        self.freeze();engine=self.db.engine
        blocker=engine.connect();txn=blocker.begin();blocker.execute(text('SELECT project_id FROM wuyu_workflows WHERE project_id=:p FOR UPDATE'),{'p':self.project_id})
        started=time.monotonic()
        try:self.assertFalse(self.owner.attempt(self.job_id))
        finally:txn.rollback();blocker.close()
        self.assertGreater(time.monotonic()-started,.5);self.assertLess(time.monotonic()-started,8)
        self.assertEqual(self.values()[0]['status'],'running');self.assertEqual(self.owner.snapshot()['pending_terminal'],1)
        self.clock[0]=1;self.assertTrue(self.owner.attempt(self.job_id));self.assertEqual(self.values()[0]['status'],'failed')
    def test_real_claim_commit_unknown_keeps_unknown_owner_without_submission(self):
        from services.wuyu_workflow_service import WuyuWorkflowService
        self.app.config.update(WUYU_WORKFLOW_WORKERS=20,WUYU_WORKFLOW_PER_ACTOR=1,WUYU_WORKFLOW_EXECUTION_MODE='combined')
        service=WuyuWorkflowService(self.app);job=self.db.session.get(self.Job,self.job_id);job.status='queued';job.started_at=None;job.worker_pid=None;wf=self.db.session.get(self.Workflow,self.project_id);wf.active_job_id=None;self.db.session.commit()
        session=self.db.session();real=session.commit
        def unknown():real();raise OSError('injected claim committed receipt loss')
        try:
            with patch('services.wuyu_workflow_lifecycle.memory_admission',return_value={'allowed':True}),patch.object(session,'commit',side_effect=unknown):
                with self.assertRaises(OSError):service._claim(self.job_id)
            self.assertEqual(service.terminal_status()['pending_terminal_unknown'],1);self.assertEqual(len(service.futures),0)
            self.assertEqual(self.values()[0]['status'],'running');self.assertFalse(service._claim(self.job_id))
        finally:service.executor.shutdown(wait=True)
    def test_actual_serve_waits_for_started_postgresql_attempt_past_provider_deadline(self):
        from sqlalchemy import event,text
        self.freeze();entered=threading.Event();errors=[];engine=self.db.engine
        blocker=engine.connect();tx=blocker.begin();blocker.execute(text('SELECT project_id FROM wuyu_workflows WHERE project_id=:p FOR UPDATE'),{'p':self.project_id})
        def observed(conn,cursor,statement,params,context,many):
            if 'wuyu_workflows' in statement and 'FOR UPDATE' in statement:entered.set()
        event.listen(engine,'before_cursor_execute',observed)
        def save():
            try:
                with self.app.app_context():self.owner.attempt(self.job_id)
            except BaseException as e:errors.append(type(e).__name__)
        thread=threading.Thread(target=save);thread.start();self.assertTrue(entered.wait(1));rows=[]
        try:
            def release():tx.rollback();blocker.close();thread.join(3);self.assertFalse(thread.is_alive())
            run,exits=ServeShutdownTests.serve_function(self,SimpleNamespace(),self.owner,self.clock,rows,release)
            run();self.assertFalse(exits)
        finally:
            if blocker.closed is False:tx.rollback();blocker.close()
            thread.join(3);event.remove(engine,'before_cursor_execute',observed)
        self.assertFalse(errors);self.assertEqual(self.values()[0]['status'],'failed');self.assertTrue(any(v['status']=='stopping'and v['active_terminal_attempts']==1 for v in rows))
    def run_real_fee_ack_unknown(self,refund):
        from services.wuyu_quota import get_quota_service
        from services.wuyu_workflow_quota import recover_workflow_charges
        from services.wuyu_workflow_service import WuyuWorkflowService
        from services.wuyu_workflow_contracts import WorkflowAPIError
        from models.wuyu_quota import QuotaReservation,QuotaLedger,QuotaAccount,QuotaDay
        self.app.config.update(WUYU_QUOTA_ENABLED=True,WUYU_PIPELINE_FACTORY=True,WUYU_WORKFLOW_EXECUTION_MODE='combined')
        service=WuyuWorkflowService(self.app);business=[0];fees=[0]
        service._check_cancel=lambda *a:None;service._relayout_validate_action=lambda *a:None;service._reading_validate_job=lambda *a,**k:None;service._research_validate=lambda *a,**k:None
        service._close_inflight=lambda body,message:body['workflow']['events'].append({'id':'fixture-terminal172','message':message})
        def work(job):
            business[0]+=1
            if refund:raise WorkflowAPIError('Synthetic failure, no model')
        service._prepare_relayout=work
        job=self.db.session.get(self.Job,self.job_id);actor=job.actor_user_id;self.db.session.rollback()
        quota=get_quota_service(self.app);hold=quota.reserve(actor,'terminal172:'+self.ident,'text',input_tokens=0,output_tokens=0,points_upper=20,project_id=self.project_id,job_id=self.job_id)
        job=self.db.session.get(self.Job,self.job_id);job.progress=terminal.encoded({'quota_receipts':{} if refund else{hold['id']:{'actual_points':13}}});self.db.session.commit()
        owner=service.terminal_owner;owner.clock=lambda:self.clock[0];owner.register_claim(job)
        def recovery(app):fees[0]+=1;recover_workflow_charges(app)
        owner.ports=terminal.TerminalPorts(lambda:self.app,lambda body,message:service._close_inflight(body,message),recovery)
        method='refund'if refund else'settle';real=getattr(quota,method);finishes=[0]
        def committed_unknown(*args,**kwargs):
            value=real(*args,**kwargs);finishes[0]+=1
            if finishes[0]==1:raise OSError('injected quota commit ACK loss')
            return value
        try:
            with patch.object(quota,method,side_effect=committed_unknown),patch.object(terminal,'persist_terminal',wraps=terminal.persist_terminal)as writes:
                service._run(self.job_id);self.assertEqual(owner.snapshot()['pending_terminal'],1);after=self.values();self.assertTrue(owner._records[self.job_id].terminal_ack)
                def balances():
                    self.db.session.rollback();account=QuotaAccount.query.filter_by(user_id=actor).one();day=QuotaDay.query.filter_by(account_id=account.id).one()
                    return tuple(getattr(row,name)for row in[account,day]for name in['points_available','points_reserved','points_spent'])
                charged_balances=balances()
                self.clock[0]=1;self.assertTrue(owner.attempt(self.job_id));self.assertEqual(self.values(),after);self.assertEqual(writes.call_count,1);self.assertEqual(balances(),charged_balances)
            self.assertEqual(business,[1]);self.assertEqual(fees,[2]);self.assertEqual(finishes,[1])
            reservation=self.db.session.get(QuotaReservation,hold['id']);self.assertEqual(reservation.status,'refunded'if refund else'settled');self.assertEqual(reservation.actual_points,0 if refund else 13)
            ledger=QuotaLedger.query.filter_by(reservation_id=hold['id'],event=method).all();self.assertEqual(len(ledger),1)
            self.assertEqual(json.loads(self.db.session.get(self.Job,self.job_id).progress)['quota_receipts'],{} if refund else{hold['id']:{'actual_points':13}})
        finally:service.executor.shutdown(wait=True);self.app.config['WUYU_QUOTA_ENABLED']=False
    def test_real_settle_commit_ack_unknown_charges_once_with_one_business_step(self):self.run_real_fee_ack_unknown(False)
    def test_real_refund_commit_ack_unknown_refunds_once_with_one_business_step(self):self.run_real_fee_ack_unknown(True)
    def test_real_payload_change_is_permanent_conflict_without_terminal_write(self):
        self.freeze();job=self.db.session.get(self.Job,self.job_id);job.payload='{"different":"claim"}';self.db.session.commit();before=self.values()
        self.assertFalse(self.owner.attempt(self.job_id));self.assertEqual(self.values(),before);self.assertEqual(self.owner.snapshot()['pending_terminal_blocked'],1)
    def test_real_foreign_task_terminal_is_not_overwritten(self):
        self.freeze();task=self.db.session.get(self.Task,self.task_id);task.status='FAILED';task.error_message='OTHER_OUTCOME';task.completed_at=FINISHED-timedelta(days=1);self.db.session.commit();before=self.values()
        self.assertFalse(self.owner.attempt(self.job_id));self.assertEqual(self.values(),before);self.assertEqual(self.owner.snapshot()['pending_terminal'],1)
    def test_real_matching_partial_terminal_only_fills_missing_components(self):
        self.freeze();record=self.owner._records[self.job_id];job=self.db.session.get(self.Job,self.job_id);task=self.db.session.get(self.Task,self.task_id);wf=self.db.session.get(self.Workflow,self.project_id)
        record.target=terminal.prepare_target(record,job,task,wf,self.owner.ports)
        for key,value in record.target.job_after.items():setattr(job,key,value)
        self.db.session.commit();job_before=self.values()[0]
        self.assertTrue(self.owner.attempt(self.job_id));after=self.values();self.assertEqual(after[0],job_before);self.assertEqual(after[1]['status'],'FAILED');self.assertEqual(after[2]['revision'],5);self.assertEqual(self.cleanup_calls,1)

def job_fields_copy(job):return deepcopy(terminal.job_fields(job))
def task_fields_copy(task):return deepcopy(terminal.task_fields(task))

if __name__=='__main__':unittest.main()
