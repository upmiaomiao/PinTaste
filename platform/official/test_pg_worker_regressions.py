"""Existing worker settlement/cancellation contracts on disposable real PG.

Each inherited test receives a fresh fixture DB and an app role with no DDL
or TEMP privileges. Provider events remain synthetic; no live jobs are used.
"""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from . import store as db
from . import test_execution_status as status_cases
from . import test_failure_accounting as accounting_cases
from . import test_job_control as control_cases
from .pg_test_fixture import PlatformPGFixture


class _PostgresFixtureMixin:
    def setUp(self):
        fixture = PlatformPGFixture()
        fixture.__enter__()
        self.pg_fixture = fixture
        self.addCleanup(fixture.close)
        activation = fixture.activate()
        activation.__enter__()
        self.addCleanup(activation.__exit__, None, None, None)

        original_temporary_directory = tempfile.TemporaryDirectory

        def temporary_data(*args, **kwargs):
            directory = original_temporary_directory(*args, **kwargs)
            try:
                key = Path(directory.name) / 'vault.key'
                shutil.copyfile(fixture.data / 'vault.key', key)
                key.chmod(0o600)
            except BaseException:
                directory.cleanup()
                raise
            return directory

        guard = patch.object(sqlite3, 'connect', side_effect=AssertionError('PG_WORKER_MUST_NOT_CONNECT_SQLITE'))
        guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self._cleanup_inherited_setup)
        with patch.object(tempfile, 'TemporaryDirectory', side_effect=temporary_data):
            super().setUp()

        self.assertTrue(db.is_postgres())
        self.assertFalse((db.DATA / 'platform.sqlite3').exists())
        self.assertEqual((db.DATA / 'vault.key').read_bytes(), (fixture.data / 'vault.key').read_bytes())
        with db.connect(readonly=True) as conn:
            self.assertEqual(conn.execute('SELECT current_user').fetchone()[0], fixture.role)
            self.assertFalse(conn.execute("SELECT has_schema_privilege(current_user,'public','CREATE')").fetchone()[0])
            self.assertFalse(conn.execute("SELECT has_database_privilege(current_user,current_database(),'TEMPORARY')").fetchone()[0])

    def _cleanup_inherited_setup(self):
        # Registered before parent setup, so partial setup failures are safe.
        try:
            if hasattr(self, 'data_patch'):
                self.data_patch.stop()
        finally:
            if hasattr(self, 'temp'):
                self.temp.cleanup()

    def tearDown(self):
        # Cleanups run after async tearDown, before role/database disposal.
        pass


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'), 'Real PG fixture DSN is required')
class PostgreSQLFailureAccountingTests(_PostgresFixtureMixin, accounting_cases.FailureAccountingTests):
    """Fifteen original worker settlement cases, including Scheduler.run."""
    pass


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'), 'Real PG fixture DSN is required')
class PostgreSQLJobControlTests(_PostgresFixtureMixin, control_cases.JobControlTests):
    """Five original durable cancellation and heartbeat cases."""
    pass


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'), 'Real PG fixture DSN is required')
class PostgreSQLExecutionStatusTests(_PostgresFixtureMixin, status_cases.ExecutionStatusTests):
    """Five original timestamp/public-turn contracts in the PG fixture."""
    pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
