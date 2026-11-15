"""Offline admission tests against the candidate's real helpers and lifecycle AST.

All inputs are synthetic; no application startup, production ORM or model client.
"""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace, ModuleType
from unittest.mock import patch
import ast
import binascii
import importlib.util
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib

from services import wuyu_resource_limits as resource
ROOT=Path(__file__).resolve().parent
SERVICES=Path(resource.__file__).resolve().parent
MiB=1024**2


def await_condition(predicate, timeout=2):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():return
        time.sleep(.005)
    raise AssertionError('Synthetic concurrency condition did not occur')


def png(path,width=8,height=8,extra=0):
    def chunk(kind,body):return struct.pack('>I',len(body))+kind+body+struct.pack('>I',binascii.crc32(kind+body)&0xffffffff)
    codec=zlib.compressobj(6);row=b'\0'+b'\x10\x20\x30\xff'*width
    encoded=[codec.compress(row) for _ in range(height)];encoded.append(codec.flush())
    value=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6,0,0,0))+chunk(b'IDAT',b''.join(encoded))+chunk(b'IEND',b'')+b'x'*extra
    path.write_bytes(value);return path


class ThreadTests(unittest.TestCase):
    def setUp(self):self.threads=[];self.errors=[];self.releases=[]
    def tearDown(self):
        for event in self.releases:event.set()
        for thread in self.threads:thread.join(timeout=3)
        self.assertFalse(any(thread.is_alive() for thread in self.threads),'Synthetic thread lease leaked')
        self.assertEqual(self.errors,[])
    def launch(self,call):
        def run():
            try:call()
            except BaseException as error:self.errors.append(error)
        thread=threading.Thread(target=run,daemon=True);self.threads.append(thread);thread.start();return thread
    def release(self):
        event=threading.Event();self.releases.append(event);return event


class ByteBudgetTests(ThreadTests):
    def test_FIFO_blocks_small_follower_until_earlier_large_lease_can_enter(self):
        budget=resource.ByteBudget(10);release=self.release();order=[]
        def lease(weight,label):
            with budget.lease(weight):order.append(label);release.wait(3)
        with budget.lease(8):
            self.launch(lambda:lease(4,'first'));await_condition(lambda:len(budget._queue)==1)
            self.launch(lambda:lease(1,'second'));await_condition(lambda:len(budget._queue)==2)
            self.assertEqual(order,[]);self.assertEqual(budget.used,8)
        await_condition(lambda:len(order)==2)
        self.assertEqual(order,['first','second']);self.assertEqual(budget.used,5)
        release.set()

    def test_cancelled_head_leaves_FIFO_and_follower_uses_remaining_bytes(self):
        budget=resource.ByteBudget(10);cancel=threading.Event();release=self.release();cancelled=threading.Event();entered=threading.Event()
        class Cancelled(Exception):pass
        def check():
            if cancel.is_set():raise Cancelled()
        def head():
            try:
                with budget.lease(4,check):self.fail('Cancelled head must not acquire')
            except Cancelled:cancelled.set()
        def follower():
            with budget.lease(1):entered.set();release.wait(3)
        with budget.lease(8):
            self.launch(head);await_condition(lambda:len(budget._queue)==1)
            self.launch(follower);await_condition(lambda:len(budget._queue)==2)
            cancel.set();self.assertTrue(cancelled.wait(2));self.assertTrue(entered.wait(2))
            self.assertEqual(budget.used,9);self.assertEqual(len(budget._queue),0)
        release.set()

    def test_exception_inside_lease_releases_exact_weight(self):
        budget=resource.ByteBudget(12)
        with self.assertRaisesRegex(RuntimeError,'synthetic'):
            with budget.lease(7):
                self.assertEqual(budget.used,7);raise RuntimeError('synthetic')
        self.assertEqual(budget.used,0);self.assertEqual(len(budget._queue),0)
        with budget.lease(12):self.assertEqual(budget.used,12)
        self.assertEqual(budget.used,0)

    def test_oversized_operation_is_rejected_before_body_and_does_not_leak(self):
        budget=resource.ByteBudget(10);invoked=[]
        with self.assertRaises(resource.PipelineError) as caught:
            with budget.lease(11):invoked.append('must-not-run')
        self.assertEqual(caught.exception.code,'capacity_memory_limit')
        self.assertEqual(invoked,[]);self.assertEqual(budget.used,0);self.assertEqual(len(budget._queue),0)

    def test_preacquisition_cancel_does_not_change_used_or_queue(self):
        budget=resource.ByteBudget(10)
        def cancel():raise RuntimeError('synthetic cancel')
        with self.assertRaises(RuntimeError):
            with budget.lease(3,cancel):self.fail('must not acquire')
        self.assertEqual((budget.used,len(budget._queue)),(0,0))


class PageWeightTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='wuyu-resource151-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.upload=self.root/'upload';self.upload.mkdir()

    def test_reference_paths_are_deduplicated_and_only_largest_nine_count(self):
        paths=[png(self.upload/f'input-{i}.png',extra=i*101) for i in range(11)]
        plan={'recipe':{'user_template_refs':[{'path':str(p)} for p in paths]},'spec':{'previous_image_path':str(paths[-1])}}
        expected=384*MiB+sum(sorted([6*p.stat().st_size+2*8*8*4 for p in paths],reverse=True)[:9])
        self.assertEqual(resource.page_weight(plan,self.upload),expected)

    def test_outside_relative_missing_and_nonimage_paths_do_not_read_files(self):
        outside=png(self.root/'outside.png');inside=png(self.upload/'valid.png')
        plan={'source':str(outside),'spec':{'image_path':'valid.png','preview_path':str(self.upload/'missing.png')},'assets':[{'path':str(inside)},{'path':str(self.upload/'ignored.pdf')}]}
        self.assertEqual(resource.page_weight(plan,self.upload),384*MiB+6*inside.stat().st_size+2*8*8*4)

    def test_small_compressed_40MP_inputs_reserve_expanded_pixel_memory(self):
        source=png(self.upload/'fortyMP-0.png',10000,4000)
        paths=[source]
        for i in range(1,9):
            path=self.upload/f'fortyMP-{i}.png';shutil.copyfile(source,path);paths.append(path)
        self.assertLess(source.stat().st_size,MiB)
        plan={'assets':[{'path':str(p)} for p in paths]}
        expected=384*MiB+sum(6*p.stat().st_size+2*40_000_000*4 for p in paths)
        self.assertEqual(resource.page_weight(plan,self.upload),expected)
        app=SimpleNamespace(config={'UPLOAD_FOLDER':str(self.upload),'WUYU_PAGE_RESOURCE_BYTES':2*1024**3})
        with self.assertRaises(resource.PipelineError) as caught:
            with resource.page_slot(app,plan,lambda:None):self.fail('Known oversized job must not dispatch')
        self.assertEqual(caught.exception.code,'capacity_memory_limit');self.assertEqual(resource.resources(app).budget.used,0)

    def test_invalid_image_reserves_maximum_pixels_for_original_pipeline_to_classify(self):
        image=self.upload/'invalid.png';image.write_bytes(b'not an image')
        self.assertEqual(resource.page_weight({'source':str(image)},self.upload),384*MiB+6*image.stat().st_size+2*40_000_000*4)

    def test_selected_controlled_catalog_image_outside_upload_is_included(self):
        import json
        library=self.root/'library';library.mkdir();image=png(library/'cover.png',32,16)
        unselected=png(library/'unselected.png',64,32)
        catalog=library/'catalog.json'
        catalog.write_text(json.dumps({'sources':[{'id':'synthetic-source','cover':'cover.png','pages':[]}]}),encoding='utf-8')
        plan={'recipe':{'style_anchor':'synthetic-source','references':[]},'spec':{},'assets':[{'path':str(unselected)}]}
        self.assertFalse(image.is_relative_to(self.upload))
        expected=384*MiB+6*image.stat().st_size+2*32*16*4
        self.assertEqual(resource.page_weight(plan,self.upload,{'WUYU_TEMPLATE_LIBRARY_PATH':str(catalog)}),expected)

    def test_extreme_pixel_header_keeps_invalid_input_for_original_pipeline(self):
        def chunk(kind,body):return struct.pack('>I',len(body))+kind+body+struct.pack('>I',binascii.crc32(kind+body)&0xffffffff)
        image=self.upload/'extreme-header.png'
        header=chunk(b'IHDR',struct.pack('>IIBBBBB',20000,10000,8,6,0,0,0))
        image.write_bytes(b'\x89PNG\r\n\x1a\n'+header+chunk(b'IDAT',zlib.compress(b'\0'))+chunk(b'IEND',b''))
        # Only the parser header is examined: never allocate the claimed 800MB image.
        expected=384*MiB+6*image.stat().st_size+2*40_000_000*4
        self.assertEqual(resource.page_weight({'source':str(image)},self.upload),expected)

    def test_disabled_page_budget_preserves_original_input_validation(self):
        app=SimpleNamespace(config={})
        with patch.object(resource,'page_weight',side_effect=AssertionError('must not inspect')):
            with resource.page_slot(app,{'unknown':'original inputs'},lambda:None):pass
        self.assertIsNone(resource.resources(app).budget)


class ExportResourcesTests(ThreadTests):
    def test_exports_two_and_pages_share_one_byte_budget(self):
        app=SimpleNamespace(config={'WUYU_PAGE_RESOURCE_BYTES':2*1024**3,'WUYU_LOCAL_EXPORT_WORKERS':2})
        limits=resource.resources(app);release=self.release();entered=[];lock=threading.Lock()
        def export(number):
            with resource.export_slot(app,lambda:None):
                with lock:entered.append(number)
                release.wait(3)
        self.launch(lambda:export(1));self.launch(lambda:export(2));await_condition(lambda:len(entered)==2)
        self.launch(lambda:export(3));time.sleep(.06)
        self.assertEqual(len(entered),2);self.assertEqual(limits.budget.used,1536*MiB)
        with resource.page_slot(app,{},lambda:None):self.assertEqual(limits.budget.used,1920*MiB)
        self.assertEqual(limits.budget.used,1536*MiB);release.set();await_condition(lambda:len(entered)==3)
        self.assertLessEqual(limits.budget.peak,limits.budget.capacity)

    def test_export_waits_for_page_bytes_even_with_free_export_token(self):
        app=SimpleNamespace(config={'WUYU_PAGE_RESOURCE_BYTES':1024*MiB,'WUYU_LOCAL_EXPORT_WORKERS':2})
        limits=resource.resources(app);entered=threading.Event();release=self.release()
        def export():
            with resource.export_slot(app,lambda:None):entered.set();release.wait(3)
        with resource.page_slot(app,{},lambda:None):
            self.launch(export);await_condition(lambda:len(limits.budget._queue)==1)
            self.assertFalse(entered.is_set());self.assertEqual(limits.budget.used,384*MiB)
        self.assertTrue(entered.wait(2));self.assertEqual(limits.budget.used,768*MiB);release.set()

    def test_export_prework_cancel_releases_semaphore(self):
        app=SimpleNamespace(config={'WUYU_LOCAL_EXPORT_WORKERS':2})
        def cancel():raise RuntimeError('synthetic cancellation')
        with self.assertRaises(RuntimeError):
            with resource.export_slot(app,cancel):self.fail('must not dispatch')
        self.assertEqual(resource.resources(app).exports._value,2)

    def test_resources_are_singleton_per_app_and_isolated_between_apps(self):
        first=SimpleNamespace(config={'WUYU_PAGE_RESOURCE_BYTES':1024,'WUYU_LOCAL_EXPORT_WORKERS':2})
        second=SimpleNamespace(config={'WUYU_PAGE_RESOURCE_BYTES':2048,'WUYU_LOCAL_EXPORT_WORKERS':1})
        self.assertIs(resource.resources(first),resource.resources(first))
        self.assertIsNot(resource.resources(first),resource.resources(second))
        with resource.resources(first).budget.lease(100):self.assertEqual(resource.resources(second).budget.used,0)


class MemoryAdmissionTests(unittest.TestCase):
    def test_disabled_admission_does_not_read_cgroup(self):
        with patch.object(resource,'cgroup_memory_current',side_effect=AssertionError('disabled')):
            self.assertEqual(resource.memory_admission({}),{'allowed':True,'reason':'disabled'})

    def test_exact_high_plus_reserve_boundary_and_one_byte_over(self):
        config={'WUYU_WORKFLOW_MEMORY_HIGH_BYTES':3000,'WUYU_WORKFLOW_MEMORY_RESERVE_BYTES':400}
        with patch.object(resource,'cgroup_memory_current',return_value=2600):self.assertTrue(resource.memory_admission(config)['allowed'])
        with patch.object(resource,'cgroup_memory_current',return_value=2601):
            value=resource.memory_admission(config);self.assertFalse(value['allowed']);self.assertEqual(value['reason'],'memory_wait')

    def test_missing_or_bad_cgroup_rejects_new_work_without_exception_text(self):
        for error in [OSError('synthetic-sensitive-path'),ValueError('synthetic-sensitive-value'),StopIteration()]:
            with patch.object(resource,'cgroup_memory_current',side_effect=error):
                self.assertEqual(resource.memory_admission({'WUYU_WORKFLOW_MEMORY_HIGH_BYTES':3000}),{'allowed':False,'reason':'memory_state_unknown'})


def lifecycle_functions():
    tree=ast.parse((SERVICES/'wuyu_workflow_lifecycle.py').read_text(encoding='utf-8'))
    wanted={'initialize_resources','_claim','_fair_queue','_schedule'}
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in wanted]
    assert len(nodes)==len(wanted)
    ns={'threading':threading,'memory_admission':resource.memory_admission}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'actual_lifecycle_AST','exec'),ns);return ns


class LifecycleWiringTests(unittest.TestCase):
    def test_actual_Config_reads_deployment_worker_limits(self):
        tree=ast.parse((ROOT/'config.py').read_text(encoding='utf-8'))
        config=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Config')
        fields={'WUYU_WORKFLOW_WORKERS':'20','WUYU_WORKFLOW_PER_ACTOR':'1','WUYU_WORKFLOW_MEMORY_HIGH_BYTES':'3000000000','WUYU_WORKFLOW_MEMORY_RESERVE_BYTES':'400000000','WUYU_PAGE_RESOURCE_BYTES':str(2*1024**3),'WUYU_LOCAL_EXPORT_WORKERS':'2'}
        nodes=[n for n in config.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in fields for t in n.targets)]
        self.assertEqual(len(nodes),len(fields));ns={'os':os}
        with patch.dict(os.environ,fields):exec(compile(ast.Module(body=nodes,type_ignores=[]),'actual_Config_fields_AST','exec'),ns)
        for name,value in fields.items():self.assertEqual(ns[name],int(value))

    def test_initialize_resources_wires_actual_executor_twenty_and_api_has_none(self):
        ns=lifecycle_functions();calls=[]
        def factory(**kwargs):calls.append(kwargs);return SimpleNamespace(_max_workers=kwargs['max_workers'])
        mode='worker';owner=SimpleNamespace(_lifecycle_dependencies=lambda:SimpleNamespace(execution_mode=lambda config:mode,executor_factory=factory))
        app=SimpleNamespace(config={'WUYU_WORKFLOW_WORKERS':20,'WUYU_WORKFLOW_PER_ACTOR':1})
        ns['initialize_resources'](owner,app);self.assertEqual(owner.executor._max_workers,20);self.assertEqual(calls[0]['max_workers'],20)
        mode='api';ns['initialize_resources'](owner,app);self.assertIsNone(owner.executor);self.assertEqual(len(calls),1)

    def test_invalid_worker_or_actor_limits_fail_before_executor(self):
        ns=lifecycle_functions();owner=SimpleNamespace(_lifecycle_dependencies=lambda:self.fail('must validate before dependencies'))
        for config in [{'WUYU_WORKFLOW_WORKERS':0},{'WUYU_WORKFLOW_WORKERS':201},{'WUYU_WORKFLOW_PER_ACTOR':-1},{'WUYU_WORKFLOW_PER_ACTOR':201}]:
            with self.assertRaises(ValueError):ns['initialize_resources'](owner,SimpleNamespace(config=config))

    def test_actor_hard_limit_prevents_claim_without_project_mutation(self):
        ns=lifecycle_functions();job=SimpleNamespace(status='queued',actor_user_id='synthetic-A',project_id='synthetic-project')
        filters=[]
        class Query:
            def filter_by(self,**kwargs):filters.append(kwargs);return self
            def count(self):return 1
        job_model=SimpleNamespace(query=Query());ns['WuyuWorkflowJob']=job_model
        ns['db']=SimpleNamespace(session=SimpleNamespace(expire_all=lambda:None,get=lambda model,ident:job))
        owner=SimpleNamespace(execution_mode='worker',app=SimpleNamespace(config={'WUYU_WORKFLOW_PER_ACTOR':1}),_ensure=lambda project:self.fail('actor-full must not touch project'))
        self.assertFalse(ns['_claim'](owner,'synthetic-job'))
        self.assertEqual(filters,[{'status':'running','actor_user_id':'synthetic-A'}])

    def test_unknown_memory_prevents_claim_before_actor_or_project(self):
        ns=lifecycle_functions();job=SimpleNamespace(status='queued');ns['WuyuWorkflowJob']=object()
        ns['db']=SimpleNamespace(session=SimpleNamespace(expire_all=lambda:None,get=lambda model,ident:job))
        owner=SimpleNamespace(execution_mode='worker',app=SimpleNamespace(config={'WUYU_WORKFLOW_MEMORY_HIGH_BYTES':3000}))
        with patch.object(resource,'cgroup_memory_current',side_effect=OSError('synthetic')):self.assertFalse(ns['_claim'](owner,'synthetic-job'))

    def test_scheduler_dispatches_at_most_twenty_then_preserves_other_queue(self):
        ns=lifecycle_functions();waits=iter([False,True]);submitted=[];jobs=[SimpleNamespace(id=i) for i in range(25)]
        owner=SimpleNamespace(stop_event=SimpleNamespace(wait=lambda duration:next(waits)),app=SimpleNamespace(config={},app_context=lambda:nullcontext()),future_lock=threading.Lock(),futures={},_fair_queue=lambda:jobs,_claim=lambda ident:True,_run_context=lambda ident:None)
        owner.executor=SimpleNamespace(_max_workers=20,submit=lambda fn,ident:submitted.append(ident) or SimpleNamespace(done=lambda:False))
        ns['_schedule'](owner);self.assertEqual(submitted,list(range(20)));self.assertEqual(len(owner.futures),20)

    def test_scheduler_memory_unknown_does_not_dispatch_or_claim(self):
        ns=lifecycle_functions();waits=iter([False,True])
        owner=SimpleNamespace(stop_event=SimpleNamespace(wait=lambda duration:next(waits)),app=SimpleNamespace(config={'WUYU_WORKFLOW_MEMORY_HIGH_BYTES':3000},app_context=lambda:nullcontext()),_fair_queue=lambda:self.fail('memory-unknown must not plan queue'))
        with patch.object(resource,'cgroup_memory_current',side_effect=OSError('synthetic')):ns['_schedule'](owner)

    def test_fair_queue_actor_cap_does_not_place_second_job_or_running_actor(self):
        ns=lifecycle_functions()
        rows=[SimpleNamespace(id='running-A',actor_user_id='A',status='running',created_at=0),
              SimpleNamespace(id='queued-A',actor_user_id='A',status='queued',created_at=1),
              SimpleNamespace(id='queued-B1',actor_user_id='B',status='queued',created_at=2),
              SimpleNamespace(id='queued-B2',actor_user_id='B',status='queued',created_at=3),
              SimpleNamespace(id='queued-C',actor_user_id='C',status='queued',created_at=4)]
        class Query:
            def __init__(self,items,actors=False):self.items=list(items);self.actors=actors
            def filter_by(self,**fields):return Query([j for j in self.items if all(getattr(j,k)==v for k,v in fields.items())],self.actors)
            def order_by(self,*unused):return Query(sorted(self.items,key=lambda j:j.created_at),self.actors)
            def limit(self,n):return Query(self.items[:n],self.actors)
            def all(self):return [(j.actor_user_id,) for j in self.items] if self.actors else list(self.items)
        ns['WuyuWorkflowJob']=SimpleNamespace(actor_user_id='actor_column',created_at='created_column',query=Query(rows))
        ns['db']=SimpleNamespace(session=SimpleNamespace(query=lambda column:Query(rows,True)))
        owner=SimpleNamespace(app=SimpleNamespace(config={'WUYU_WORKFLOW_PER_ACTOR':1}))
        self.assertEqual([j.id for j in ns['_fair_queue'](owner)],['queued-B1','queued-C'])
        owner.app.config['WUYU_WORKFLOW_PER_ACTOR']=0
        self.assertEqual(len(ns['_fair_queue'](owner)),4)


if __name__=='__main__':unittest.main(verbosity=2)
