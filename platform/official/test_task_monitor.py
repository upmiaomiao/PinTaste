"""Journal notification deduplication and strict subprocess output filtering."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from official import task_monitor as monitor


def healthy():
    return {'status':'ok','execution_mode':'api','alerts':[],
            'worker':{'ready':True,'status':'ready','age_seconds':1},
            'queue':{'queued':0,'running':0,'oldest_queued_age_seconds':0},'queue_warn_seconds':300}


class TaskMonitorTests(unittest.TestCase):
    def test_initial_failure_repeated_failure_and_recovery(self):
        failed={'status':'unavailable','alerts':['probe_failed']}
        state,event=monitor.transition(None,failed,100)
        self.assertEqual(event['event'],'task_monitor_alert')
        state,event=monitor.transition(state,failed,130)
        self.assertIsNone(event)
        state,event=monitor.transition(state,failed,1000)
        self.assertEqual(event['event'],'task_monitor_alert')
        state,event=monitor.transition(state,healthy(),1001)
        self.assertEqual(event['event'],'task_monitor_recovery')
        state,event=monitor.transition(state,healthy(),1031)
        self.assertIsNone(event)

    def test_changed_alert_set_emits_immediately_without_count_noise(self):
        value=healthy();value.update(status='degraded',alerts=['worker_unavailable'])
        state,event=monitor.transition(None,value,100)
        value['queue']['queued']=8
        state,event=monitor.transition(state,value,101)
        self.assertIsNone(event)
        value['alerts']=['queue_wait_high','worker_unavailable']
        state,event=monitor.transition(state,value,102)
        self.assertEqual(event['event'],'task_monitor_alert')

    def test_sanitize_discards_unknown_fields_including_private_values(self):
        value=healthy();value['token']='must-not-log';value['queue']['user_email']='must-not-log'
        self.assertNotIn('must-not-log',json.dumps(monitor.sanitize(value)))

    def test_probe_failure_never_logs_raw_errors_or_invalid_payloads(self):
        for result in [subprocess.CompletedProcess([],2,'secret stdout','secret stderr'),
                       subprocess.CompletedProcess([],0,'not json','secret stderr'),
                       subprocess.CompletedProcess([],0,json.dumps({'status':'ok','alerts':['untrusted-content']}),'secret stderr')]:
            with patch.object(monitor.subprocess,'run',return_value=result):
                self.assertEqual(monitor.probe('fixture'),{'status':'unavailable','alerts':['probe_failed']})
        with patch.object(monitor.subprocess,'run',side_effect=subprocess.TimeoutExpired('secret-command',15)):
            self.assertEqual(monitor.probe('fixture'),{'status':'unavailable','alerts':['probe_failed']})

    def test_probe_accepts_ok_and_degraded_only_with_matching_exit_code(self):
        value=healthy()
        with patch.object(monitor.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(value),'')):
            self.assertEqual(monitor.probe('fixture'),value)
        value.update(status='degraded',alerts=['worker_unavailable'])
        value['worker'].update(ready=False,status='stopped')
        with patch.object(monitor.subprocess,'run',return_value=subprocess.CompletedProcess([],1,json.dumps(value),'')):
            self.assertEqual(monitor.probe('fixture'),value)
        with patch.object(monitor.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(value),'')):
            self.assertEqual(monitor.probe('fixture')['alerts'],['probe_failed'])

    def test_invalid_counts_and_heartbeat_ages_are_rejected(self):
        for field,value in [('queued',-1),('running',True),('oldest_queued_age_seconds',float('nan'))]:
            data=healthy();data['queue'][field]=value
            with self.assertRaises(ValueError):monitor.sanitize(data)
        data=healthy();data['worker']['age_seconds']=float('inf')
        with self.assertRaises(ValueError):monitor.sanitize(data)

    def test_state_is_atomic_private_and_unchanged_health_is_quiet(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'state.json'
            with patch.object(monitor,'probe',return_value=healthy()), patch('builtins.print') as emit:
                monitor.run_once('fixture',path)
                self.assertEqual(emit.call_count,1)
                monitor.run_once('fixture',path)
                self.assertEqual(emit.call_count,1)
            self.assertEqual(json.loads(path.read_text())['status'],'ok')
            self.assertFalse(path.with_suffix('.tmp').exists())


if __name__=='__main__':
    unittest.main()
