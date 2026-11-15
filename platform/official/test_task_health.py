"""Read-only task diagnostics with missing/stale workers and disposable queues."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from official import store as db, task_health
from official.chat_worker import heartbeat_status


class TaskHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)
        self.data_patch=patch.object(db,'DATA',self.path)
        self.mode_patch=patch.object(task_health,'runtime',replace(task_health.runtime,chat_execution_mode='api',task_queue_warn_seconds=300))
        self.data_patch.start();self.mode_patch.start()
        with sqlite3.connect(self.path/'platform.sqlite3') as conn:
            conn.execute('CREATE TABLE jobs(id TEXT,status TEXT,created REAL)')

    def tearDown(self):
        self.mode_patch.stop();self.data_patch.stop();self.temp.cleanup()

    def heartbeat(self,status='ready',updated=1000):
        (self.path/'chat-worker.json').write_text(json.dumps({'status':status,'updated':updated}))

    def job(self,status,created=1000):
        with sqlite3.connect(self.path/'platform.sqlite3') as conn:
            conn.execute('INSERT INTO jobs VALUES(?,?,?)',('private-job-id',status,created))

    def test_idle_ready_worker_has_no_alert(self):
        self.heartbeat()
        value=task_health.snapshot(1002)
        self.assertEqual(value['status'],'ok')
        self.assertEqual(value['alerts'],[])
        self.assertEqual(value['queue'],{'queued':0,'running':0,'oldest_queued_age_seconds':0})
        self.assertEqual(value['worker']['age_seconds'],2)

    def test_worker_alert_even_without_waiting_tasks(self):
        self.assertEqual(task_health.snapshot(1000)['alerts'],['worker_unavailable'])
        for status,updated,expected in [('ready',984,'stale'),('stopped',999,'stopped')]:
            self.heartbeat(status,updated)
            value=task_health.snapshot(1000)
            self.assertEqual(value['worker']['status'],expected)
            self.assertIn('worker_unavailable',value['alerts'])

    def test_queue_age_boundary_counts_and_private_values_excluded(self):
        self.heartbeat(updated=1300)
        self.job('queued',1000);self.job('queued',1250);self.job('running',800);self.job('complete',100)
        before=(self.path/'platform.sqlite3').read_bytes()
        value=task_health.snapshot(1300)
        self.assertEqual(value['queue'],{'queued':2,'running':1,'oldest_queued_age_seconds':300})
        self.assertEqual(value['alerts'],['queue_wait_high'])
        self.assertNotIn('private-job-id',json.dumps(value))
        self.assertEqual(before,(self.path/'platform.sqlite3').read_bytes())
        self.heartbeat(updated=1299)
        self.assertEqual(task_health.snapshot(1299)['alerts'],[])

    def test_missing_database_is_reported_without_creating_it(self):
        self.heartbeat()
        (self.path/'platform.sqlite3').unlink()
        value=task_health.snapshot(1000)
        self.assertIsNone(value['queue'])
        self.assertEqual(value['alerts'],['queue_unavailable'])
        self.assertFalse((self.path/'platform.sqlite3').exists())

    def test_corrupt_database_has_no_exception_details(self):
        self.heartbeat()
        (self.path/'platform.sqlite3').write_bytes(b'invalid database')
        value=task_health.snapshot(1000)
        self.assertEqual(value['alerts'],['queue_unavailable'])
        self.assertNotIn(str(self.path),json.dumps(value))

    def test_invalid_heartbeat_shapes_and_nonfinite_timestamps_are_safe(self):
        path=self.path/'chat-worker.json'
        for value in [None,[],{'status':'ready','updated':True},{'status':'ready','updated':'1000'},
                      {'status':'ready','updated':float('nan')},{'status':'ready','updated':float('inf')},
                      {'status':'ready','updated':1001},{'status':'unknown','updated':1000}]:
            with self.subTest(value=value):
                path.write_text(json.dumps(value))
                result=heartbeat_status(1000)
                self.assertEqual(result['status'],'invalid')
                json.dumps(result,allow_nan=False)

    def test_combined_mode_does_not_claim_missing_independent_worker(self):
        with patch.object(task_health,'runtime',replace(task_health.runtime,chat_execution_mode='combined')):
            value=task_health.snapshot(1000)
        self.assertEqual(value['status'],'ok')
        self.assertEqual(value['worker']['status'],'not_applicable')
        self.assertIsNone(value['worker']['ready'])

    def test_simultaneous_worker_failure_and_old_queue_are_both_reported(self):
        self.job('queued',100)
        self.assertEqual(task_health.snapshot(1000)['alerts'],['worker_unavailable','queue_wait_high'])


if __name__=='__main__':
    unittest.main()
