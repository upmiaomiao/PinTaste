"""Self-created fixtures only: no database, Docker, credentials or provider IO."""
from contextlib import redirect_stdout,contextmanager
from copy import deepcopy
from pathlib import Path
import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('backup171',Path(__file__).resolve().parents[1]/'staging_pg_backup.py')
B=importlib.util.module_from_spec(spec);spec.loader.exec_module(B)
POSIX=os.name=='posix'


class SequenceTests(unittest.TestCase):
    def case(self,increment=1,called=True,last=10,bounds=(1,10),cycle=False,owned=True):
        definition={'schema':'public','name':'self_seq','increment':increment,'minimum':-1000,'maximum':1000,'cycle':cycle,
                    'table_schema':'public','table_name':'self_table'if owned else None,'column_name':'id'if owned else None}
        state={'schema':'public','name':'self_seq','last_value':last,'is_called':called}
        expected={'sequence_before':[deepcopy(state)],'sequence_after':[deepcopy(state)]}
        actual={'catalog':{'sequences':[definition]}}
        class Session:
            def query(self,sql):
                return {'last_value':state['last_value'],'is_called':state['is_called']}if'last_value'in sql else{'minimum':bounds[0],'maximum':bounds[1]}
        return expected,actual,state,Session()

    def test_positive_step_has_safe_next_value(self):
        expected,actual,_,session=self.case();before=deepcopy((expected,actual))
        result=B.sequence_checks(session,expected,actual)
        self.assertTrue(result[0]['next_value_outside_restored_keys']);self.assertEqual((expected,actual),before)

    def test_negative_step_is_checked_against_minimum(self):
        expected,actual,_,session=self.case(increment=-2,last=-10,bounds=(-10,5))
        self.assertTrue(B.sequence_checks(session,expected,actual)[0]['source_boundary_stable'])

    def test_is_called_false_does_not_increment_before_check(self):
        expected,actual,_,session=self.case(called=False,last=10,bounds=(1,10))
        with self.assertRaisesRegex(B.BackupError,'SEQUENCE_NEXT_VALUE_COLLISION'):B.sequence_checks(session,expected,actual)

    def test_never_called_empty_sequence_is_safe(self):
        expected,actual,_,session=self.case(called=False,last=1,bounds=(None,None))
        self.assertTrue(B.sequence_checks(session,expected,actual)[0]['next_value_outside_restored_keys'])

    def test_cycle_or_unowned_sequence_is_not_claimed_verified(self):
        for options in [{'cycle':True},{'owned':False}]:
            expected,actual,_,session=self.case(**options)
            with self.subTest(options=options),self.assertRaisesRegex(B.BackupError,'SEQUENCE_SHAPE_NOT_VERIFIED'):B.sequence_checks(session,expected,actual)

    def test_advanced_boundary_is_observation_not_same_time_claim(self):
        expected,actual,state,session=self.case(last=11,bounds=(1,8))
        expected['sequence_before'][0]['last_value']=10;expected['sequence_after'][0]['last_value']=12
        self.assertFalse(B.sequence_checks(session,expected,actual)[0]['source_boundary_stable'])

    def test_value_outside_dump_boundary_is_rejected(self):
        expected,actual,state,session=self.case(last=20,bounds=(1,8))
        expected['sequence_before'][0]['last_value']=10;expected['sequence_after'][0]['last_value']=12
        with self.assertRaisesRegex(B.BackupError,'SEQUENCE_STATE_OUTSIDE_SOURCE_BOUNDARY'):B.sequence_checks(session,expected,actual)

    def test_stable_value_but_wrong_called_flag_is_rejected(self):
        expected,actual,state,session=self.case(last=11,bounds=(1,8));state['is_called']=False
        with self.assertRaisesRegex(B.BackupError,'STABLE_SEQUENCE_STATE_MISMATCH'):B.sequence_checks(session,expected,actual)

    def test_exhausted_sequence_is_not_reset(self):
        expected,actual,state,session=self.case(last=1000,bounds=(1,8))
        with self.assertRaisesRegex(B.BackupError,'SEQUENCE_NEXT_VALUE_EXHAUSTED'):B.sequence_checks(session,expected,actual)
        self.assertEqual(state['last_value'],1000)

    def test_hash_order_is_deterministic_and_quote_identifier_is_literal(self):
        self.assertEqual(B.encoded({'b':1,'a':2}),B.encoded({'a':2,'b':1}))
        self.assertEqual(B.ident('name"; --'), '"name""; --"')
        self.assertIn('READ ONLY',Path(B.__file__).read_text())


@unittest.skipUnless(POSIX,'Linux/POSIX permission, flock and pipe tests run in the bounded server fixture')
class PrivateBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='wuyu-backup171-unit-');self.root=Path(self.temp.name)/'private'
        self.store=B.Store(self.root);self.config={'max_managed_bytes':10**7,'min_free_bytes':0,'max_batch_bytes':10**6,
            'targets':deepcopy(B.TARGETS),'pg_image':B.IMAGE,'statement_timeout_ms':60000,'command_timeout_seconds':180,'postgres_version_num':160015}

    def tearDown(self):self.temp.cleanup()

    def successful_target(self,config,target,path,store,started):
        with B.create_file(path)as stream:stream.write(b'PGDMP-self-created-'+target['name'].encode())
        return {'target':target,'bytes':path.stat().st_size,'SHA256':B.file_sha(path),'archive_full_read_passed':True,
                'snapshot_started_at_epoch':time.time()-10,
                'database_bytes':100,'fingerprint':{'catalog':{'sequences':[]},'rows':[]},'sequence_before':[],'sequence_after':[]}

    def test_batch_publishes_both_or_nothing_and_files_are_private(self):
        with patch.object(B,'resources',return_value={}),patch.object(B,'backup_target',side_effect=self.successful_target):
            result=B.backup(self.config,self.store)
        batch=self.root/'batches'/result['batch'];self.assertTrue(batch.is_dir())
        self.assertEqual({path.name for path in batch.iterdir()},{'PPT.dump','QA.dump','manifest.json'})
        for path in batch.iterdir():self.assertEqual(path.stat().st_mode&0o777,0o600)
        self.assertEqual(batch.stat().st_mode&0o777,0o700)
        state=self.store.state();self.assertEqual(state['last_backup']['batch'],result['batch']);self.assertIsNone(state['last_restore'])
        self.assertGreater(state['last_backup']['at_epoch']-state['last_backup']['oldest_snapshot_started_at_epoch'],9)

    def test_second_database_failure_retains_stage_and_old_success(self):
        old={'batch':'old-success','at_epoch':1};self.store.update(last_backup=old)
        def target(*args):
            if args[1]['name']=='QA':raise B.BackupError('PG_COMMAND_FAILED')
            return self.successful_target(*args)
        with patch.object(B,'resources',return_value={}),patch.object(B,'backup_target',side_effect=target),self.assertRaisesRegex(B.BackupError,'PG_COMMAND_FAILED'):
            B.backup(self.config,self.store)
        self.assertFalse((self.root/'batches').exists());self.assertEqual(self.store.state()['last_backup'],old)
        self.assertEqual(len(list((self.root/'.incomplete').glob('*/PPT.dump'))),1)

    def test_disk_floor_failure_is_recorded_without_changing_success(self):
        old={'batch':'old','at_epoch':1};self.store.update(last_backup=old)
        with patch.object(B,'resources',return_value={}),patch.object(self.store,'capacity',side_effect=B.BackupError('DISK_FLOOR_REACHED')),self.assertRaisesRegex(B.BackupError,'DISK_FLOOR_REACHED'):
            B.backup(self.config,self.store)
        state=self.store.state();self.assertEqual(state['last_backup'],old);self.assertEqual(state['last_backup_error']['code'],'DISK_FLOOR_REACHED')

    def test_budget_exhaustion_does_not_delete_old_backup(self):
        path=self.root/'existing.dump'
        with B.create_file(path)as stream:stream.write(b'keep')
        config=dict(self.config,max_managed_bytes=3)
        with self.assertRaisesRegex(B.BackupError,'MANAGED_BUDGET_EXHAUSTED'):self.store.capacity(config)
        self.assertEqual(path.read_bytes(),b'keep')

    def test_busy_lock_does_not_overwrite_running_status(self):
        self.store.update(running={'batch':'other-owned-run'})
        before=(self.root/'status.json').read_bytes()
        with self.store.locked(),self.assertRaisesRegex(B.BackupError,'BACKUP_BUSY'):B.backup(self.config,self.store)
        self.assertEqual((self.root/'status.json').read_bytes(),before)

    def test_atomic_status_replace_failure_keeps_old_bytes(self):
        self.store.update(last_backup={'batch':'old'})
        old=(self.root/'status.json').read_bytes()
        with patch.object(B.os,'replace',side_effect=OSError('synthetic disk full')),self.assertRaises(OSError):self.store.update(last_backup={'batch':'new'})
        self.assertEqual((self.root/'status.json').read_bytes(),old)

    def test_world_readable_or_symlink_directory_is_rejected(self):
        other=Path(self.temp.name)/'public';other.mkdir(mode=0o755)
        with self.assertRaisesRegex(B.BackupError,'PRIVATE_DIRECTORY_MODE'):B.Store(other)
        link=Path(self.temp.name)/'link';link.symlink_to(self.root,target_is_directory=True)
        with self.assertRaisesRegex(B.BackupError,'PATH_SYMLINK_OR_DRIFT'):B.Store(link)

    def test_sensitive_exception_text_is_not_persisted(self):
        marker='SYNTHETIC_PRIVATE_ROW_171'
        with patch.object(B,'resources',return_value={}),patch.object(B,'backup_target',side_effect=RuntimeError(marker)),self.assertRaisesRegex(B.BackupError,'BACKUP_INTERNAL_ERROR'):
            B.backup(self.config,self.store)
        self.assertNotIn(marker,(self.root/'status.json').read_text())

    def test_stderr_is_discarded_without_leaking_secret_marker(self):
        marker='SYNTHETIC_PRIVATE_ROW_171';output=io.StringIO()
        with redirect_stdout(output),self.assertRaisesRegex(B.BackupError,'PG_COMMAND_FAILED'):
            B.job([sys.executable,'-c',"import sys;sys.stderr.write('"+marker+"');sys.exit(3)"],timeout=2)
        self.assertEqual(output.getvalue(),'');self.assertEqual(list(self.root.iterdir()),[])

    def test_metadata_command_timeout_terminates_own_child(self):
        started=time.monotonic()
        with self.assertRaisesRegex(B.BackupError,'COMMAND_TIMEOUT'):B.job([sys.executable,'-c','import time;time.sleep(3)'],timeout=0.1)
        self.assertLess(time.monotonic()-started,2)

    def test_oversized_output_is_not_returned_or_logged(self):
        with self.assertRaisesRegex(B.BackupError,'METADATA_OUTPUT_LIMIT'):
            B.job([sys.executable,'-c',"import sys;sys.stdout.write('x'*1200000)"],timeout=2)

    def test_restore_failure_never_promotes_last_restore(self):
        prior={'batch':'previous-verified','at_epoch':1};self.store.update(last_restore=prior,last_backup={'batch':'keep'})
        with self.assertRaisesRegex(B.BackupError,'RESTORE_INTERNAL_ERROR'):
            with B.restore_failure_state(self.store,'new-batch'):
                self.store.update(last_restore={'batch':'not-yet-cleaned'})
                raise RuntimeError('SYNTHETIC_PRIVATE_ROW_171')
        state=self.store.state();self.assertEqual(state['last_restore'],prior);self.assertEqual(state['last_backup'],{'batch':'keep'})
        self.assertNotIn('SYNTHETIC_PRIVATE_ROW_171',json.dumps(state))

    def test_replayed_admission_is_rejected_before_container_or_manifest_access(self):
        token='a'*32;batch='20261112T000000Z-'+'b'*12
        B.private_dir(self.root/'admissions')
        with B.create_file(self.root/'admissions'/(token+'.used.json'))as stream:stream.write(b'{}')
        with patch.object(B,'resources',return_value={'batch_id':batch,'pg_image':B.IMAGE,'token':token}),patch.object(B,'job')as command,self.assertRaisesRegex(B.BackupError,'RESTORE_GRANT_ALREADY_USED'):
            B.restore_check(self.config,self.store,batch,'root-owned-lease')
        command.assert_not_called()

    def test_wrong_batch_or_image_grant_cannot_create_used_marker(self):
        for grant in [{'batch_id':'wrong','pg_image':B.IMAGE,'token':'a'*32},{'batch_id':'20261112T000000Z-'+'b'*12,'pg_image':'wrong','token':'a'*32}]:
            with patch.object(B,'resources',return_value=grant),self.assertRaisesRegex(B.BackupError,'RESTORE_GRANT_BATCH_OR_IMAGE'):
                B.restore_check(self.config,self.store,'20261112T000000Z-'+'b'*12,'root-owned-lease')
        self.assertFalse((self.root/'admissions').exists())


class OwnedConnectionTests(unittest.TestCase):
    def test_cleanup_only_terminates_exact_owned_pid_start_and_application(self):
        target=deepcopy(B.TARGETS[0]);app='wuyu-bkp171-dump-'+'a'*32
        identity={'pid':123,'start':'2026-11-12 10:00:00+00'};queries=[]
        def sql(_target,query):
            queries.append(query)
            return [identity]if len(queries)==1 else [True]if len(queries)==2 else []
        with patch.object(B,'sql_once',side_effect=sql):result=B.cleanup_connection(target,app,1)
        self.assertTrue(result['verified']);self.assertIn('pid=123',queries[1]);self.assertIn(identity['start'],queries[1])
        self.assertTrue(all('datname=current_database()'in query and 'usename=current_user'in query and app in query for query in queries))

    def test_changed_identity_is_never_terminated(self):
        app='wuyu-bkp171-session-'+'b'*32
        with patch.object(B,'sql_once',return_value=[{'pid':124,'start':'changed'}])as query,self.assertRaisesRegex(B.BackupError,'BACKUP_CONNECTION_IDENTITY_CHANGED'):
            B.cleanup_connection(B.TARGETS[0],app,1,{'pid':123,'start':'original'})
        self.assertEqual(query.call_count,1)

    def test_ambiguous_or_foreign_label_is_never_terminated(self):
        for app,rows,code in [('real-application',[],'BACKUP_CONNECTION_LABEL'),('wuyu-bkp171-dump-'+'a'*32,[{},{}],'BACKUP_CONNECTION_AMBIGUOUS')]:
            with self.subTest(code=code),patch.object(B,'sql_once',return_value=rows)as query,self.assertRaisesRegex(B.BackupError,code):
                B.cleanup_connection(B.TARGETS[0],app,1)
            self.assertFalse(any('terminate_backend'in call.args[1]for call in query.call_args_list))

    def test_no_remaining_connection_is_success_without_termination(self):
        with patch.object(B,'sql_once',return_value=[])as query:
            result=B.cleanup_connection(B.TARGETS[0],'wuyu-bkp171-dump-'+'a'*32,1)
        self.assertEqual(result['terminated_own_connections'],0)
        self.assertFalse(any('terminate_backend'in call.args[1]for call in query.call_args_list))


class JsonRecordFramingTests(unittest.TestCase):
    def session(self,buffer=b'',stdout=None,timeout=1):
        session=object.__new__(B.Session);session.buffer=buffer;session.timeout=timeout
        session.process=SimpleNamespace(stdin=io.BytesIO(),stdout=stdout,poll=lambda:None)
        return session

    @contextmanager
    def channel(self,responses):
        pending=[];sent=[]
        class Input:
            def write(self,value):sent.append(value);pending.extend(responses.pop(0))
            def flush(self):pass
        class Selector:
            def register(self,*_):pass
            def select(self,*_):return [True]if pending else[]
            def close(self):pass
        session=self.session(stdout=SimpleNamespace(fileno=lambda:1234));session.process.stdin=Input()
        with patch.object(B.selectors,'DefaultSelector',side_effect=Selector),patch.object(B.os,'read',side_effect=lambda *_:pending.pop(0)):
            yield session,sent

    def test_composite_aggregate_multiline_json_is_one_record(self):
        raw=b'[{"synthetic_id":1},\n {"synthetic_id":2},\n {"synthetic_id":3}]\0'
        with self.assertRaises(ValueError):json.loads(raw.split(b'\n')[0])
        value,remaining=B.decode_json_record(raw,exact=True)
        self.assertEqual(value,[{'synthetic_id':1},{'synthetic_id':2},{'synthetic_id':3}]);self.assertEqual(remaining,b'')

    def test_escaped_newline_unicode_nul_and_backslash_keep_their_meaning(self):
        expected={'label':'中文','newline':'first\nsecond','nul':'\0','slash':'a\\b','empty':[]}
        raw=json.dumps(expected,ensure_ascii=False,indent=2).encode('utf-8')+b'\0'
        self.assertEqual(raw.count(b'\0'),1);self.assertEqual(B.decode_json_record(raw,exact=True)[0],expected)

    def test_session_rejects_preexisting_record_without_sending_a_query(self):
        session=self.session(b'{"first":[\n1,2\n]}\0["second"]\0')
        with self.assertRaisesRegex(B.BackupError,'PG_METADATA_UNEXPECTED_RECORD'):session.query('SELECT self_created_first')
        self.assertEqual(session.process.stdin.getvalue(),b'')

    def test_two_queries_receive_only_their_own_chunked_responses(self):
        with self.channel([[b'{"first":[\n',b'1,2\n]}\0'],[b'["second"]',b'\0']])as(session,sent):
            self.assertEqual(session.query('SELECT self_created_first'),{'first':[1,2]});self.assertEqual(session.buffer,b'')
            self.assertEqual(session.query('SELECT self_created_second'),['second']);self.assertEqual(session.buffer,b'')
        self.assertEqual(len(sent),2)

    def test_session_rejects_extra_record_in_same_read(self):
        with self.channel([[b'{}\0{}\0']])as(session,_),self.assertRaisesRegex(B.BackupError,'PG_METADATA_EXTRA_RECORDS'):
            session.query('SELECT synthetic')

    def test_unread_extra_record_prevents_next_sql_request(self):
        with self.channel([[b'{}\0',b'{}\0'],[b'[]\0']])as(session,sent):
            self.assertEqual(session.query('SELECT first'),{})
            with self.assertRaisesRegex(B.BackupError,'PG_METADATA_UNEXPECTED_RECORD'):session.query('SELECT second')
        self.assertEqual(len(sent),1)

    def test_empty_record_is_rejected_without_skipping_to_next(self):
        with self.assertRaisesRegex(B.BackupError,'PG_METADATA_EMPTY'):B.decode_json_record(b'\0{}\0')

    def test_exact_response_rejects_extra_frame_or_trailing_bytes(self):
        for raw in [b'{}\0{}\0',b'{}\0\n',b'{}\0x']:
            with self.subTest(raw=raw),self.assertRaisesRegex(B.BackupError,'PG_METADATA_EXTRA_RECORDS'):B.decode_json_record(raw,exact=True)

    def test_unterminated_response_is_rejected_even_when_json_itself_is_valid(self):
        for raw in [b'{}',b'[]',b'{"prefix":']:
            with self.subTest(raw=raw),self.assertRaisesRegex(B.BackupError,'PG_METADATA_TRUNCATED'):B.decode_json_record(raw,exact=True)

    def test_one_mib_record_limit_is_enforced(self):
        allowed=b'"'+b'x'*(B.MAX_JSON_RECORD_BYTES-2)+b'"\0'
        self.assertEqual(len(B.decode_json_record(allowed,exact=True)[0]),B.MAX_JSON_RECORD_BYTES-2)
        with self.assertRaisesRegex(B.BackupError,'PG_METADATA_OUTPUT_LIMIT'):B.decode_json_record(b'"'+b'x'*(B.MAX_JSON_RECORD_BYTES-1)+b'"\0')

    def test_invalid_json_is_not_repaired_by_removing_newlines(self):
        with self.assertRaisesRegex(B.BackupError,'PG_METADATA_INVALID'):B.decode_json_record(b'{"broken":"first\nsecond"}\0')

    def test_sql_once_uses_nul_and_the_same_exact_decoder(self):
        raw=b'[{"first":1},\n {"second":2}]\0'
        with patch.object(B,'job',return_value=(raw,{}))as command:
            self.assertEqual(B.sql_once(B.TARGETS[0],'SELECT synthetic'),[{'first':1},{'second':2}])
        self.assertIn('--record-separator-zero',command.call_args.args[0])

    def test_sql_once_refuses_empty_truncated_and_multiple_records(self):
        for raw,code in [(b'\0','PG_METADATA_EMPTY'),(b'{}','PG_METADATA_TRUNCATED'),(b'{}\0{}\0','PG_METADATA_EXTRA_RECORDS')]:
            with self.subTest(code=code),patch.object(B,'job',return_value=(raw,{})),self.assertRaisesRegex(B.BackupError,code):B.sql_once(B.TARGETS[0],'SELECT synthetic')

    @unittest.skipUnless(POSIX,'POSIX pipe read boundary test')
    def test_session_reads_multiline_unicode_across_arbitrary_pipe_chunks(self):
        expected={'label':'中文','rows':[{'n':1},{'n':2}],'text':'literal\\n and newline\n'}
        raw=json.dumps(expected,ensure_ascii=False,indent=2).encode()+b'\0'
        read_fd,write_fd=os.pipe();reader=os.fdopen(read_fd,'rb')
        sent=threading.Event()
        class Input(io.BytesIO):
            def write(self,value):sent.set();return super().write(value)
        def write():
            try:
                if not sent.wait(timeout=2):return
                for index in range(0,len(raw),3):os.write(write_fd,raw[index:index+3]);time.sleep(0.001)
            finally:os.close(write_fd)
        worker=threading.Thread(target=write);worker.start()
        session=self.session(stdout=reader);session.process.stdin=Input()
        try:self.assertEqual(session.query('SELECT synthetic'),expected)
        finally:reader.close();worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    @unittest.skipUnless(POSIX,'POSIX pipe timeout test')
    def test_session_timeout_never_parses_an_incomplete_record(self):
        read_fd,write_fd=os.pipe();reader=os.fdopen(read_fd,'rb')
        try:
            with self.assertRaisesRegex(B.BackupError,'PG_SESSION_TIMEOUT'):self.session(stdout=reader,timeout=0.02).query('SELECT synthetic')
        finally:reader.close();os.close(write_fd)

    @unittest.skipUnless(POSIX,'POSIX pipe EOF test')
    def test_session_eof_with_partial_record_is_failure(self):
        read_fd,write_fd=os.pipe();reader=os.fdopen(read_fd,'rb')
        class Input(io.BytesIO):
            def write(self,value):
                os.write(write_fd,b'{"partial":');os.close(write_fd);return super().write(value)
        session=self.session(stdout=reader);session.process.stdin=Input()
        try:
            with self.assertRaisesRegex(B.BackupError,'PG_SESSION_CLOSED'):session.query('SELECT synthetic')
        finally:reader.close()


if __name__=='__main__':unittest.main()
