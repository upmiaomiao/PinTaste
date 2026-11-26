"""Explicit PostgreSQL migrator for 027_user_profiles (staging op201; same recipe as op191's 026).

The backend refuses Alembic through create_app on PostgreSQL
(DATABASE_EXPLICIT_MIGRATOR_REQUIRED) and the app role has no CREATE on the
schema. Tables are owned by the migrator role and granted table-by-table to the
app role (no default privileges). So this script:

  upgrade   - as the MIGRATOR role, in ONE transaction: create wuyu_user_profiles,
              GRANT SELECT/INSERT/UPDATE/DELETE on exactly that table to the app
              role, stamp 026_design_habits -> 027_user_profiles.
  downgrade - as the MIGRATOR role, in ONE transaction: drop the table,
              stamp 027 -> 026 (rollback).
  check     - as the APP role: version, boot schema check, profile table present,
              app privileges, and a write probe that is always rolled back.

Environment:
  DATABASE_URL       app role URL (as used by the backend container)
  MIGRATOR_SECRETS   path to the JSON file holding role passwords (read only)
Never prints a URL or password.
"""
import importlib.util
import json
import os
import sys
import uuid
from datetime import datetime

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

FROM_REV, TO_REV = '026_design_habits', '027_user_profiles'
MIGRATOR_ROLE = 'ppt_migrator91'
MIGRATION = os.path.join(os.getcwd(), 'migrations', 'versions', '027_user_profiles.py')
HABIT_TABLES = ('wuyu_user_profiles',)
PRIVILEGES = ('SELECT', 'INSERT', 'UPDATE', 'DELETE')


def app_url():
    url = make_url(os.environ['DATABASE_URL'])
    if not url.drivername.startswith('postgresql'):
        sys.exit('refusing: not a PostgreSQL URL')
    return url


def migrator_url():
    url = app_url()
    with open(os.environ['MIGRATOR_SECRETS'], encoding='utf-8') as stream:
        password = json.load(stream)[MIGRATOR_ROLE]
    return url.set(username=MIGRATOR_ROLE, password=password)


def version(conn):
    rows = conn.execute(text('select version_num from alembic_version')).fetchall()
    if len(rows) != 1:
        sys.exit('refusing: alembic_version must have exactly one row, found %d' % len(rows))
    return rows[0][0]


def load_migration():
    spec = importlib.util.spec_from_file_location('m027', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.revision != TO_REV or module.down_revision != FROM_REV or tuple(module.TABLES) != HABIT_TABLES:
        sys.exit('refusing: unexpected migration file contents')
    return module


def run(direction):
    module = load_migration()
    app_role = app_url().username
    with create_engine(migrator_url()).begin() as conn:  # one transaction, rolled back on any error
        if conn.execute(text('select current_user')).scalar() != MIGRATOR_ROLE:
            sys.exit('refusing: not connected as the migrator role')
        current = version(conn)
        want_from, want_to = (FROM_REV, TO_REV) if direction == 'upgrade' else (TO_REV, FROM_REV)
        if current == want_to:
            print('no-op: already at', current)
            return
        if current != want_from:
            sys.exit('refusing: database at %s, expected %s' % (current, want_from))
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            getattr(module, direction)()
        if direction == 'upgrade':
            role = conn.dialect.identifier_preparer.quote(app_role)
            for table in HABIT_TABLES:
                conn.execute(text('GRANT %s ON TABLE public.%s TO %s' % (', '.join(PRIVILEGES), table, role)))
        conn.execute(text('update alembic_version set version_num = :v'), {'v': want_to})
        print('%s: %s -> %s%s' % (direction, current, want_to,
                                  ' (granted %s on %d tables to %s)' % ('/'.join(PRIVILEGES), len(HABIT_TABLES), app_role)
                                  if direction == 'upgrade' else ''))


def write_probe(engine):
    """Insert/update/delete one row as the app role on a separate connection; always rolled back."""
    probe = 'op201-probe-' + uuid.uuid4().hex[:8]
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("insert into wuyu_user_profiles (owner_id, value, updated_at) values (:o, '{}', :t)"),
                         {'o': probe, 't': datetime.utcnow()})
            seen = conn.execute(text('select count(*) from wuyu_user_profiles where owner_id=:o'), {'o': probe}).scalar()
            conn.execute(text("update wuyu_user_profiles set value='{\"status\":\"skipped\"}' where owner_id=:o"), {'o': probe})
            conn.execute(text('delete from wuyu_user_profiles where owner_id=:o'), {'o': probe})
        finally:
            trans.rollback()
    with engine.connect() as conn:
        left = conn.execute(text('select count(*) from wuyu_user_profiles where owner_id=:o'), {'o': probe}).scalar()
    return seen == 1, left


def check():
    sys.path.insert(0, os.getcwd())
    import models  # noqa: F401  registers the eagerly imported models
    from models import db
    boot_tables = {t.name for t in db.metadata.sorted_tables}
    has_habits = os.path.exists(os.path.join(os.getcwd(), 'models', 'wuyu_user_profile.py'))
    if has_habits:
        importlib.import_module('models.wuyu_user_profile')
    engine = create_engine(app_url())
    with engine.connect() as conn:
        print('connected as:', conn.execute(text('select current_user')).scalar())
        print('version:', version(conn))
        inspector = inspect(conn)
        missing = []
        for table in db.metadata.sorted_tables:
            if not inspector.has_table(table.name, schema=table.schema):
                missing.append(table.name + ' (table)')
                continue
            columns = {row['name'] for row in inspector.get_columns(table.name, schema=table.schema)}
            for column in sorted(set(table.columns.keys()) - columns):
                missing.append('%s.%s' % (table.name, column))
        boot_missing = [m for m in missing if m.split(' ')[0].split('.')[0] in boot_tables]
        habit_missing = [m for m in missing if m not in boot_missing]
        print('model tables: %d (boot-checked %d)' % (len(db.metadata.sorted_tables), len(boot_tables)))
        print('boot schema check:', 'PASS' if not boot_missing else 'FAIL ' + str(boot_missing))
        lacking, owners = None, None
        if has_habits and not habit_missing:
            lacking = [t for t in HABIT_TABLES if not all(conn.execute(text(
                'select has_table_privilege(current_user, :t, :p)'), {'t': 'public.' + t, 'p': p}).scalar()
                for p in PRIVILEGES)]
            owners = conn.execute(text(
                "select string_agg(distinct tableowner, ',') from pg_tables where schemaname='public' and tablename = any(:t)"),
                {'t': list(HABIT_TABLES)}).scalar()
    if not has_habits:
        print('user profiles: not in this code')
    elif habit_missing:
        print('user profiles available: NO, missing', habit_missing)
    else:
        print('profile table owner:', owners)
        print('app privileges on profile table:', 'OK' if not lacking else 'MISSING on ' + str(lacking))
        wrote, left = write_probe(engine) if not lacking else (False, None)
        print('app write probe (rolled back):', 'OK' if wrote else 'FAILED', ' rows left:', left)
        print('user profiles available:', 'YES' if not lacking and wrote and left == 0 else 'NO')
    if boot_missing:
        sys.exit(1)


if __name__ == '__main__':
    action = sys.argv[1] if len(sys.argv) > 1 else ''
    if action in ('upgrade', 'downgrade'):
        run(action)
    elif action == 'check':
        check()
    else:
        sys.exit(__doc__)
