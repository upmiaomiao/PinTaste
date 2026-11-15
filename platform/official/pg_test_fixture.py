"""Only disposable qa111 fixture databases; never application startup code."""
from __future__ import annotations
import contextlib
import os
import re
import secrets
import tempfile
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode
from unittest.mock import patch

from . import store as db


class PlatformPGFixture:
    def __init__(self):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        self.psycopg = psycopg
        self.admin_dsn = os.environ.get('WUYU_PG_TEST_DSN', '')
        config = conninfo_to_dict(self.admin_dsn)
        if (not self.admin_dsn or config.get('host') not in ('127.0.0.1', 'localhost')
                or not re.fullmatch(r'(?:wuyu_qa111_fixture|qa111_fixture)(?:_[a-f0-9]{8,32})?', config.get('dbname', ''))):
            raise RuntimeError('PG_TEST_FIXTURE_DSN_REQUIRED')
        if config.get('hostaddr') not in (None, '127.0.0.1'):
            raise RuntimeError('PG_TEST_FIXTURE_DSN_REQUIRED')
        self.config = config
        suffix = uuid.uuid4().hex[:12]
        self.database = 'qa111_core_' + suffix
        self.role = 'qa111_app_' + suffix
        self.password = secrets.token_urlsafe(24)
        self.created_database = False
        self.created_role = False
        self._temp = tempfile.TemporaryDirectory(prefix='qa111-store-fixture-')
        self.data = Path(self._temp.name)
        self.admin_url = self._url(self.database)
        self.app_url = self._url(self.database, self.role, self.password)

    def _url(self, database, user=None, password=None):
        config = self.config
        username = user if user is not None else config.get('user', 'postgres')
        secret = password if password is not None else config.get('password', '')
        authority = quote(username, safe='') + (':' + quote(secret, safe='') if secret else '') + '@' + config['host']
        if config.get('port'):
            authority += ':' + config['port']
        extras = {k: v for k, v in config.items() if k not in {'user', 'password', 'host', 'hostaddr', 'port', 'dbname'}}
        return 'postgresql://' + authority + '/' + database + ('?' + urlencode(extras) if extras else '')

    def __enter__(self):
        from psycopg import sql
        try:
            with self.psycopg.connect(self.admin_dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'").format(sql.Identifier(self.database)))
                self.created_database = True
                admin.execute(sql.SQL('CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS').format(sql.Identifier(self.role), sql.Literal(self.password)))
                self.created_role = True
                admin.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(self.database)))
                admin.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(sql.Identifier(self.database), sql.Identifier(self.role)))
            with self.activate(admin=True):
                from .runtime_bootstrap import initialize_runtime
                with db.schema_bootstrap():
                    initialize_runtime()
            with self.psycopg.connect(self.admin_url, autocommit=True) as admin:
                admin.execute('REVOKE CREATE ON SCHEMA public FROM PUBLIC')
                admin.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(self.role)))
                admin.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {}').format(sql.Identifier(self.role)))
                admin.execute(sql.SQL('REVOKE INSERT,UPDATE,DELETE ON wuyu_platform_schema_meta FROM {}').format(sql.Identifier(self.role)))
                admin.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO {}').format(sql.Identifier(self.role)))
            return self
        except BaseException:
            self.close()
            raise

    @contextlib.contextmanager
    def activate(self, *, admin=False):
        environment = {k: v for k, v in os.environ.items() if k not in {
            'WUYU_PLATFORM_DATABASE_URL', 'WUYU_PLATFORM_DATABASE_URL_FILE',
            'WUYU_PLATFORM_DATABASE_EXPECTED_DIALECT', 'WUYU_DATA'}}
        environment.update({'WUYU_PLATFORM_DATABASE_URL': self.admin_url if admin else self.app_url,
                            'WUYU_PLATFORM_DATABASE_EXPECTED_DIALECT': 'postgresql', 'WUYU_DATA': str(self.data)})
        from . import pg_pool
        pg_pool.close()
        with patch.dict(os.environ, environment, clear=True), patch.object(db, 'DATA', self.data):
            try:
                yield self
            finally:
                pg_pool.close()

    def close(self):
        from psycopg import sql
        if not re.fullmatch('qa111_core_[a-f0-9]{12}', self.database) or not re.fullmatch('qa111_app_[a-f0-9]{12}', self.role):
            raise RuntimeError('PG_FIXTURE_CLEANUP_GUARD_FAILED')
        with self.psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            if self.created_database:
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(self.database)))
                self.created_database = False
            if self.created_role:
                admin.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(self.role)))
                self.created_role = False
        self._temp.cleanup()

    def __exit__(self, *error):
        self.close()
