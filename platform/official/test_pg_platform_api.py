"""Existing API budget contracts against a fresh real PostgreSQL per test.

Only WUYU_PG_TEST_DSN fixture databases are accepted by PlatformPGFixture.
The normal API uses a separate DML-only role after privileged bootstrap.
All providers are synthetic; sqlite3.connect is forbidden during API use.
"""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from . import test_model_budget as budget_cases
from .pg_test_fixture import PlatformPGFixture


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'), 'Real PG fixture DSN is required')
class PostgreSQLModelBudgetAdmissionTests(budget_cases.ModelBudgetAdmissionTests):
    """Run all sixteen original API tests, without a SQLite fallback."""

    def setUp(self):
        self.pg_fixture = PlatformPGFixture()
        self.pg_fixture.__enter__()
        self.addCleanup(self.pg_fixture.close)
        activation = self.pg_fixture.activate()
        activation.__enter__()
        self.addCleanup(activation.__exit__, None, None, None)

        # Bootstrap created the schema and the original synthetic vault key.
        # The inherited setup uses a new DATA directory. Preserve that key so
        # consumer startup validates an existing identity rather than creating
        # a new key or needing privileged schema initialization.
        original_temporary_directory = tempfile.TemporaryDirectory
        fixture = self.pg_fixture

        def temporary_data(*args, **kwargs):
            directory = original_temporary_directory(*args, **kwargs)
            try:
                shutil.copyfile(Path(fixture.data) / 'vault.key', Path(directory.name) / 'vault.key')
                (Path(directory.name) / 'vault.key').chmod(0o600)
            except BaseException:
                directory.cleanup()
                raise
            return directory

        # Any accidental use of the sealed SQLite path, including a monitoring
        # or FTS fallback, must fail this suite rather than silently pass.
        no_sqlite = patch.object(sqlite3, 'connect', side_effect=AssertionError('PG_API_MUST_NOT_CONNECT_SQLITE'))
        no_sqlite.start()
        self.addCleanup(no_sqlite.stop)
        self.addCleanup(self._close_inherited_setup)
        with patch.object(budget_cases.tempfile, 'TemporaryDirectory', side_effect=temporary_data):
            super().setUp()

        with budget_cases.db.connect(readonly=True) as conn:
            role = conn.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user').fetchone()
            self.assertFalse(any(role))
            self.assertFalse(conn.execute("SELECT has_schema_privilege(current_user,'public','CREATE')").fetchone()[0])
            self.assertFalse(conn.execute("SELECT has_database_privilege(current_user,current_database(),'TEMPORARY')").fetchone()[0])
        self.assertTrue(budget_cases.db.is_postgres())
        self.assertFalse((Path(self.temp.name) / 'platform.sqlite3').exists())

    def _close_inherited_setup(self):
        # Registered before parent setup so partial startup failures still
        # release the DATA patch, local files, DB connections and fixture.
        try:
            if hasattr(self, 'client'):
                self.client.__exit__(None, None, None)
        finally:
            try:
                if hasattr(self, 'data'):
                    self.data.stop()
            finally:
                if hasattr(self, 'temp'):
                    self.temp.cleanup()

    def tearDown(self):
        # unittest cleanups run in the required reverse order: API, sqlite
        # guard, app environment, then this exact fixture DB and role.
        pass


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'), 'Real PG fixture DSN is required')
class PostgreSQLReservationContractTests(budget_cases.ReservationContractTests):
    """The two original pure policy tests; no duplicated imported TestCase."""
    pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
