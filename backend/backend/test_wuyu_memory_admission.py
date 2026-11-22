"""Exercise actual admission helpers without initializing Flask, DB or worker."""
import ast
import hashlib
import os
from pathlib import Path, PurePosixPath
import unittest


def backend_root():
    if os.environ.get('WUYU_TEST_BACKEND_ROOT'):
        return Path(os.environ['WUYU_TEST_BACKEND_ROOT'])
    here = Path(__file__).resolve()
    if (here.parent / 'services/wuyu_resource_limits.py').is_file():
        return here.parent
    return here.parents[2] / 'candidate/backend/backend'


SOURCE = backend_root() / 'services/wuyu_resource_limits.py'
SOURCE_SHA256 = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
FUNCTIONS = ('cgroup_memory_current', 'cgroup_reclaimable_file', 'memory_admission')


class FakePath:
    def __init__(self, value, files):
        self.value, self.files = str(value), files

    def joinpath(self, *parts):
        return FakePath(str(PurePosixPath(self.value).joinpath(*parts)), self.files)

    def read_text(self, **kwargs):
        value = self.files.get(self.value)
        if value is None:
            raise FileNotFoundError(self.value)
        if isinstance(value, BaseException):
            raise value
        return value


def observation(current=3300, stat=None, *, raw_stat=None, cgroup='0::/fixture\n', high=3000, reserve=128):
    if stat is None:
        stat = dict(file=1000, shmem=0, inactive_file=800, file_dirty=0, file_writeback=0)
    files = {'/proc/self/cgroup': cgroup,
             '/sys/fs/cgroup/fixture/memory.current': str(current),
             '/sys/fs/cgroup/fixture/memory.stat': raw_stat if raw_stat is not None else '\n'.join(f'{k} {v}' for k, v in stat.items())}
    namespace = {'Path': lambda value: FakePath(value, files), 'PurePosixPath': PurePosixPath}
    tree = ast.parse(SOURCE.read_text(encoding='utf-8-sig'))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    assert len(selected) == len(FUNCTIONS)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), namespace)
    result = namespace['memory_admission']({'WUYU_WORKFLOW_MEMORY_HIGH_BYTES': high,
        'WUYU_WORKFLOW_MEMORY_RESERVE_BYTES': reserve})
    return result


class MemoryAdmissionTests(unittest.TestCase):
    def test_clean_inactive_cache_does_not_strand_queued_work(self):
        result = observation()
        self.assertTrue(result['allowed'])
        self.assertEqual(result['current_bytes'], 3300)
        self.assertEqual(result['working_set_bytes'], 2500)
        self.assertEqual(result['reclaimable_file_bytes'], 800)
        self.assertEqual(result['admission_bytes'], 3000)

    def test_genuine_resident_high_water_still_refuses_new_work(self):
        result = observation(current=3300, stat=dict(file=100, shmem=0, inactive_file=50, file_dirty=0, file_writeback=0))
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason'], 'memory_wait')

    def test_dirty_inactive_counterexample_never_borrows_clean_active_pages(self):
        result = observation(current=1000, high=850, reserve=0,
            stat=dict(file=400, shmem=0, inactive_file=200, active_file=200, file_dirty=200, file_writeback=0))
        self.assertEqual(result['reclaimable_file_bytes'], 0)
        self.assertFalse(result['allowed'])

    def test_writeback_and_dirty_overlap_remains_conservative(self):
        result = observation(stat=dict(file=1000, shmem=0, inactive_file=800, file_dirty=500, file_writeback=400))
        self.assertEqual(result['reclaimable_file_bytes'], 0)
        self.assertFalse(result['allowed'])

    def test_shmem_is_never_credited_as_reclaimable_file(self):
        result = observation(stat=dict(file=1000, shmem=900, inactive_file=800, file_dirty=0, file_writeback=0))
        self.assertEqual(result['reclaimable_file_bytes'], 100)
        self.assertFalse(result['allowed'])

    def test_reserve_inclusive_boundary_is_preserved(self):
        self.assertTrue(observation(reserve=500)['allowed'])
        self.assertFalse(observation(reserve=501)['allowed'])

    def test_negative_reservation_does_not_manufacture_capacity(self):
        result = observation(current=3300, reserve=-1000, stat=dict(file=0, shmem=0, inactive_file=0, file_dirty=0, file_writeback=0))
        self.assertFalse(result['allowed'])

    def test_missing_stat_fields_fall_back_to_raw_current(self):
        keys = ('file', 'shmem', 'inactive_file', 'file_dirty', 'file_writeback')
        for missing in keys:
            with self.subTest(missing=missing):
                values = dict(file=1000, shmem=0, inactive_file=800, file_dirty=0, file_writeback=0)
                values.pop(missing)
                self.assert_raw_fallback(observation(stat=values))

    def test_malformed_duplicate_and_negative_stat_fall_back(self):
        valid = 'file 1000\nshmem 0\ninactive_file 800\nfile_dirty 0\nfile_writeback 0'
        for invalid in (valid + '\nfile 1000', valid + '\nbroken', valid.replace('800', '-1'),
                        valid.replace('800', '800.0'), valid.replace('800', 'NaN')):
            with self.subTest(case=invalid):
                self.assert_raw_fallback(observation(raw_stat=invalid))

    def test_inconsistent_or_stale_stat_values_do_not_create_credit(self):
        for bad in (dict(file=1000, shmem=1001, inactive_file=800, file_dirty=0, file_writeback=0),
                    dict(file=4000, shmem=0, inactive_file=800, file_dirty=0, file_writeback=0),
                    dict(file=1000, shmem=0, inactive_file=1001, file_dirty=0, file_writeback=0),
                    dict(file=1000, shmem=0, inactive_file=800, file_dirty=1001, file_writeback=0)):
            with self.subTest(case=bad):
                self.assert_raw_fallback(observation(stat=bad))

    def test_unrelated_counters_do_not_require_type_list_partition_identity(self):
        result = observation(stat=dict(file=1000, shmem=0, inactive_file=800,
            active_file=13, file_dirty=0, file_writeback=0, pgscan=999999999))
        self.assertTrue(result['allowed'])

    def test_unreadable_stat_keeps_raw_current(self):
        self.assert_raw_fallback(observation(raw_stat=PermissionError('fixture access denied')))

    def test_unknown_or_invalid_cgroup_current_fails_closed(self):
        for current in ('not-a-number', '-1'):
            with self.subTest(current=current):
                result = observation(current=current)
                self.assertFalse(result['allowed'])
                self.assertEqual(result['reason'], 'memory_state_unknown')

    def test_invalid_self_cgroup_cannot_read_another_path(self):
        for cgroup in ('0::/fixture/../other', '0::relative', '1:name=x:/fixture'):
            with self.subTest(cgroup=cgroup):
                self.assertEqual(observation(cgroup=cgroup)['reason'], 'memory_state_unknown')

    def test_disabled_admission_contract_is_unchanged(self):
        self.assertEqual(observation(high=0), {'allowed': True, 'reason': 'disabled'})

    def assert_raw_fallback(self, result):
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reclaimable_file_bytes'], 0)
        self.assertEqual(result['working_set_bytes'], result['current_bytes'])


if __name__ == '__main__':
    unittest.main()
