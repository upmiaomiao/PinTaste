"""Durable cancellation and worker health checks against disposable state."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from official import store as db
from official.chat_worker import heartbeat_ready,write_heartbeat
from official.jobs import request_cancel


class JobControlTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.data_patch=patch.object(db,'DATA',Path(self.temp.name))
        self.data_patch.start()
        db.init()
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?,1,?)',('user','fixture@example.test','Fixture','Test','其他','user',time.time()))
        db.execute('INSERT INTO conversations(id,user_id,title,created,updated) VALUES(?,?,?,?,?)',('conversation','user','Fixture',time.time(),time.time()))

    def tearDown(self):
        self.data_patch.stop()
        self.temp.cleanup()

    def job(self,status='queued'):
        turn={'status':status,'answer':'partial answer'}
        db.execute('INSERT INTO jobs(id,user_id,conversation_id,status,input,turn,created,reserved,inflight,day) VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('job','user','conversation',status,json.dumps({'request':{'messages':[]}}),json.dumps(turn),time.time(),100,50,'2026-11-03'))
        return db.query('SELECT * FROM jobs WHERE id=?',('job',),one=True)

    def test_cancel_queued_without_worker_stops_and_releases_reservation(self):
        row=self.job()
        request_cancel(row)
        current=db.query('SELECT * FROM jobs WHERE id=?',('job',),one=True)
        self.assertEqual(current['status'],'stopped')
        self.assertEqual(current['charged'],0)
        self.assertIsNotNone(current['finished'])
        self.assertGreater(current['revision'],row['revision'])

    def test_running_cancel_persists_intent_without_overwriting_progress(self):
        row=self.job('running')
        request_cancel(row)
        current=db.query('SELECT * FROM jobs WHERE id=?',('job',),one=True)
        self.assertEqual(current['status'],'running')
        self.assertEqual(current['turn'],row['turn'])
        self.assertTrue(json.loads(current['input'])['_cancel_requested'])

    def test_stale_queued_snapshot_cannot_cancel_as_unstarted_after_claim(self):
        row=self.job()
        db.execute("UPDATE jobs SET status='running' WHERE id='job'")
        request_cancel(row)
        current=db.query('SELECT * FROM jobs WHERE id=?',('job',),one=True)
        self.assertEqual(current['status'],'running')
        self.assertTrue(json.loads(current['input'])['_cancel_requested'])

    def test_completed_job_and_missing_job_are_not_rewritten(self):
        row=self.job('complete')
        request_cancel(row)
        request_cancel({'id':'missing'})
        self.assertEqual(db.query('SELECT * FROM jobs WHERE id=?',('job',),one=True),row)

    def test_health_requires_recent_ready_heartbeat(self):
        self.assertFalse(heartbeat_ready())
        write_heartbeat('ready')
        self.assertTrue(heartbeat_ready())
        write_heartbeat('stopped')
        self.assertFalse(heartbeat_ready())
        path=db.DATA/'chat-worker.json'
        path.write_text(json.dumps({'status':'ready','updated':time.time()-60}))
        self.assertFalse(heartbeat_ready())
        path.write_text('invalid')
        self.assertFalse(heartbeat_ready())


if __name__=='__main__':
    unittest.main()
