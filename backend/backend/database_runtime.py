"""Database configuration boundary; importing this module performs no database IO.

PostgreSQL requires an explicit psycopg driver, separate API/worker roles and
pre-created schemas. SQLite defaults remain compatible.
"""
from collections.abc import Mapping

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy import inspect


_ERROR_MESSAGES = {
    'DATABASE_URL_INVALID': 'Database configuration is invalid.',
    'DATABASE_DIALECT_UNSUPPORTED': 'Database dialect or driver is unsupported.',
    'DATABASE_BINDS_INVALID': 'Database bindings are invalid.',
    'DATABASE_DIALECT_MISMATCH': 'Database deployment dialect does not match its explicit configuration.',
    'DATABASE_POSTGRES_ROLE_INVALID': 'PostgreSQL requires a single main database and separate API or worker mode.',
    'DATABASE_EXPLICIT_MIGRATOR_REQUIRED': 'PostgreSQL schemas require an explicit offline migrator.',
    'DATABASE_POSTGRES_VERSION_UNSUPPORTED': 'PostgreSQL 16 or later is required.',
    'DATABASE_SCHEMA_NOT_READY': 'Database schema is not ready; run the reviewed migration separately.',
    'DATABASE_POSTGRES_NOT_READY': (
        'PostgreSQL runtime is not enabled: accounting, design transactions, '
        'template queries and COS readers still require compatibility validation.'
    ),
}
_DRIVERS = {
    'sqlite': 'sqlite',
    'sqlite+pysqlite': 'sqlite',
    'postgresql': 'postgresql',
    'postgresql+psycopg': 'postgresql',
}


class DatabaseConfigurationError(ValueError):
    """A fixed error code/message which never includes caller-supplied data."""

    def __init__(self, code='DATABASE_URL_INVALID'):
        self.code = code if isinstance(code, str) and code in _ERROR_MESSAGES else 'DATABASE_URL_INVALID'
        super().__init__(_ERROR_MESSAGES[self.code])


def _configuration_url(value):
    if not isinstance(value, (str, URL)):
        raise DatabaseConfigurationError()
    if isinstance(value, str) and (
        not value or len(value) > 8192 or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise DatabaseConfigurationError()
    try:
        url = make_url(value)
        # URL plugins can load application code during engine construction.
        if any(str(key).lower() == 'plugin' for key in url.query):
            raise DatabaseConfigurationError()
        dialect = _DRIVERS.get(url.drivername)
        if dialect is None:
            raise DatabaseConfigurationError('DATABASE_DIALECT_UNSUPPORTED')
        if dialect == 'sqlite':
            if any(item is not None for item in (url.username, url.password, url.host, url.port)):
                raise DatabaseConfigurationError()
        else:
            if not url.username or not url.host or not url.database:
                raise DatabaseConfigurationError()
            if url.port is not None and not 1 <= url.port <= 65535:
                raise DatabaseConfigurationError()
        return url
    except DatabaseConfigurationError:
        raise
    except (ArgumentError, TypeError, ValueError, AttributeError):
        # SQLAlchemy parsing errors can contain the original URL or port value.
        raise DatabaseConfigurationError() from None


def database_dialect(value):
    """Return a supported dialect name without loading a driver or opening a DB."""
    return _DRIVERS[_configuration_url(value).drivername]


def engine_options(value):
    """Return fresh dialect-specific options for isolated compatibility testing."""
    if database_dialect(value) == 'sqlite':
        # Preserve the existing SQLite options exactly.
        return {
            'connect_args': {'check_same_thread': False, 'timeout': 30},
            'pool_pre_ping': True,
            'pool_recycle': 3600,
        }
    return {
        'connect_args': {'connect_timeout': 10},
        'hide_parameters': True,
        'pool_pre_ping': True,
        'pool_recycle': 3600,
        'pool_size': 5,
        'max_overflow': 0,
        'pool_timeout': 30,
    }


def normalize_runtime_url(value):
    url = _configuration_url(value)
    if database_dialect(url) == 'postgresql':
        return url.set(drivername='postgresql+psycopg')
    return value


def runtime_engine_options(value):
    return engine_options(normalize_runtime_url(value))


def validate_runtime_databases(config):
    if not isinstance(config, Mapping):
        raise DatabaseConfigurationError()
    main = config.get('SQLALCHEMY_DATABASE_URI')
    dialect = database_dialect(main)
    expected = config.get('WUYU_DATABASE_EXPECTED_DIALECT', 'sqlite')
    if expected not in {'sqlite','postgresql'} or dialect != expected:
        raise DatabaseConfigurationError('DATABASE_DIALECT_MISMATCH')
    runtime_engine_options(main)
    bindings = config.get('SQLALCHEMY_BINDS')
    if bindings is not None and not isinstance(bindings, Mapping):
        raise DatabaseConfigurationError('DATABASE_BINDS_INVALID')
    if dialect == 'postgresql':
        if bindings or config.get('WUYU_WORKFLOW_EXECUTION_MODE') not in {'api','worker'}:
            raise DatabaseConfigurationError('DATABASE_POSTGRES_ROLE_INVALID')
        if config.get('WUYU_MIGRATION_PROCESS') == '1':
            raise DatabaseConfigurationError('DATABASE_EXPLICIT_MIGRATOR_REQUIRED')
    for value in (bindings or {}).values():
        if isinstance(value, Mapping):
            if set(value) != {'url'}:
                raise DatabaseConfigurationError('DATABASE_BINDS_INVALID')
            value = value['url']
        if database_dialect(value) != 'sqlite':
            raise DatabaseConfigurationError('DATABASE_BINDS_INVALID')
        runtime_engine_options(value)


def assert_postgres_schema_ready(app, db):
    if database_dialect(app.config['SQLALCHEMY_DATABASE_URI']) != 'postgresql':
        return
    try:
        with app.app_context(), db.engine.connect() as connection:
            if connection.dialect.server_version_info < (16,):
                raise DatabaseConfigurationError('DATABASE_POSTGRES_VERSION_UNSUPPORTED')
            inspector = inspect(connection)
            for table in db.metadata.sorted_tables:
                if not inspector.has_table(table.name, schema=table.schema):
                    raise DatabaseConfigurationError('DATABASE_SCHEMA_NOT_READY')
                columns = {row['name'] for row in inspector.get_columns(table.name, schema=table.schema)}
                if not set(table.columns.keys()) <= columns:
                    raise DatabaseConfigurationError('DATABASE_SCHEMA_NOT_READY')
    except SQLAlchemyError:
        raise DatabaseConfigurationError('DATABASE_SCHEMA_NOT_READY') from None


def safe_database_label(value):
    """Log only a fixed dialect label, never a URL, path or query parameter."""
    try:
        return database_dialect(value)
    except DatabaseConfigurationError:
        return 'invalid'
