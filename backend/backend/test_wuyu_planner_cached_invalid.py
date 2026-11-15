"""Real Pipeline, synthetic outlines, private temporary cache, and fake gateway only."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from services import wuyu_pipeline as module
from services.wuyu_errors import PipelineError

NEW_CODE='planner_cached_plan_invalid'
VALID={'slide_specs':[{'number':1,'title':'Fixture flow','kind':'content',
 'copy':'Receive and review supplied material','points':['Receive','Review'],
 'speaker_notes':'Synthetic fixture only','evidence_ids':[],'data_ids':[],
 'source_asset_ids':[],'source_asset_views':[],'layout':{'structure':'flow','intent':'fixture'}}]}

class FakeGateway:
    models={'planner':'synthetic-planner','image':'synthetic-image','trace':'synthetic-trace'}
    def __init__(self,raw):
        self.raw=raw;self.calls=0;self.last_response_metadata={'role':'planner','finish_reason':'stop'}
    def planner_effective_parameters(self):return {'max_tokens':8192}
    def text(self,role,prompt,**kwargs):
        assert role=='planner';self.calls+=1;return self.raw


class PlannerInvalidCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.pipeline=module.WuyuPipeline.__new__(module.WuyuPipeline)
        self.pipeline.config={'TESTING':True,'UPLOAD_FOLDER':str(self.root),
            'WUYU_CLIENT_FACTORY':True,'WUYU_PLANNER_CONTEXT_TOKENS':65536,'WUYU_PLANNER_OUTPUT_TOKENS':8192}
        self.pipeline.demo=False;self.pipeline.gateway=FakeGateway(json.dumps(VALID))
        self.pipeline._hybrid_page_qa_cache={}
        self.recipe={'project_id':str(uuid.uuid4())}
        self.brief={'topic':'Synthetic fixture','creation_kind':'pages','page_count':1,'language':'en'}

    def tearDown(self):self.temp.cleanup()
    def run_outline(self):return self.pipeline.create_outline(deepcopy(self.brief),[],deepcopy(self.recipe))
    def files(self):
        folders=list((self.root/self.recipe['project_id']/'wuyu/planning').iterdir())
        self.assertEqual(len(folders),1)
        return folders[0]/'response.json',folders[0]/'metadata.json'
    def seed(self,raw,code=None):
        self.pipeline.gateway.raw=raw
        if code:
            with self.assertRaises(PipelineError)as caught:self.run_outline()
            self.assertEqual(caught.exception.code,code)
        else:self.run_outline()
        self.assertEqual(self.pipeline.gateway.calls,1)
        return self.files()
    def repeat_rejected(self,original_code):
        response,meta=self.files();before=(response.read_bytes(),meta.read_bytes()if meta.exists()else None)
        with self.assertRaises(PipelineError)as caught:self.run_outline()
        error=caught.exception
        self.assertEqual(error.code,NEW_CODE)
        self.assertFalse(error.retryable)
        self.assertEqual(error.diagnostics['validation_code'],original_code)
        self.assertEqual(error.cause_code,original_code)
        self.assertIs(error.diagnostics['response_reused'],True)
        self.assertIs(error.diagnostics['provider_called'],False)
        self.assertEqual(self.pipeline.gateway.calls,1)
        self.assertEqual((response.read_bytes(),meta.read_bytes()if meta.exists()else None),before)
        self.assertNotIn('raw_text',error.to_dict())
        return error

    def test_first_invalid_retains_original_code_and_saves_raw(self):
        value=deepcopy(VALID);value['slide_specs'][0].pop('layout')
        raw=json.dumps(value);response,meta=self.seed(raw,'invalid_slide_spec')
        self.assertEqual(json.loads(response.read_text())['raw_text'],raw)
        saved=json.loads(meta.read_text());self.assertEqual(saved['status'],'validation_failed')
        self.assertEqual(saved['error_code'],'invalid_slide_spec')
        self.assertTrue(saved['provider_called'])

    def test_repeated_invalid_spec_is_explicit_without_model_or_file_rewrite(self):
        value=deepcopy(VALID);value['slide_specs'][0].pop('layout')
        self.seed(json.dumps(value),'invalid_slide_spec');self.repeat_rejected('invalid_slide_spec')
        self.repeat_rejected('invalid_slide_spec')

    def test_invalid_json_repeat_is_explicit_and_original_raw_retained(self):
        self.seed('{not-json','invalid_model_json');self.repeat_rejected('invalid_model_json')

    def test_wrong_page_count_repeat_is_explicit(self):
        self.seed(json.dumps({'slide_specs':[]}), 'outline_page_count');self.repeat_rejected('outline_page_count')

    def test_unknown_evidence_id_is_dropped_not_fatal(self):
        # op 176: one invented reference is dropped instead of voiding the whole outline.
        value=deepcopy(VALID);value['slide_specs'][0]['evidence_ids']=['not-owned']
        self.seed(json.dumps(value))

    def test_model_invented_data_repeat_is_explicit(self):
        value=deepcopy(VALID);value['slide_specs'][0]['structured_data']={'rows':[['invented']]}
        self.seed(json.dumps(value),'untrusted_structured_data');self.repeat_rejected('untrusted_structured_data')

    def test_unknown_data_id_is_dropped_not_fatal(self):
        # op 176: an unknown data ID is dropped; no model data is trusted.
        value=deepcopy(VALID);value['slide_specs'][0]['data_ids']=['not-owned']
        self.seed(json.dumps(value))

    def test_model_invented_asset_record_repeat_is_explicit(self):
        value=deepcopy(VALID);value['slide_specs'][0]['source_assets']=[{'path':'untrusted'}]
        self.seed(json.dumps(value),'untrusted_source_asset');self.repeat_rejected('untrusted_source_asset')

    def test_unsupported_number_guard_stays_effective_on_replay(self):
        value=deepcopy(VALID);value['slide_specs'][0]['copy']='Observed recovery reached 984321.76 percent.'
        self.seed(json.dumps(value),'unsupported_numeric_claim');self.repeat_rejected('unsupported_numeric_claim')

    def test_valid_cache_remains_reusable(self):
        response,meta=self.seed(json.dumps(VALID));raw=response.read_bytes()
        output=self.run_outline()
        self.assertEqual(output['slide_specs'][0]['title'],'Fixture flow')
        self.assertEqual(self.pipeline.gateway.calls,1)
        self.assertEqual(response.read_bytes(),raw)
        self.assertEqual(json.loads(meta.read_text())['status'],'validated')

    def test_old_invalid_marker_does_not_block_now_valid_raw(self):
        response,meta=self.seed(json.dumps(VALID));raw=response.read_bytes()
        old=json.loads(meta.read_text());old.update(status='validation_failed',error_code='invalid_slide_spec')
        meta.write_text(json.dumps(old))
        output=self.run_outline()
        self.assertEqual(output['slide_specs'][0]['title'],'Fixture flow')
        self.assertEqual(self.pipeline.gateway.calls,1)
        self.assertEqual(response.read_bytes(),raw)
        updated=json.loads(meta.read_text());self.assertEqual(updated['status'],'validated')
        self.assertNotIn('error_code',updated)

    def test_old_or_missing_marker_still_revalidates_invalid_raw(self):
        value=deepcopy(VALID);value['slide_specs'][0].pop('layout')
        _,meta=self.seed(json.dumps(value),'invalid_slide_spec')
        meta.unlink();self.repeat_rejected('invalid_slide_spec')

    def test_finish_length_stays_truncation_not_generic_invalid_cache(self):
        response,meta=self.seed(json.dumps(VALID));old=json.loads(meta.read_text())
        old.update(finish_reason='length',status='validation_failed');meta.write_text(json.dumps(old))
        before=(response.read_bytes(),meta.read_bytes())
        with self.assertRaises(PipelineError)as caught:self.run_outline()
        self.assertEqual(caught.exception.code,'output_truncated')
        self.assertEqual(self.pipeline.gateway.calls,1)
        self.assertEqual((response.read_bytes(),meta.read_bytes()),before)

    def test_integrity_and_request_identity_rejections_precede_semantic_retyping(self):
        response,meta=self.seed(json.dumps(VALID));cached=json.loads(response.read_text())
        for field,value in [('prompt_sha256','0'*64),('input_fingerprint_sha256','0'*64),('response_sha256','0'*64),('raw_text','{}')]:
            with self.subTest(field=field):
                changed=deepcopy(cached);changed[field]=value;response.write_text(json.dumps(changed))
                before=response.read_bytes()
                with self.assertRaises(PipelineError)as caught:self.run_outline()
                self.assertEqual(caught.exception.code,'planner_cache_invalid')
                self.assertEqual(self.pipeline.gateway.calls,1)
                self.assertEqual(response.read_bytes(),before)
        response.write_text(json.dumps(cached))

    def test_cancel_before_planning_is_not_retyped_or_dispatched(self):
        with self.assertRaises(PipelineError)as caught:
            self.pipeline.create_outline(self.brief,[],self.recipe,cancel_check=lambda:True)
        self.assertEqual(caught.exception.code,'cancelled');self.assertEqual(self.pipeline.gateway.calls,0)

    def test_unowned_asset_id_is_dropped_never_rebound(self):
        # op 176: an ID outside this project's figure catalog is dropped (never re-bound to
        # another image) instead of voiding the whole outline.
        value=deepcopy(VALID);value['slide_specs'][0]['source_asset_ids']=['not-owned']
        self.seed(json.dumps(value))

    def test_provider_failure_is_not_retyped(self):
        with patch.object(self.pipeline.gateway,'text',side_effect=PipelineError('provider_unavailable','synthetic')):
            with self.assertRaises(PipelineError)as caught:self.run_outline()
        self.assertEqual(caught.exception.code,'provider_unavailable')

    def test_changed_explicit_brief_gets_new_fingerprint_without_deleting_old_cache(self):
        value=deepcopy(VALID);value['slide_specs'][0].pop('layout')
        response,meta=self.seed(json.dumps(value),'invalid_slide_spec');before=(response.read_bytes(),meta.read_bytes())
        self.pipeline.gateway.raw=json.dumps(VALID)
        self.brief['requirements']='Use a concise receive-review process.'
        output=self.run_outline()
        self.assertEqual(output['slide_specs'][0]['title'],'Fixture flow')
        self.assertEqual(self.pipeline.gateway.calls,2)
        self.assertEqual((response.read_bytes(),meta.read_bytes()),before)

    def test_service_public_failure_message_and_diagnostics_do_not_expose_private_fields(self):
        from datetime import datetime
        from types import SimpleNamespace
        from services.wuyu_workflow_service import WuyuWorkflowService,_failure_message,_failure_diagnostics,_safe_failure_code
        message=_failure_message(NEW_CODE)
        self.assertIn('原样重试不会改变结果',message)
        self.assertIn('更新需求',message)
        self.assertEqual(_safe_failure_code(NEW_CODE),NEW_CODE)
        diagnostics=_failure_diagnostics({'validation_code':'unsupported_numeric_claim','provider_called':False,
            'response_cached':True,'response_reused':True,'unsupported_numbers':['984321.76'],'page':1,
            'raw_text':'PRIVATE_FIXTURE_RAW','api_key':'PRIVATE_FIXTURE_KEY','cache_path':'/private/fixture'})
        self.assertEqual(diagnostics['validation_code'],'unsupported_numeric_claim')
        self.assertEqual(diagnostics['unsupported_numbers'],['984321.76'])
        self.assertEqual(diagnostics['page'],1)
        for key in ['raw_text','api_key','cache_path']:self.assertNotIn(key,diagnostics)
        service=WuyuWorkflowService.__new__(WuyuWorkflowService)
        service.queue_snapshot=lambda job:None
        record=SimpleNamespace(progress=json.dumps({'error':{'retryable':False,'diagnostics':{
            'validation_code':'unsupported_numeric_claim','unsupported_numbers':['984321.76'],
            'page':1,'provider_called':False,'raw_text':'PRIVATE_FIXTURE_RAW'}}}),
            error_code=NEW_CODE,payload='{}',status='failed',action='generate_outline',
            project_id='fixture-project',id='fixture-job',task_id='fixture-task',input_epoch=1,
            cancel_requested=False,retry_of=None,created_at=datetime(2026,1,1))
        public=service._job_dict(record)
        self.assertFalse(public['retryable'])
        self.assertEqual(public['error_code'],NEW_CODE)
        self.assertEqual(public['diagnostics']['validation_code'],'unsupported_numeric_claim')
        self.assertNotIn('PRIVATE_FIXTURE_RAW',json.dumps(public))


if __name__=='__main__':unittest.main()
