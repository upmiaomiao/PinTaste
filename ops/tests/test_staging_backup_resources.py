"""Pure known-oneshot lifecycle and unchanged cgroup admission contracts."""
from pathlib import Path
import importlib.util
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('resources171_lifecycle',Path(__file__).resolve().parents[1]/'staging_backup_resources.py')
R=importlib.util.module_from_spec(spec);spec.loader.exec_module(R)


class ResourceServiceLifecycleTests(unittest.TestCase):
    def props(self,**changes):
        value={'Type':'oneshot','ActiveState':'activating','SubState':'start','MainPID':'123',
               'ControlGroup':'/system.slice/synthetic.service','MemoryMax':'128000000','Result':'success','ExecMainStatus':'0'}
        value.update(changes);return value

    def observe(self,props,actual=128000000,current=100,exists=True):
        class VirtualPath:
            def __init__(self,path):self.path=str(path)
            def __truediv__(self,name):return VirtualPath(self.path+'/'+name)
            def exists(self):return exists
            def read_text(self):
                if self.path.endswith('/memory.max'):return str(actual)
                if self.path.endswith('/memory.current'):return str(current)
                raise AssertionError('Unexpected synthetic path')
        def command(argv):
            if argv[:2]==['systemctl','show']:
                self.assertIn('Type',argv)
                return '\n'.join(k+'='+v for k,v in props.items())
            self.assertEqual(argv[:3],['docker','ps','-q']);return ''
        with patch.object(R,'Path',VirtualPath),patch.object(R,'command',side_effect=command):
            return R._observe({'wuyu-staging-cert-renew.service':128000000},fixture_lease=True)

    def test_running_known_oneshots_are_accepted_for_cgroup_verification(self):
        for name in R.ONESHOTS:
            with self.subTest(name=name):self.assertEqual(R.service_lifecycle(name,self.props()),'running_cgroup')

    def test_inactive_known_oneshots_reserve_their_future_cap(self):
        for name in R.ONESHOTS:
            with self.subTest(name=name):
                self.assertEqual(R.service_lifecycle(name,self.props(ActiveState='inactive',SubState='dead',MainPID='0',ControlGroup='')),
                                 'inactive_reservation')

    def test_failed_unknown_and_non_oneshot_activation_remain_rejected(self):
        for name,state in [('unknown.service','activating'),(R.ROUTER_UNIT,'activating'),
                           ('wuyu-staging-cert-renew.service','failed'),('wuyu-staging-cert-renew.service','unknown')]:
            with self.subTest(name=name,state=state),self.assertRaisesRegex(RuntimeError,'RESOURCE_SERVICE_NOT_ACTIVE'):
                R.service_lifecycle(name,self.props(ActiveState=state))

    def test_fixed_oneshot_type_cannot_silently_change(self):
        for kind in ['simple','exec','notify','unknown',None]:
            with self.subTest(kind=kind),self.assertRaisesRegex(RuntimeError,'ONESHOT_SERVICE_TYPE_DRIFT'):
                R.service_lifecycle('wuyu-staging-task-monitor.service',self.props(Type=kind))

    def test_activation_requires_a_strict_positive_main_pid(self):
        for pid in ['0','-1','01','',None,True,123]:
            with self.subTest(pid=pid),self.assertRaisesRegex(RuntimeError,'ONESHOT_EXECUTION_NOT_PROVEN'):
                R.service_lifecycle('wuyu-staging-task-monitor.service',self.props(MainPID=pid))

    def test_activation_requires_known_start_state_and_non_root_cgroup(self):
        for state in ['dead','stop','unknown',None]:
            with self.subTest(state=state),self.assertRaisesRegex(RuntimeError,'ONESHOT_EXECUTION_NOT_PROVEN'):
                R.service_lifecycle('wuyu-staging-task-monitor.service',self.props(SubState=state))
        for group in ['', '/', 'relative', '/system.slice/../other', None]:
            with self.subTest(group=group),self.assertRaisesRegex(RuntimeError,'RESOURCE_CGROUP_PATH'):
                R.service_lifecycle('wuyu-staging-task-monitor.service',self.props(ControlGroup=group))

    def test_backup_prior_failure_is_not_hidden_by_running_or_inactive_state(self):
        for state in ['active','activating','inactive']:
            with self.subTest(state=state),self.assertRaisesRegex(RuntimeError,'BACKUP_SERVICE_PREVIOUS_FAILURE'):
                R.service_lifecycle(R.BACKUP_UNIT,self.props(ActiveState=state,Result='exit-code',ExecMainStatus='1'))

    def test_regular_and_explicitly_admitted_transient_active_services_keep_the_existing_path(self):
        for name in [R.ROUTER_UNIT,'wuyu-staging-pg-restore171-'+'a'*12+'.service']:
            with self.subTest(name=name):
                self.assertEqual(R.service_lifecycle(name,self.props(Type='exec',ActiveState='active',SubState='running')),'running_cgroup')

    def test_accepted_activation_still_requires_exact_finite_cap_and_headroom(self):
        good=self.observe(self.props())
        self.assertEqual(good['components']['wuyu-staging-cert-renew.service']['memory_max'],128000000)
        for props,actual,current in [(self.props(MemoryMax='129000000'),128000000,100),
                                     (self.props(),129000000,100),(self.props(),128000000,128000000),(self.props(),128000000,-1)]:
            with self.subTest(actual=actual,current=current),self.assertRaisesRegex(RuntimeError,'ACTUAL_MEMORY_CAP_DRIFT'):
                self.observe(props,actual,current)
        with self.assertRaises(ValueError):self.observe(self.props(),actual='max')
        self.assertEqual(len(R.CAPS),15);self.assertEqual(sum(R.CAPS.values())+R.CERTBOT_RESERVE,24786006016)

    def test_inactive_service_cannot_hide_a_remaining_cgroup(self):
        value=self.props(ActiveState='inactive',SubState='dead',MainPID='0')
        with self.assertRaisesRegex(RuntimeError,'INACTIVE_SERVICE_CGROUP_STILL_PRESENT'):self.observe(value,exists=True)
        accepted=self.observe(value,exists=False)
        self.assertTrue(accepted['components']['wuyu-staging-cert-renew.service']['inactive_future_start_limit'])


if __name__=='__main__':unittest.main()
