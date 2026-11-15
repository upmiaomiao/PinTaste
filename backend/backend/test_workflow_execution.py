"""Workflow role boundaries and recovery against disposable SQLite, no provider calls."""
from datetime import datetime,timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from models import db,Project,Task
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_workflow_service import WuyuWorkflowService
from workflow_execution import (execution_mode,worker_owner,owner_lock_held,heartbeat_ready,write_heartbeat)


class WorkflowExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.directory=Path(self.temp.name)
        self.app=Flask(__name__)
        self.app.config.update(TESTING=True,SQLALCHEMY_DATABASE_URI='sqlite:///'+str(self.directory/'test.db'),
            SQLALCHEMY_TRACK_MODIFICATIONS=False,WUYU_WORKFLOW_EXECUTION_MODE='api',
            WUYU_WORKFLOW_AUTOSTART=False,WUYU_QUOTA_ENABLED=False,UPLOAD_FOLDER=str(self.directory/'uploads'))
        db.init_app(self.app)
        self.context=self.app.app_context();self.context.push()
        db.create_all()
        self.services=[]

    def tearDown(self):
        for service in self.services:service.shutdown()
        db.session.remove();db.engine.dispose();self.context.pop();self.temp.cleanup()

    def service(self,mode='api'):
        self.app.config['WUYU_WORKFLOW_EXECUTION_MODE']=mode
        value=WuyuWorkflowService(self.app);self.services.append(value)
        return value

    def job(self,service,status='queued',pid=None,fresh=False):
        project=Project(project_title='Isolated fixture');db.session.add(project);db.session.commit()
        row=service._ensure(project.id)
        task=Task(project_id=project.id,task_type='WUYU_WORKFLOW',status='PROCESSING' if status=='running' else 'PENDING')
        db.session.add(task);db.session.flush()
        job=WuyuWorkflowJob(project_id=project.id,task_id=task.id,action='_parse_materials',
            payload='{"material_ids":[]}',status=status,worker_pid=pid,
            heartbeat_at=datetime.utcnow()-(timedelta() if fresh else timedelta(days=1)))
        db.session.add(job);db.session.flush()
        if status=='running':row.active_job_id=job.id
        db.session.commit();return job

    def test_execution_modes_and_inline_conflicts_are_rejected(self):
        self.assertEqual(execution_mode({}),'combined')
        for mode in ('combined','api','worker'):self.assertEqual(execution_mode({'WUYU_WORKFLOW_EXECUTION_MODE':mode}),mode)
        for mode in ('','API','both'):
            with self.assertRaises(ValueError):execution_mode({'WUYU_WORKFLOW_EXECUTION_MODE':mode})
        for mode in ('api','worker'):
            with self.assertRaises(ValueError):execution_mode({'WUYU_WORKFLOW_EXECUTION_MODE':mode,'WUYU_WORKFLOW_INLINE':True})

    def test_api_startup_does_not_recover_running_or_reconcile_points(self):
        service=self.service();job=self.job(service,'running',pid=999999)
        before=(job.status,job.worker_pid,job.heartbeat_at)
        with patch('services.wuyu_workflow_service.recover_workflow_charges') as recover:
            service.initialize();recover.assert_not_called()
        db.session.expire_all();current=db.session.get(WuyuWorkflowJob,job.id)
        self.assertEqual((current.status,current.worker_pid,current.heartbeat_at),before)
        self.assertIsNone(service.executor);self.assertIsNone(service.scheduler)

    def test_api_cannot_claim_execute_or_start_worker(self):
        service=self.service();service.initialize();job=self.job(service)
        self.assertFalse(service._claim(job.id))
        with self.assertRaises(RuntimeError):service.run_pending()
        with self.assertRaises(RuntimeError):service.start_worker()
        self.assertEqual(db.session.get(WuyuWorkflowJob,job.id).status,'queued')

    def test_api_does_not_create_missing_workflow_schema(self):
        from models.wuyu_workflow import WuyuWorkflow
        WuyuWorkflowJob.__table__.drop(db.engine)
        WuyuWorkflow.__table__.drop(db.engine)
        with self.assertRaises(RuntimeError):self.service().initialize()
        from sqlalchemy import inspect
        self.assertFalse(inspect(db.engine).has_table('wuyu_workflow_jobs'))

    def test_worker_must_hold_lock_before_any_recovery(self):
        service=self.service('worker');job=self.job(service,'running',pid=os.getpid(),fresh=True)
        with self.assertRaises(RuntimeError):service.initialize()
        self.assertEqual(db.session.get(WuyuWorkflowJob,job.id).status,'running')

    def test_new_worker_recovers_even_same_pid_and_fresh_old_heartbeat(self):
        service=self.service('worker');job=self.job(service,'running',pid=os.getpid(),fresh=True)
        queued=self.job(service)
        with worker_owner(self.directory):
            with patch('services.wuyu_workflow_service.recover_workflow_charges') as recover:
                service.initialize();recover.assert_called_once_with(self.app)
            self.assertIsNone(service.scheduler,'Worker app initialization must not dispatch yet')
        db.session.expire_all()
        self.assertEqual(db.session.get(WuyuWorkflowJob,job.id).status,'interrupted')
        self.assertEqual(db.session.get(WuyuWorkflowJob,queued.id).status,'queued')
        self.assertIsNone(service._ensure(job.project_id).active_job_id)

    def test_combined_mode_keeps_alive_current_pid_job(self):
        service=self.service('combined');job=self.job(service,'running',pid=os.getpid(),fresh=True)
        with patch('services.wuyu_workflow_service.recover_workflow_charges'):service.initialize()
        self.assertEqual(db.session.get(WuyuWorkflowJob,job.id).status,'running')

    def test_queue_status_uses_database_not_api_futures(self):
        service=self.service();service.initialize();self.job(service,'running',pid=123,fresh=True);queued=self.job(service)
        self.assertEqual(service.futures,{})
        queue=service.queue_snapshot(queued)
        self.assertEqual(queue['running'],1);self.assertEqual(queue['workers'],2)

    def test_second_process_cannot_acquire_worker_lock(self):
        with worker_owner(self.directory):
            self.assertTrue(owner_lock_held())
            code='from workflow_execution import worker_owner; import sys\nwith worker_owner(sys.argv[1]): pass'
            result=subprocess.run([sys.executable,'-c',code,str(self.directory)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Another workflow worker owns',result.stderr)
        self.assertFalse(owner_lock_held())
        with worker_owner(self.directory):self.assertTrue(owner_lock_held())

    def test_health_probe_does_not_initialize_application_or_database(self):
        self.assertFalse(heartbeat_ready(self.directory))
        write_heartbeat('ready',self.directory);self.assertTrue(heartbeat_ready(self.directory))
        write_heartbeat('starting',self.directory);self.assertFalse(heartbeat_ready(self.directory))
        write_heartbeat('stopped',self.directory);self.assertFalse(heartbeat_ready(self.directory))
        path=self.directory/'workflow-worker.json'
        for value in ([],{'status':'ready','updated':True},{'status':'ready','updated':float('nan')},
                      {'status':'ready','updated':99999}):
            path.write_text(json.dumps(value));self.assertFalse(heartbeat_ready(self.directory,now=1000))
        env={**os.environ,'WUYU_WORKFLOW_STATE_DIR':str(self.directory/'probe-only')}
        result=subprocess.run([sys.executable,'-m','workflow_worker','--check-health'],env=env,capture_output=True,text=True)
        self.assertEqual(result.returncode,1)
        self.assertFalse((self.directory/'probe-only').exists())
        self.assertEqual(result.stdout,'');self.assertEqual(result.stderr,'')


if __name__=='__main__':unittest.main()
