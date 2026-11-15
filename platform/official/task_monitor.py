"""Host-side single probe: emit journal alerts, transitions and bounded reminders.

No network notification or database writes. Run by the staging systemd timer.
"""
import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time

ALERTS={'worker_unavailable','queue_wait_high','queue_unavailable'}
WORKER_STATES={'ready','stopped','stale','missing','invalid','not_applicable'}


def number(value):
    return not isinstance(value,bool) and isinstance(value,(int,float)) and math.isfinite(value) and value>=0


def sanitize(value):
    """Accept only the fixed diagnostic schema, never log subprocess output/errors."""
    if not isinstance(value,dict) or value.get('status') not in {'ok','degraded'}:
        raise ValueError('Invalid status')
    alerts=value['alerts']
    if not isinstance(alerts,list) or any(code not in ALERTS for code in alerts):
        raise ValueError('Invalid alerts')
    if (value['status']=='ok') != (not alerts):
        raise ValueError('Status/alert mismatch')
    worker=value['worker']
    state=worker['status'];ready=worker['ready'];age=worker['age_seconds']
    if state not in WORKER_STATES or (ready is not None and not isinstance(ready,bool)):
        raise ValueError('Invalid worker state')
    if ready != (True if state=='ready' else None if state=='not_applicable' else False):
        raise ValueError('Worker state mismatch')
    if age is not None and not number(age):
        raise ValueError('Invalid heartbeat age')
    mode=value['execution_mode'];threshold=value['queue_warn_seconds']
    if mode not in {'api','worker','combined'} or type(threshold) is not int or not 30<=threshold<=86400:
        raise ValueError('Invalid execution settings')
    queue=value['queue']
    if queue is not None:
        if (type(queue['queued']) is not int or queue['queued']<0
                or type(queue['running']) is not int or queue['running']<0
                or not number(queue['oldest_queued_age_seconds'])):
            raise ValueError('Invalid queue counts')
        queue={key:queue[key] for key in ('queued','running','oldest_queued_age_seconds')}
    return {'status':value['status'],'execution_mode':mode,'alerts':sorted(set(alerts)),
            'worker':{'status':state,'ready':ready,'age_seconds':age},
            'queue':queue,'queue_warn_seconds':threshold}


def probe(container, docker='docker'):
    try:
        result=subprocess.run([docker,'exec',container,'python','-m','official.task_health'],
                              capture_output=True,text=True,timeout=15)
        if result.returncode not in (0,1) or len(result.stdout)>16384:
            raise ValueError('Probe failed')
        value=sanitize(json.loads(result.stdout))
        if result.returncode != (0 if value['status']=='ok' else 1):
            raise ValueError('Probe exit/status mismatch')
        return value
    except (subprocess.SubprocessError,OSError,ValueError,KeyError,TypeError,AttributeError):
        return {'status':'unavailable','alerts':['probe_failed']}


def transition(previous,current,now,repeat_seconds=900):
    fingerprint=(current['status'],tuple(current['alerts']))
    old=None if previous is None else (previous.get('status'),tuple(previous.get('alerts',[])))
    changed=fingerprint!=old
    last=previous.get('last_event',0) if previous else 0
    repeated=(current['status']!='ok' and not changed and now-last>=repeat_seconds)
    event=None
    if changed or repeated:
        name=('task_monitor_ready' if previous is None and current['status']=='ok' else
              'task_monitor_recovery' if current['status']=='ok' else 'task_monitor_alert')
        event={'event':name,'level':'info' if current['status']=='ok' else 'warning',
               'checked_at':now,**current}
        last=now
    return {**current,'checked_at':now,'last_event':last},event


def run_once(container,state_path,repeat_seconds=900,docker='docker'):
    previous=None
    try:
        value=json.loads(state_path.read_text())
        if (isinstance(value,dict) and value.get('status') in {'ok','degraded','unavailable'}
                and isinstance(value.get('alerts'),list) and number(value.get('last_event'))):
            previous=value
    except (OSError,ValueError,TypeError):
        pass
    current=probe(container,docker)
    updated,event=transition(previous,current,time.time(),repeat_seconds)
    state_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=state_path.with_suffix('.tmp')
    temp.write_text(json.dumps(updated,allow_nan=False))
    temp.chmod(0o600)
    os.replace(temp,state_path)
    if event is not None:
        print(json.dumps(event,allow_nan=False),flush=True)
    # A diagnostic alert is handled successfully; failures to probe are in the event/state.
    return updated


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container',default='wuyu-staging-platform')
    parser.add_argument('--state',type=Path,default=Path('/var/lib/wuyu-staging-task-monitor/state.json'))
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',args.container):
        parser.error('Invalid container name')
    run_once(args.container,args.state)


if __name__=='__main__':
    main()
