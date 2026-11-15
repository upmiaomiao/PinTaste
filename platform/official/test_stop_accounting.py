"""Manual-stop quota contracts, synthetic events only, no model requests."""
import asyncio
import copy
import json
import os
import unittest
from unittest.mock import patch

import httpx

from official import store as db
from official.jobs import reduce_event, settle_accounting, public_turn, Scheduler
from official.chat_accounting import KEY
from official import test_failure_accounting as accounting_fixture
from official import test_model_budget as admission_fixture
from official.test_pg_worker_regressions import _PostgresFixtureMixin


def request(*, tools=False, image=False):
    content = [{'type': 'image_url', 'image_url': {'url': 'data:fixture'}}] if image else 'Self-created prompt'
    return {'type': 'model_request', 'round': 1, 'attempt': 1,
            'payload': {'messages': [{'role': 'user', 'content': content}], 'max_tokens': 160000,
                        **({'tools': [{'type': 'function'}]} if tools else {})},
            'context_budget': {'input_tokens': 512, 'count_method': 'multimodal_estimate' if image else 'estimate'}}


def turn_from(events, status='stopped'):
    turn={'answer':'','reasoning':'','toolCalls':[],'status':status}
    for event in events:reduce_event(turn,copy.deepcopy(event))
    turn['status']=status
    return turn


class StopEvidenceTests(unittest.TestCase):
    def account(self, events, status='stopped'):
        turn=turn_from(events,status);return settle_accounting(turn,1040000),turn

    def test_reasoning_only_770_characters_does_not_charge_full_reservation(self):
        value,turn=self.account([request(),{'type':'reasoning','text':'中'*770}])
        self.assertEqual(value,{'tokens':1667,'estimated':True,'basis':'stopped_observed_estimate'})
        self.assertNotIn(KEY,turn)

    def test_both_streams_accumulate_despite_tool_plan_display_reset(self):
        value,_=self.account([request(),{'type':'content','text':'abc'}, {'type':'reasoning','text':'中中'}])
        self.assertEqual(value['tokens'],516)
        value,_=self.account([request(),{'type':'content','text':'abc'},{'type':'tool_plan'}, {'type':'reasoning','text':'中中'}])
        self.assertEqual(value['tokens'],1040000)

    def test_zero_output_and_untraced_old_turn_keep_reservation(self):
        for events in ([request()], [{'type':'content','text':'Old partial'}], []):
            self.assertEqual(self.account(events)[0]['tokens'],1040000)

    def test_tool_or_multimodal_unfinished_use_keeps_reservation(self):
        for first in (request(tools=True),request(image=True)):
            self.assertEqual(self.account([first,{'type':'content','text':'Partial'}])[0]['tokens'],1040000)

    def test_checkpoint_is_cumulative_not_added_twice(self):
        events=[request(),{'type':'usage','estimated':False,'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}},
                request(),{'type':'usage','estimated':False,'usage':{'prompt_tokens':250,'completion_tokens':50,'total_tokens':300}}]
        value,_=self.account(events)
        self.assertEqual(value,{'tokens':300,'estimated':False,'basis':'stopped_reported_usage'})

    def test_new_request_invalidates_old_cumulative_coverage(self):
        value,_=self.account([request(),{'type':'usage','estimated':False,'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}},request(),{'type':'content','text':'new'}])
        self.assertEqual(value['tokens'],1040000)

    def test_estimated_usage_is_never_claimed_exact(self):
        value,_=self.account([request(),{'type':'usage','estimated':True,'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}}])
        self.assertEqual(value,{'tokens':120,'estimated':True,'basis':'stopped_round_budget_estimate'})

    def test_estimated_prefix_cannot_be_upgraded_to_exact_by_inconsistent_flag(self):
        value,_=self.account([request(),{'type':'usage','estimated':True,'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}},
                             request(),{'type':'usage','estimated':False,'usage':{'prompt_tokens':200,'completion_tokens':40,'total_tokens':240}}])
        self.assertEqual(value['tokens'],1040000)

    def test_replayed_old_checkpoint_cannot_cover_a_new_request(self):
        usage={'type':'usage','estimated':False,'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}}
        value,_=self.account([request(),usage,request(),usage])
        self.assertEqual(value['tokens'],1040000)
        # Repeating a checkpoint for the same request still counts only once.
        self.assertEqual(self.account([request(),usage,usage])[0]['tokens'],120)

    def test_invalid_usage_types_or_totals_keep_reservation(self):
        for usage,estimated in [({'prompt_tokens':1,'completion_tokens':2,'total_tokens':True},False),
                ({'prompt_tokens':1,'completion_tokens':2,'total_tokens':4},False),
                ({'prompt_tokens':1,'completion_tokens':2,'total_tokens':3},None),
                (None,False), (['invalid'],False)]:
            value,_=self.account([request(),{'type':'content','text':'observed'}, {'type':'usage','usage':usage,'estimated':estimated}])
            self.assertEqual(value['tokens'],1040000)

    def test_context_budget_must_be_valid_and_plain(self):
        for change in ({'input_tokens':True},{'input_tokens':-1},{'count_method':'unknown'}):
            first=request();first['context_budget'].update(change)
            self.assertEqual(self.account([first,{'type':'content','text':'observed'}])[0]['tokens'],1040000)

    def test_estimate_is_bounded_by_output_and_task_limits(self):
        first=request();first['payload']['max_tokens']=2
        value,_=self.account([first,{'type':'content','text':'中'*1000}])
        self.assertEqual(value['tokens'],514)

    def test_error_timeout_and_restart_do_not_gain_stop_policy(self):
        for status in ('error','interrupted'):
            self.assertEqual(self.account([request(),{'type':'content','text':'partial'}],status)[0]['tokens'],1040000)

    def test_public_snapshot_hides_scalar_ledger_before_terminal_settlement(self):
        turn=turn_from([request(),{'type':'content','text':'partial'}], 'running')
        view=public_turn({'turn':json.dumps(turn),'status':'running','revision':1,'input':'{}'})
        self.assertNotIn(KEY,view)


class StopSchedulerTests(unittest.IsolatedAsyncioTestCase):
    setUp=accounting_fixture.FailureAccountingTests.setUp
    tearDown=accounting_fixture.FailureAccountingTests.tearDown
    row=accounting_fixture.FailureAccountingTests.row
    run_events=accounting_fixture.FailureAccountingTests.run_events

    async def test_cancelled_run_persists_estimate_in_job_and_end_event(self):
        row,turn=await self.run_events([request(),{'type':'reasoning','text':'中'*770},asyncio.CancelledError()])
        self.assertEqual((row['status'],row['charged']),('stopped',1667))
        self.assertEqual(turn['accounting']['basis'],'stopped_observed_estimate')
        end=json.loads(db.query('SELECT event FROM job_events WHERE job_id=? ORDER BY sequence DESC',('job',),one=True)['event'])
        self.assertEqual(end['accounting'],turn['accounting'])
        self.assertNotIn(KEY,turn)

    async def test_normal_unknown_failure_still_keeps_101_policy(self):
        row,_=await self.run_events([request(),{'type':'content','text':'partial'},TimeoutError()])
        self.assertEqual(row['charged'],1040000)

    async def test_real_agent_request_shape_and_reasoning_stream_are_eligible(self):
        import agent_stream
        from server import ChatRequest
        row=self.row();requests=[]
        original_client=httpx.AsyncClient
        wire='data: '+json.dumps({'choices':[{'index':0,'delta':{'reasoning_content':'中'*770}}]},ensure_ascii=False)+'\n\n'
        transport=httpx.MockTransport(lambda request:httpx.Response(200,text=wire,headers={'Content-Type':'text/event-stream'}))
        def factory(*args,**kwargs):
            return original_client(*args,**dict(kwargs,transport=transport))
        body=ChatRequest(provider='team',messages=[{'role':'user','content':'Self-created ordinary question'}],
                         system='Fixture only',max_tokens=2048,thinking=True,network_search=False)
        async def runner(row,output):
            async for raw in agent_stream.run_agent(body,'self-fixture-key',provider_config={
                    'model':'synthetic','base_url':'https://fixture.invalid/v1','context_window':32768},
                    total_token_budget=row['reserved'],capture_trace=True):
                event=json.loads(raw[6:])
                if event['type']=='model_request':requests.append(copy.deepcopy(event))
                yield event
                if event['type']=='reasoning':raise asyncio.CancelledError()
        with patch.object(agent_stream.httpx,'AsyncClient',side_effect=factory):
            await Scheduler(runner).run(row,False)
        saved=db.query('SELECT * FROM jobs WHERE id=?',('job',),one=True)
        self.assertEqual(saved['status'],'stopped')
        self.assertEqual(len(requests),1)
        self.assertEqual(saved['charged'],requests[0]['context_budget']['input_tokens']+1155)
        self.assertLess(saved['charged'],1040000)


class AdmissionMessagesTests(unittest.TestCase):
    setUp=admission_fixture.ModelBudgetAdmissionTests.setUp
    tearDown=admission_fixture.ModelBudgetAdmissionTests.tearDown
    new_user=admission_fixture.ModelBudgetAdmissionTests.new_user
    conversation=admission_fixture.ModelBudgetAdmissionTests.conversation
    submit=admission_fixture.ModelBudgetAdmissionTests.submit
    job=admission_fixture.ModelBudgetAdmissionTests.job

    def test_pending_reservation_gets_occupancy_message(self):
        self.job(self.submit(model_id='team'))
        response=self.submit(model_id='team')
        self.assertEqual(response.status_code,429)
        self.assertIn('已有任务正在预留',response.json()['detail'])

    def test_known_stop_releases_enough_for_next_main_model_question(self):
        row=self.job(self.submit(model_id='team'))
        async def runner(row,output):
            yield request()
            yield {'type':'reasoning','text':'中'*770}
            raise asyncio.CancelledError()
        asyncio.run(Scheduler(runner).run(row,False))
        saved=db.query('SELECT * FROM jobs WHERE id=?',(row['id'],),one=True)
        self.assertEqual(saved['charged'],1667)
        self.assertEqual(self.submit(model_id='team').status_code,200)

    def test_committed_daily_use_is_not_described_as_temporary(self):
        row=self.job(self.submit(model_id='team'))
        db.execute("UPDATE jobs SET status='stopped',charged=1040000 WHERE id=?",(row['id'],))
        response=self.submit(model_id='team')
        self.assertEqual(response.status_code,429)
        self.assertIn('今日可用额度不足',response.json()['detail'])

    def test_question_cap_is_identified_separately(self):
        db.save_settings({'daily_questions':1},'fixture')
        self.job(self.submit())
        response=self.submit()
        self.assertEqual(response.status_code,429)
        self.assertIn('提问次数',response.json()['detail'])

    def test_impossible_task_reservation_is_not_tomorrow_advice(self):
        db.save_settings({'daily_tokens':1000},'fixture')
        response=self.submit(model_id='team')
        self.assertEqual(response.status_code,429)
        self.assertIn('超过账号日额度',response.json()['detail'])


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'),'Real isolated PG required')
class PGStopSchedulerTests(_PostgresFixtureMixin,StopSchedulerTests):
    pass


@unittest.skipUnless(os.environ.get('WUYU_PG_TEST_DSN'),'Real isolated PG required')
class PGAdmissionMessagesTests(_PostgresFixtureMixin,AdmissionMessagesTests):
    pass


if __name__=='__main__':unittest.main()
