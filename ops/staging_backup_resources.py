"""Read-only current 171 resource profile; never replay historical releases.

The backup parent cap is funded by an equal Router cap reduction. pg_dump
executes inside the existing database cgroups. Temporary restore/test instances
must take a separate, explicitly recorded Router loan and close it afterwards.
"""
from pathlib import Path
import json
import os
import re
import stat
import subprocess
import time

FULL_BUDGET = 24_786_006_016
CERTBOT_RESERVE = 256_000_000
BACKUP_UNIT = 'wuyu-staging-pg-backup.service'
ROUTER_UNIT = 'wuyu-staging-router.service'
OLD_ROUTER_CAP = 9_799_999_488
ROUTER_CAP = 9_671_999_488
BACKUP_CAP = 128_000_000
CAPS = {
    'wuyu-staging-cert-renew.service': 128_000_000,
    'wuyu-staging-gateway.service': 1_999_998_976,
    'wuyu-staging-remote-sensing.service': 2_147_483_648,
    ROUTER_UNIT: ROUTER_CAP,
    'wuyu-staging-science.service': 2_147_483_648,
    'wuyu-staging-task-monitor.service': 128_000_000,
    'wuyu-staging-workflow-worker': 3_999_997_952,
    'wuyu-staging-backend': 999_997_440,
    'wuyu-staging-chat-worker': 399_998_976,
    'wuyu-staging-platform': 399_998_976,
    'wuyu-staging-qa-postgres111': 1_073_741_824,
    'wuyu-staging-cos-worker': 399_998_976,
    'wuyu-staging-ppt-postgres91': 805_306_368,
    'wuyu-staging-proxy': 99_999_744,
    BACKUP_UNIT: BACKUP_CAP,
}
ONESHOTS = {BACKUP_UNIT, 'wuyu-staging-cert-renew.service',
            'wuyu-staging-task-monitor.service'}


def require(value, code):
    if not value:
        raise RuntimeError(code)


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    require(result.returncode == 0, 'BOUNDED_RESOURCE_COMMAND_FAILED')
    return result.stdout.strip()


def service_lifecycle(name, props):
    """Classify admitted profile services without assuming oneshot is active.

    systemd keeps a Type=oneshot ExecStart in activating while it runs. Only
    the fixed ONESHOTS gain that path; actual cgroup and limits are checked by
    the caller. Explicitly admitted transient parents retain their active path.
    """
    known_oneshot = name in ONESHOTS
    if known_oneshot:
        require(props.get('Type') == 'oneshot', 'ONESHOT_SERVICE_TYPE_DRIFT')
    if name == BACKUP_UNIT:
        require(props.get('Result') == 'success' and props.get('ExecMainStatus') == '0',
                'BACKUP_SERVICE_PREVIOUS_FAILURE')
    if (known_oneshot and props.get('ActiveState') == 'inactive'
            and props.get('SubState') == 'dead' and props.get('MainPID') == '0'):
        return 'inactive_reservation'
    if props.get('ActiveState') == 'active':
        return 'running_cgroup'
    require(known_oneshot and props.get('ActiveState') == 'activating',
            'RESOURCE_SERVICE_NOT_ACTIVE')
    require(props.get('SubState') in {'start-pre', 'start', 'start-post'}
            and isinstance(props.get('MainPID'), str)
            and re.fullmatch(r'[1-9][0-9]*', props['MainPID']),
            'ONESHOT_EXECUTION_NOT_PROVEN')
    group = props.get('ControlGroup')
    require(isinstance(group, str) and group.startswith('/') and group != '/'
            and '..' not in group.split('/'), 'RESOURCE_CGROUP_PATH')
    return 'running_cgroup'


def _observe(profile, *, fixture_lease=False):
    out, groups = {}, []
    for name, expected in profile.items():
        if name.endswith('.service'):
            props = dict(line.split('=', 1) for line in command([
                'systemctl', 'show', name, '-p', 'ControlGroup', '-p', 'MemoryMax',
                '-p', 'MainPID', '-p', 'ActiveState', '-p', 'SubState',
                '-p', 'Result', '-p', 'ExecMainStatus', '-p', 'Type']).splitlines())
            limit, group = int(props['MemoryMax']), props['ControlGroup']
            lifecycle = service_lifecycle(name, props)
            if lifecycle == 'inactive_reservation':
                require(not group or not Path('/sys/fs/cgroup' + group).exists(),
                        'INACTIVE_SERVICE_CGROUP_STILL_PRESENT')
                require(limit == expected, 'INACTIVE_SERVICE_CAP_DRIFT')
                out[name] = {'memory_max': limit, 'memory_current': 0, 'cgroup': None,
                             'inactive_future_start_limit': True}
                continue
        else:
            row = json.loads(command(['docker', 'inspect', name]))[0]
            require(row['State']['Running'] is True and row['State']['Pid'] > 0,
                    'RESOURCE_CONTAINER_NOT_RUNNING')
            limit = row['HostConfig']['Memory']
            group = Path('/proc/' + str(row['State']['Pid']) + '/cgroup').read_text().strip().split('::', 1)[1]
        require(group.startswith('/') and '..' not in group.split('/'), 'RESOURCE_CGROUP_PATH')
        cg = Path('/sys/fs/cgroup' + group)
        actual, current = int((cg / 'memory.max').read_text()), int((cg / 'memory.current').read_text())
        require(limit == actual == expected and 0 <= current < actual, 'ACTUAL_MEMORY_CAP_DRIFT')
        out[name] = {'memory_max': actual, 'memory_current': current, 'cgroup': group}
        groups.append(group)
    require(len(set(groups)) == len(groups) and not any(
        a != b and b.startswith(a + '/') for a in groups for b in groups),
        'NESTED_RESOURCE_CAP_DOUBLE_COUNT')
    # A Docker-created certbot is outside its invoking oneshot's cgroup.
    for identity in command(['docker', 'ps', '-q', '--filter',
                             'name=^/wuyu-staging-certbot$']).split():
        row = json.loads(command(['docker', 'inspect', identity]))[0]
        require(row['State']['Running'] is True and row['State']['Pid'] > 0,
                'CERTBOT_NOT_RUNNING')
        group = Path('/proc/' + str(row['State']['Pid']) + '/cgroup').read_text().strip().split('::', 1)[1]
        require(group.startswith('/') and '..' not in group.split('/'), 'CERTBOT_CGROUP_PATH')
        cg = Path('/sys/fs/cgroup' + group)
        require(row['HostConfig']['Memory'] == int((cg / 'memory.max').read_text()) == CERTBOT_RESERVE
                and 0 <= int((cg / 'memory.current').read_text()) < CERTBOT_RESERVE,
                'CERTBOT_CAP_DRIFT')
        require(all(group != a and not group.startswith(a + '/') and not a.startswith(group + '/')
                    for a in groups), 'CERTBOT_NESTED_CAP_DOUBLE_COUNT')
    require(fixture_lease or not Path('/opt/wuyu-staging/operations/fixture-budget.active.json').exists(),
            'GLOBAL_FIXTURE_LOAN_ACTIVE')
    return {'schema_version': 2, 'resource_profile': 'staging-pg-backup-171',
            'components': out, 'hard_sum_bytes': sum(profile.values()),
            'certbot_reserved_bytes': CERTBOT_RESERVE,
            'full_hard_budget_bytes': sum(profile.values()) + CERTBOT_RESERVE}


def resources():
    require(len(CAPS) == 15 and sum(CAPS.values()) + CERTBOT_RESERVE == FULL_BUDGET,
            'RESOURCE_PROFILE_INVALID')
    return _observe(CAPS)


def validate_restore_admission(path):
    """Observe a root-frozen single restore window, without changing limits."""
    p = Path(path)
    root = Path('/opt/wuyu-staging/operations/staging-pg-backup-171/private')
    require(p.is_absolute() and p.parent == root and p.resolve() == p
            and not any(x.is_symlink() for x in [p, *p.parents]), 'RESTORE_ADMISSION_PATH')
    require(p.is_file() and p.stat().st_uid == 0 and stat.S_IMODE(p.stat().st_mode) == 0o600,
            'RESTORE_ADMISSION_MODE')
    value = json.loads(p.read_text())
    keys = {'schema_version', 'operation', 'purpose', 'token', 'batch_id', 'issued_at_epoch',
            'expires_at_epoch', 'Router_PID', 'router_memory_before_bytes',
            'router_memory_during_bytes', 'fixture_memory_bytes', 'parent_memory_bytes',
            'full_budget_bytes', 'pg_image', 'parent_unit'}
    require(isinstance(value, dict) and set(value) == keys and type(value['schema_version']) is int
            and value['schema_version'] == 1 and value['operation'] == 'staging-pg-backup-171'
            and value['purpose'] == 'isolated_pg_restore', 'RESTORE_ADMISSION_SCHEMA')
    require(isinstance(value['token'], str) and re.fullmatch('[a-f0-9]{32}', value['token'])
            and p.name == 'restore-lease-' + value['token'] + '.json', 'RESTORE_ADMISSION_TOKEN')
    require(value['parent_unit'] == 'wuyu-staging-pg-restore171-' + value['token'][:12] + '.service',
            'RESTORE_PARENT_UNIT')
    require(all(type(value[key]) in (int, float) and not isinstance(value[key], bool)
                for key in ['issued_at_epoch', 'expires_at_epoch'])
            and 0 < value['expires_at_epoch'] - value['issued_at_epoch'] <= 1800
            and value['issued_at_epoch'] <= time.time() < value['expires_at_epoch'], 'RESTORE_ADMISSION_EXPIRED')
    expected = {'router_memory_before_bytes': ROUTER_CAP, 'router_memory_during_bytes': 8_871_997_440,
                'fixture_memory_bytes': 536_870_912, 'parent_memory_bytes': 128_000_000,
                'full_budget_bytes': FULL_BUDGET}
    require(all(type(value[k]) is int and value[k] == v for k, v in expected.items()), 'RESTORE_RAM_CONTRACT')
    require(value['pg_image'] == 'sha256:933463a6c8eba2b9f5cd414138738cd8d5519cd2c471214c222652f547a66a32',
            'RESTORE_FIXED_PG_IMAGE')
    marker = Path('/opt/wuyu-staging/operations/fixture-budget.active.json')
    require(marker.is_file() and not marker.is_symlink() and marker.stat().st_uid == 0
            and stat.S_IMODE(marker.stat().st_mode) == 0o600
            and json.loads(marker.read_text()) == value, 'RESTORE_GLOBAL_LEASE_BINDING')
    actual_pid = command(['systemctl', 'show', ROUTER_UNIT, '-p', 'MainPID', '--value'])
    require(type(value['Router_PID']) is int and value['Router_PID'] > 0
            and actual_pid == str(value['Router_PID']), 'RESTORE_ROUTER_IDENTITY_DRIFT')
    require(command(['systemctl', 'show', value['parent_unit'], '-p', 'MainPID', '--value']) == str(os.getpid()),
            'RESTORE_PARENT_PROCESS_OUTSIDE_CAP')
    profile = dict(CAPS)
    profile[ROUTER_UNIT] = value['router_memory_during_bytes']
    profile[value['parent_unit']] = value['parent_memory_bytes']
    observed = _observe(profile, fixture_lease=True)
    require(observed['full_hard_budget_bytes'] + value['fixture_memory_bytes'] <= FULL_BUDGET,
            'RESTORE_TOTAL_BUDGET_EXCEEDED')
    require(not command(['docker', 'ps', '-q', '--filter',
                         'label=wuyu.operation=staging-pg-backup-171-restore']), 'RESTORE_FIXTURE_ALREADY_RUNNING')
    return value


if __name__ == '__main__':
    print(json.dumps(resources(), sort_keys=True))
