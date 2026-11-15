"""Private staging-only PostgreSQL logical backups and isolated restore evidence.

No retention/deletion, cloud, live restore or arbitrary database targets. Raw
database bytes stay in the private backup root; stderr is hashed then discarded.
"""
from contextlib import contextmanager
from pathlib import Path
import argparse
import hashlib
import importlib.util
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
try:
    import fcntl
except ImportError:
    fcntl = None

DOCKER = '/usr/bin/docker'
ROOT = Path('/opt/wuyu-staging/backups/postgres')
IMAGE = 'sha256:933463a6c8eba2b9f5cd414138738cd8d5519cd2c471214c222652f547a66a32'
TARGETS = [
    {'name':'PPT','container':'wuyu-staging-ppt-postgres91','database':'wuyu_staging_ppt','user':'ppt_admin91','pgpassfile':'/run/secrets/admin.pgpass','data_root':'/opt/wuyu-staging/state/postgres91/data'},
    {'name':'QA','container':'wuyu-staging-qa-postgres111','database':'wuyu_staging_qa','user':'postgres','pgpassfile':None,'data_root':'/opt/wuyu-staging/state/qa-postgres111/pgdata'},
]
BATCH = re.compile(r'\d{8}T\d{6}Z-[0-9a-f]{12}')
MAX_JSON_RECORD_BYTES = 1048576


class BackupError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(value, code):
    if not value:
        raise BackupError(code)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)+'\n').encode('utf-8')


def decode_json_record(data, *, exact=True):
    """psql unaligned NUL record; JSON whitespace and escapes remain untouched."""
    require(b'\0' in data, 'PG_METADATA_TRUNCATED')
    record, remaining = data.split(b'\0', 1)
    require(bool(record), 'PG_METADATA_EMPTY')
    require(len(record) <= MAX_JSON_RECORD_BYTES, 'PG_METADATA_OUTPUT_LIMIT')
    require(not exact or not remaining, 'PG_METADATA_EXTRA_RECORDS')
    try:return json.loads(record), remaining
    except (ValueError, UnicodeError):raise BackupError('PG_METADATA_INVALID')from None


def file_sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def no_links(path):
    path = Path(path)
    require(path.is_absolute() and path.resolve() == path and not any(p.is_symlink() for p in [path, *path.parents]), 'PATH_SYMLINK_OR_DRIFT')
    return path


def private_dir(path):
    path = no_links(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    require(stat.S_IMODE(info.st_mode) == 0o700 and info.st_uid == os.geteuid(), 'PRIVATE_DIRECTORY_MODE')
    return path


def private_file(path):
    path = no_links(path)
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == os.geteuid(), 'PRIVATE_FILE_MODE')
    return path


def create_file(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb')


def sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path, value):
    temporary = path.with_name(path.name+'.pending-'+uuid.uuid4().hex)
    with create_file(temporary) as stream:
        stream.write(encoded(value)); stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path); sync_dir(path.parent)


def read_json(path):
    return json.loads(private_file(path).read_text(encoding='utf-8'))


def load_config(path):
    config = read_json(path)
    expected = {'schema_version','backup_root','resource_module','resource_module_SHA256','pg_image','postgres_version_num',
                'max_managed_bytes','min_free_bytes','max_batch_bytes','command_timeout_seconds','statement_timeout_ms','restore_memory_bytes','targets'}
    require(set(config) == expected and config['schema_version'] == 1 and config['backup_root'] == str(ROOT), 'FIXED_CONFIG_SCHEMA')
    require(config['targets'] == TARGETS and config['pg_image'] == IMAGE and config['postgres_version_num'] == 160015, 'FIXED_DATABASE_TARGETS')
    require(config['max_managed_bytes'] == 20*1024**3 and config['min_free_bytes'] == 10*1024**3
            and config['max_batch_bytes'] == 1024**3 and config['restore_memory_bytes'] == 536870912, 'DISK_OR_MEMORY_POLICY_DRIFT')
    require(config['command_timeout_seconds'] == 180 and config['statement_timeout_ms'] == 60000, 'TIMEOUT_POLICY_DRIFT')
    module = no_links(config['resource_module'])
    require(module.name == 'staging_backup_resources.py' and re.fullmatch('[0-9a-f]{64}', config['resource_module_SHA256'])
            and file_sha(module) == config['resource_module_SHA256'], 'RESOURCE_OWNER_SOURCE_DRIFT')
    return config


def resources(config, admission=None):
    spec = importlib.util.spec_from_file_location('staging_backup_resource_owner', config['resource_module'])
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.resources() if admission is None else module.validate_restore_admission(admission)


class Diagnostic:
    """Only length/hash survive; error text and COPY values never enter a log."""
    def __init__(self, pipe):
        self.bytes = 0; self.digest = hashlib.sha256()
        self.thread = threading.Thread(target=self.drain, args=(pipe,), daemon=True)
        self.thread.start()

    def drain(self, pipe):
        with pipe:
            for chunk in iter(lambda: pipe.read(65536), b''):
                self.bytes += len(chunk); self.digest.update(chunk)

    def result(self):
        self.thread.join(timeout=5)
        require(not self.thread.is_alive(), 'DIAGNOSTIC_DRAIN_TIMEOUT')
        return {'stderr_bytes':self.bytes,'stderr_SHA256':self.digest.hexdigest()}


def job(argv, *, stdin=None, stdout=None, timeout=180, watch=None, input_bytes=None):
    require(stdin is None or input_bytes is None,'COMMAND_STDIN_CONTRACT')
    process = subprocess.Popen(argv, stdin=subprocess.PIPE if input_bytes is not None else stdin,
                               stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE)
    diagnostic = Diagnostic(process.stderr); started = time.monotonic()
    selector = selectors.DefaultSelector()
    try:
        if input_bytes is not None:process.stdin.write(input_bytes);process.stdin.close()
        output = b''; eof = stdout is not None
        if stdout is None: selector.register(process.stdout, selectors.EVENT_READ)
        while process.poll() is None or not eof:
            require(time.monotonic()-started <= timeout, 'COMMAND_TIMEOUT')
            if watch:
                watch()
            if not eof:
                if selector.select(0.1):
                    chunk=os.read(process.stdout.fileno(),65536)
                    if not chunk:eof=True;selector.unregister(process.stdout)
                    else:output+=chunk
                    require(len(output)<=MAX_JSON_RECORD_BYTES+1,'METADATA_OUTPUT_LIMIT')
            else:time.sleep(0.1)
        require(process.returncode == 0, 'PG_COMMAND_FAILED')
        return output, diagnostic.result()
    finally:
        selector.close()
        if process.poll() is None:
            # Only this backup's Docker exec client, never the live DB container.
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
        diagnostic.result()
        if process.stdout is not None:process.stdout.close()


def docker_exec(target, command, *, interactive=True, app_name=None):
    argv = [DOCKER,'exec','--user','999:999']
    if interactive: argv.append('-i')
    if target.get('pgpassfile'): argv += ['-e','PGPASSFILE='+target['pgpassfile']]
    if app_name:
        argv += ['-e','PGAPPNAME='+app_name,'-e','PGOPTIONS=-c statement_timeout=180000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=300000']
    return argv+[target['container'],*command]


def psql_command(target,app_name=None):
    return docker_exec(target, ['psql','--no-password','-X','-q','-A','-t','--record-separator-zero','-v','ON_ERROR_STOP=1',
                                '-h','127.0.0.1','-U',target['user'],'-d',target['database']],app_name=app_name)


def literal(value):return "'"+str(value).replace("'","''")+"'"


def sql_once(target,sql):
    command=psql_command(target,'wuyu-bkp171-control-'+uuid.uuid4().hex)
    raw,_=job(command,input_bytes=("SET statement_timeout='3000ms';SET lock_timeout='1000ms';SET timezone='UTC';"+sql+';\n').encode(),timeout=10)
    return decode_json_record(raw,exact=True)[0]


def cleanup_connection(target,app_name,started,expected=None):
    require(re.fullmatch(r'wuyu-bkp171-(?:session|dump)-[0-9a-f]{32}',app_name),'BACKUP_CONNECTION_LABEL')
    where=('datname=current_database() AND usename=current_user AND application_name='+literal(app_name)
           +' AND pid<>pg_backend_pid() AND backend_start>=to_timestamp('+str(started)+')')
    query="SELECT coalesce(json_agg(json_build_object('pid',pid,'start',backend_start::text)),'[]'::json) FROM pg_stat_activity WHERE "+where
    owned=sql_once(target,query)
    require(isinstance(owned,list)and len(owned)<=1,'BACKUP_CONNECTION_AMBIGUOUS')
    for row in owned:
        require(type(row.get('pid'))is int and row['pid']>0 and isinstance(row.get('start'),str),'BACKUP_CONNECTION_IDENTITY')
        if expected:require(row==expected,'BACKUP_CONNECTION_IDENTITY_CHANGED')
        # Recheck PID+start+database+user+unique application name in the action
        # statement, preventing a recycled PID from targeting an application job.
        sql_once(target,"SELECT coalesce(json_agg(pg_terminate_backend(pid)),'[]'::json) FROM pg_stat_activity WHERE "+where
                 +' AND pid='+str(row['pid'])+' AND backend_start='+literal(row['start'])+'::timestamptz')
    require(sql_once(target,query)==[],'BACKUP_CONNECTION_CLEANUP_UNCONFIRMED')
    return {'verified':True,'terminated_own_connections':len(owned)}


class Session:
    def __init__(self, target, timeout_ms=60000):
        self.target=target;self.app_name='wuyu-bkp171-session-'+uuid.uuid4().hex;self.started=time.time()-2;self.backend_identity=None
        self.process = subprocess.Popen(psql_command(target,self.app_name), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.diagnostic = Diagnostic(self.process.stderr); self.buffer = b''; self.timeout = timeout_ms/1000+10
        try:self.backend_identity=self.query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET LOCAL statement_timeout='60000ms'; "
                   "SET LOCAL lock_timeout='2000ms'; SET LOCAL idle_in_transaction_session_timeout='300000ms'; "
                   "SET LOCAL timezone='UTC'; SET LOCAL datestyle='ISO, YMD'; SET LOCAL intervalstyle='postgres'; SET LOCAL bytea_output='hex'; SET LOCAL extra_float_digits=3; "
                   "SELECT json_build_object('pid',pg_backend_pid(),'start',(SELECT backend_start::text FROM pg_stat_activity WHERE pid=pg_backend_pid()))")
        except BaseException:
            self.close();raise

    def query(self, sql):
        # This interface never pipelines. Pending bytes before a request, or an
        # extra result in the response, must not become the next query's value.
        require(not self.buffer, 'PG_METADATA_UNEXPECTED_RECORD')
        pending=selectors.DefaultSelector();pending.register(self.process.stdout,selectors.EVENT_READ)
        try:
            if pending.select(0):
                old=os.read(self.process.stdout.fileno(),65536)
                require(not old, 'PG_METADATA_UNEXPECTED_RECORD')
                raise BackupError('PG_SESSION_CLOSED')
        finally:pending.close()
        self.process.stdin.write((sql+';\n').encode('utf-8')); self.process.stdin.flush()
        deadline = time.monotonic()+self.timeout
        while b'\0' not in self.buffer:
            require(len(self.buffer) <= MAX_JSON_RECORD_BYTES, 'PG_METADATA_OUTPUT_LIMIT')
            require(self.process.poll() is None, 'PG_SESSION_FAILED')
            selector = selectors.DefaultSelector(); selector.register(self.process.stdout, selectors.EVENT_READ)
            try: ready = selector.select(max(0, deadline-time.monotonic()))
            finally: selector.close()
            require(ready, 'PG_SESSION_TIMEOUT')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            require(bool(chunk), 'PG_SESSION_CLOSED'); self.buffer += chunk
        value, self.buffer = decode_json_record(self.buffer,exact=True)
        return value

    def close(self):
        if self.process.poll() is None:
            try: self.process.stdin.write(b'ROLLBACK;\n\\q\n'); self.process.stdin.flush()
            except (BrokenPipeError, OSError): pass
            self.process.stdin.close()
            try: self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=5)
        diagnostic=self.diagnostic.result()
        self.process.stdout.close()
        diagnostic['connection_cleanup']=cleanup_connection(self.target,self.app_name,self.started,self.backend_identity)
        return diagnostic

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def ident(value):
    return '"'+value.replace('"','""')+'"'


def relation(row):
    return ident(row['schema'])+'.'+ident(row['name'])


CATALOG_SQL = """SELECT json_build_object(
 'tables',(SELECT coalesce(json_agg(x ORDER BY x.schema,x.name),'[]'::json) FROM (
  SELECT n.nspname AS schema,c.relname AS name,c.relkind::text AS kind,c.relpersistence::text AS persistence,
         c.relrowsecurity AS row_security,c.relforcerowsecurity AS force_row_security
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p'))x),
 'columns',(SELECT coalesce(json_agg(x ORDER BY x.schema,x.table_name,x.position),'[]'::json) FROM (
  SELECT n.nspname AS schema,c.relname AS table_name,a.attname AS name,
   row_number() OVER(PARTITION BY c.oid ORDER BY a.attnum) AS position,format_type(a.atttypid,a.atttypmod) AS type,
   a.attnotnull AS not_null,a.attidentity::text AS identity,a.attgenerated::text AS generated,pg_get_expr(d.adbin,d.adrelid) AS default_value
  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema' AND c.relkind IN ('r','p') AND a.attnum>0 AND NOT a.attisdropped)x),
 'constraints',(SELECT coalesce(json_agg(x ORDER BY x.schema,x.table_name,x.name),'[]'::json) FROM (
  SELECT n.nspname AS schema,c.relname AS table_name,k.conname AS name,k.contype::text AS type,
   pg_get_constraintdef(k.oid,true) AS definition,k.convalidated AS validated,k.condeferrable AS deferrable,k.condeferred AS deferred
  FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema')x),
 'indexes',(SELECT coalesce(json_agg(x ORDER BY x.schemaname,x.tablename,x.indexname),'[]'::json) FROM (
  SELECT schemaname,tablename,indexname,indexdef FROM pg_indexes WHERE schemaname !~ '^pg_' AND schemaname<>'information_schema')x),
 'sequences',(SELECT coalesce(json_agg(x ORDER BY x.schema,x.name),'[]'::json) FROM (
  SELECT n.nspname AS schema,c.relname AS name,s.seqstart AS start,s.seqincrement AS increment,s.seqmax AS maximum,
   s.seqmin AS minimum,s.seqcache AS cache,s.seqcycle AS cycle,format_type(s.seqtypid,NULL) AS type,
   tn.nspname AS table_schema,t.relname AS table_name,a.attname AS column_name
  FROM pg_sequence s JOIN pg_class c ON c.oid=s.seqrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  LEFT JOIN pg_depend d ON d.objid=c.oid AND d.classid='pg_class'::regclass AND d.deptype IN ('a','i')
  LEFT JOIN pg_class t ON t.oid=d.refobjid LEFT JOIN pg_namespace tn ON tn.oid=t.relnamespace
  LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
  WHERE n.nspname !~ '^pg_' AND n.nspname<>'information_schema')x))"""


def fingerprint(session):
    catalog = session.query(CATALOG_SQL); rows = []
    for table in catalog['tables']:
        # SHA256 of every canonical row, sorted as hashes, preserves duplicates.
        value = session.query("SELECT json_build_object('count',count(*),'SHA256',encode(sha256(convert_to(coalesce(string_agg(h,'' ORDER BY h COLLATE \"C\"),''),'UTF8')),'hex')) "
                              "FROM(SELECT encode(sha256(convert_to(row_to_json(t)::text,'UTF8')),'hex')h FROM "+relation(table)+" t)x")
        rows.append({'schema':table['schema'],'name':table['name'],**value})
    return {'catalog':catalog,'catalog_SHA256':hashlib.sha256(encoded(catalog)).hexdigest(),'rows':rows}


def sequences(session, catalog):
    return [{'schema':row['schema'],'name':row['name'],**session.query(
        'SELECT json_build_object(\'last_value\',last_value,\'is_called\',is_called) FROM '+relation(row))} for row in catalog['sequences']]


class Store:
    def __init__(self, root):
        self.root = private_dir(root)

    @contextmanager
    def locked(self):
        require(fcntl is not None, 'POSIX_LOCK_REQUIRED')
        path = self.root/'backup.lock'
        fd = os.open(path, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW, 0o600)
        try:
            require(stat.S_IMODE(os.fstat(fd).st_mode)==0o600 and os.fstat(fd).st_uid==os.geteuid(), 'LOCK_MODE')
            try: fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise BackupError('BACKUP_BUSY') from None
            yield
        finally: os.close(fd)

    def state(self):
        path=self.root/'status.json'
        return read_json(path) if path.exists() else {'schema_version':1,'last_backup':None,'last_restore':None}

    def update(self, **fields):
        state=self.state();state.update(fields);atomic_json(self.root/'status.json',state)

    def capacity(self, config, reserve=0):
        size=0
        for parent, dirs, files in os.walk(self.root, followlinks=False):
            for name in [*dirs,*files]: require(not(Path(parent)/name).is_symlink(), 'MANAGED_ROOT_SYMLINK')
            size+=sum((Path(parent)/name).stat().st_size for name in files)
        require(size+reserve <= config['max_managed_bytes'], 'MANAGED_BUDGET_EXHAUSTED')
        require(shutil.disk_usage(self.root).free >= config['min_free_bytes']+reserve, 'DISK_FLOOR_REACHED')
        return size


def target_guard(config,target):
    raw,_=job([DOCKER,'inspect','--format','{"image":{{json .Image}},"running":{{json .State.Running}},"mounts":{{json .Mounts}}}',target['container']])
    info=json.loads(raw)
    require(info['image']==config['pg_image'] and info['running']is True, 'SOURCE_PG_IDENTITY_DRIFT')
    mounts=[m for m in info['mounts']if m['Destination'].startswith('/var/lib/postgresql/data')]
    require(len(mounts)==1 and mounts[0]['Source']==target['data_root'], 'SOURCE_PG_DATA_ROOT_DRIFT')
    job(docker_exec(target,['test','-x','/usr/bin/timeout']))


def backup_target(config,target,path,store,batch_start):
    target_guard(config,target);started=time.time();diagnostics=[]
    with Session(target,config['statement_timeout_ms']) as session:
        meta=session.query("SELECT json_build_object('snapshot',pg_export_snapshot(),'snapshot_started_at_epoch',extract(epoch FROM transaction_timestamp()),'version_num',current_setting('server_version_num'),'database',current_database(),'database_bytes',pg_database_size(current_database()))")
        require(meta['database']==target['database'] and int(meta['version_num'])==config['postgres_version_num'], 'PG_VERSION_OR_DATABASE_DRIFT')
        source=fingerprint(session);before=sequences(session,source['catalog'])
        def watch():
            require(time.monotonic()-batch_start<840, 'BACKUP_DEADLINE')
            require(path.stat().st_size<=config['max_batch_bytes'], 'BATCH_SIZE_LIMIT')
            store.capacity(config)
        dump_app='wuyu-bkp171-dump-'+uuid.uuid4().hex;dump_started=time.time()-2
        try:
            with create_file(path) as stream:
                _,diag=job(docker_exec(target,['/usr/bin/timeout','--signal=TERM','--kill-after=5s',str(config['command_timeout_seconds']),
                    'pg_dump','--no-password','-h','127.0.0.1','-U',target['user'],'-d',target['database'],
                    '--format=custom','--compress=6','--lock-wait-timeout=2000','--snapshot='+meta['snapshot']],app_name=dump_app),stdout=stream,timeout=config['command_timeout_seconds'],watch=watch)
                stream.flush();os.fsync(stream.fileno());diagnostics.append(diag)
        finally:diagnostics.append({'dump_connection_cleanup':cleanup_connection(target,dump_app,dump_started)})
        after=sequences(session,source['catalog'])
    require(0<path.stat().st_size<=config['max_batch_bytes'], 'DUMP_SIZE_INVALID')
    with path.open('rb') as stream:
        require(stream.read(5)==b'PGDMP', 'DUMP_HEADER_INVALID')
    # Scan/decompress the entire archive without executing SQL or returning it.
    with path.open('rb') as stream, open(os.devnull,'wb') as sink:
        _,diag=job(docker_exec(target,['pg_restore','--file=/dev/null']),stdin=stream,stdout=sink,timeout=config['command_timeout_seconds'])
        diagnostics.append(diag)
    return {'target':target,'started_at_epoch':started,'snapshot_started_at_epoch':meta['snapshot_started_at_epoch'],'finished_at_epoch':time.time(),'snapshot':meta['snapshot'],
        'postgres_version_num':int(meta['version_num']),'database_bytes':meta['database_bytes'],'bytes':path.stat().st_size,'SHA256':file_sha(path),
        'archive_full_read_passed':True,'fingerprint':source,'sequence_before':before,'sequence_after':after,'diagnostics':diagnostics}


def backup(config,store):
    with store.locked():
        batch=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime())+'-'+uuid.uuid4().hex[:12]
        store.update(running={'action':'backup','batch':batch,'started_at_epoch':time.time()})
        try:
            resources(config);store.capacity(config,config['max_batch_bytes'])
            stage=private_dir(private_dir(store.root/'.incomplete')/batch)
            started=time.monotonic();targets=[]
            for target in config['targets']:
                targets.append(backup_target(config,target,stage/(target['name']+'.dump'),store,started))
            require(sum(row['bytes']for row in targets)<=config['max_batch_bytes'], 'BATCH_SIZE_LIMIT')
            manifest={'schema_version':1,'batch':batch,'created_at_epoch':time.time(),'module_SHA256':file_sha(Path(__file__).resolve()),
                'consistency':'independent_per_database_exported_snapshots','files_sessions_cross_cluster_atomic_point':False,
                'owner_acl_metadata_retained':True,'verification_level':'full_archive_read_not_yet_restore_tested','targets':targets}
            atomic_json(stage/'manifest.json',manifest);sync_dir(stage)
            destination=private_dir(store.root/'batches')/batch
            require(not destination.exists(), 'BATCH_ALREADY_EXISTS');os.rename(stage,destination);sync_dir(destination.parent)
            receipt={'batch':batch,'at_epoch':time.time(),'manifest_SHA256':file_sha(destination/'manifest.json'),
                     'oldest_snapshot_started_at_epoch':min(row['snapshot_started_at_epoch']for row in targets),
                     'dump_bytes':sum(row['bytes']for row in targets),'verification_level':'full_archive_read'}
            store.update(running=None,last_backup=receipt,last_backup_error=None)
            return {'passed':True,'action':'backup',**receipt,'databases':len(targets)}
        except BaseException as error:
            code=error.code if isinstance(error,BackupError) else 'BACKUP_INTERNAL_ERROR'
            store.update(running=None,last_backup_error={'batch':batch,'code':code,'at_epoch':time.time()})
            raise BackupError(code) from None


def sequence_checks(session,expected,restored):
    before={(row['schema'],row['name']):row for row in expected['sequence_before']}
    after={(row['schema'],row['name']):row for row in expected['sequence_after']}
    current={(row['schema'],row['name']):row for row in sequences(session,restored['catalog'])};verified=[]
    for definition in restored['catalog']['sequences']:
        key=(definition['schema'],definition['name']);value=current[key];start=before[key];end=after[key]
        require(not definition['cycle'] and definition['increment']!=0 and definition['table_name'] and definition['column_name'], 'SEQUENCE_SHAPE_NOT_VERIFIED')
        lo,hi=sorted([start['last_value'],end['last_value']]);require(lo<=value['last_value']<=hi, 'SEQUENCE_STATE_OUTSIDE_SOURCE_BOUNDARY')
        if start==end:require(value==start, 'STABLE_SEQUENCE_STATE_MISMATCH')
        next_value=value['last_value']+(definition['increment'] if value['is_called'] else 0)
        require(definition['minimum']<=next_value<=definition['maximum'], 'SEQUENCE_NEXT_VALUE_EXHAUSTED')
        table=ident(definition['table_schema'])+'.'+ident(definition['table_name']);column=ident(definition['column_name'])
        bounds=session.query('SELECT json_build_object(\'minimum\',min('+column+'),\'maximum\',max('+column+')) FROM '+table)
        require(bounds['maximum']is None or (next_value>bounds['maximum'] if definition['increment']>0 else next_value<bounds['minimum']), 'SEQUENCE_NEXT_VALUE_COLLISION')
        verified.append({'schema':key[0],'name':key[1],'source_boundary_stable':start==end,'restored_state_within_boundary':True,'next_value_outside_restored_keys':True})
    return verified


@contextmanager
def restore_failure_state(store,batch):
    prior=store.state().get('last_restore')
    try:yield
    except BaseException as error:
        code=error.code if isinstance(error,BackupError)else'RESTORE_INTERNAL_ERROR'
        store.update(running=None,last_restore=prior,last_restore_error={'batch':batch,'code':code,'at_epoch':time.time()})
        raise BackupError(code)from None


def restore_check(config,store,batch,admission_path):
    require(BATCH.fullmatch(batch), 'BATCH_ID_INVALID')
    with store.locked(),restore_failure_state(store,batch):
        admission=resources(config,admission_path)
        require(admission['batch_id']==batch and admission['pg_image']==config['pg_image'], 'RESTORE_GRANT_BATCH_OR_IMAGE')
        token=admission['token'];require(re.fullmatch('[0-9a-f]{32}',token), 'RESTORE_GRANT_TOKEN')
        used=private_dir(store.root/'admissions')/(token+'.used.json')
        try:
            with create_file(used) as stream:stream.write(encoded({'batch':batch,'at_epoch':time.time()}));stream.flush();os.fsync(stream.fileno())
        except FileExistsError:raise BackupError('RESTORE_GRANT_ALREADY_USED')from None
        directory=no_links(store.root/'batches'/batch);manifest=read_json(directory/'manifest.json')
        require(manifest['batch']==batch and len(manifest['targets'])==2, 'BATCH_MANIFEST_INVALID')
        for row in manifest['targets']:
            dump=private_file(directory/(row['target']['name']+'.dump'))
            require(dump.stat().st_size==row['bytes']and file_sha(dump)==row['SHA256'], 'DUMP_BYTES_CHANGED')
        store.capacity(config,max(1024**3,3*sum(row['database_bytes']for row in manifest['targets'])))
        run=private_dir(private_dir(store.root/'restore-checks')/token);data=private_dir(run/'pgdata');os.chown(data,999,999)
        name='wuyu-staging-pg-restore171-'+token[:12];identity=None;created=None;results=[]
        store.update(running={'action':'restore-check','batch':batch,'token':token,'started_at_epoch':time.time()})
        try:
            raw,_=job([DOCKER,'create','--pull','never','--name',name,'--user','999:999','--network','none','--read-only',
                '--memory',str(config['restore_memory_bytes']),'--memory-swap',str(config['restore_memory_bytes']),'--cpus','1','--pids-limit','128',
                '--cap-drop','ALL','--security-opt','no-new-privileges','--label','wuyu.operation=staging-pg-backup-171-restore','--label','wuyu.restore_token='+token,
                '--tmpfs','/tmp:rw,size=16m,mode=1777','--tmpfs','/var/run/postgresql:rw,size=4m,mode=1777',
                '--mount','type=bind,src='+str(data)+',dst=/var/lib/postgresql/data',
                '-e','POSTGRES_USER=backup_restore','-e','POSTGRES_HOST_AUTH_METHOD=trust',config['pg_image'],
                '-c','shared_buffers=32MB','-c','work_mem=4MB','-c','maintenance_work_mem=64MB','-c','max_connections=8',
                '-c','max_wal_size=64MB','-c','min_wal_size=32MB','-c','wal_level=minimal','-c','max_wal_senders=0'])
            identity=raw.decode().strip();require(re.fullmatch('[0-9a-f]{64}',identity), 'RESTORE_CONTAINER_ID')
            raw,_=job([DOCKER,'inspect','--format','{{json .}}',identity]);info=json.loads(raw)
            require(info['Image']==config['pg_image'] and info['Name']=='/'+name and info['Config']['Labels']['wuyu.restore_token']==token
                    and info['HostConfig']['NetworkMode']=='none'and not info['HostConfig']['PortBindings']and not info['HostConfig']['Privileged']
                    and info['HostConfig']['ReadonlyRootfs']is True and info['HostConfig']['Memory']==config['restore_memory_bytes'], 'RESTORE_CONTAINER_BOUNDARY')
            binds=[value for value in info['Mounts']if value['Type']=='bind']
            require(len(binds)==1 and binds[0]['Source']==str(data) and binds[0]['Destination']=='/var/lib/postgresql/data', 'RESTORE_LIVE_MOUNT_FORBIDDEN')
            created={'id':identity,'name':name,'image':info['Image'],'labels':info['Config']['Labels'],'data_root':str(data),
                     'network':'none','memory':config['restore_memory_bytes'],'live_mounts':0}
            atomic_json(run/'container.json',created);job([DOCKER,'start',identity])
            ready=False
            for _ in range(60):
                try:job([DOCKER,'exec','--user','999:999',identity,'pg_isready','-U','backup_restore'],timeout=3);ready=True;break
                except BackupError:time.sleep(0.5)
            require(ready, 'RESTORE_START_TIMEOUT')
            for row in manifest['targets']:
                db='restore_'+row['target']['name'].lower()
                job([DOCKER,'exec','--user','999:999',identity,'createdb','-U','backup_restore',db])
                with (directory/(row['target']['name']+'.dump')).open('rb') as stream, open(os.devnull,'wb') as sink:
                    job([DOCKER,'exec','--user','999:999','-i',identity,'pg_restore','-U','backup_restore','--no-owner','--no-acl',
                         '--exit-on-error','--single-transaction','-d',db],stdin=stream,stdout=sink,timeout=180)
                target={'container':identity,'user':'backup_restore','database':db,'pgpassfile':None}
                with Session(target) as session:
                    version=session.query("SELECT json_build_object('version_num',current_setting('server_version_num'))")
                    require(int(version['version_num'])==row['postgres_version_num'], 'RESTORE_PG_VERSION')
                    actual=fingerprint(session)
                    require(actual==row['fingerprint'], 'RESTORE_SCHEMA_ROWS_CONSTRAINTS_MISMATCH')
                    seq=sequence_checks(session,row,actual)
                results.append({'target':row['target']['name'],'schema_rows_constraints_equal':True,'tables':len(actual['rows']),
                                'sequences':seq,'role_owner_acl_execution_verified':False})
            result={'passed':True,'batch':batch,'at_epoch':time.time(),'container':created,'targets':results,
                    'files_sessions_cross_cluster_atomic_point':False,'owner_acl_metadata_retained_not_execution_verified':True}
            atomic_json(run/'result.json',result)
            receipt={'batch':batch,'at_epoch':result['at_epoch'],'report_SHA256':file_sha(run/'result.json'),'databases':2,
                     'tables':sum(row['tables']for row in results),'sequences':sum(len(row['sequences'])for row in results)}
        except BaseException as error:
            code=error.code if isinstance(error,BackupError)else'RESTORE_INTERNAL_ERROR'
            atomic_json(run/'failure.json',{'passed':False,'batch':batch,'code':code,'at_epoch':time.time(),'container':created})
            raise BackupError(code)from None
        finally:
            if identity:
                raw,_=job([DOCKER,'inspect','--format','{{json .}}',identity]);info=json.loads(raw)
                require(info['Id']==identity and info['Name']=='/'+name and info['Image']==config['pg_image']
                        and info['Config']['Labels'].get('wuyu.restore_token')==token, 'RESTORE_CLEANUP_IDENTITY')
                job([DOCKER,'rm','--force',identity])
                atomic_json(run/'container-removed.json',{'id':identity,'data_preserved':True,'at_epoch':time.time()})
        store.update(running=None,last_restore=receipt,last_restore_error=None)
        return {'passed':True,'action':'restore-check',**receipt}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True)
    sub=parser.add_subparsers(dest='action',required=True);sub.add_parser('backup');sub.add_parser('status')
    restore=sub.add_parser('restore-check');restore.add_argument('--batch',required=True);restore.add_argument('--admission',required=True)
    args=parser.parse_args();os.umask(0o077)
    def interrupted(*_):raise BackupError('OPERATION_INTERRUPTED')
    signal.signal(signal.SIGTERM,interrupted)
    try:
        config=load_config(args.config);store=Store(Path(config['backup_root']))
        if args.action=='backup':result=backup(config,store)
        elif args.action=='restore-check':result=restore_check(config,store,args.batch,args.admission)
        else:
            result=store.state();now=time.time()
            for key in ['last_backup','last_restore']:
                moment=(result.get(key)or{}).get('oldest_snapshot_started_at_epoch'if key=='last_backup'else'at_epoch')
                result[key+'_age_seconds']=None if moment is None else round(now-moment)
        print(json.dumps(result,sort_keys=True));return 0
    except BaseException as error:
        code=error.code if isinstance(error,BackupError)else'BACKUP_INTERNAL_ERROR'
        print(json.dumps({'passed':False,'action':args.action,'code':code}));return 75 if code=='BACKUP_BUSY'else 1


if __name__=='__main__':sys.exit(main())
