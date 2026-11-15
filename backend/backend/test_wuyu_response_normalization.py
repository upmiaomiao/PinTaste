"""Actual validator regressions; self-created text/catalogs, no provider calls."""
from copy import deepcopy
import json
import types
import unittest
from unittest.mock import patch

import test_wuyu_reading_project_recovery as fixture

from services.wuyu_errors import PipelineError
from services.wuyu_pipeline import _outline_json_object, json_object
from services.wuyu_reading import sha, validate_reader, validate_grounded_reader
from services.wuyu_reading_synthesis import apply_synthesis
from services.wuyu_reading_partial import continuation_options, accepted_reading
from services.wuyu_reading_workflow import _write


class ResponseNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.text='本次结果下降27%，原文保留。'
        self.segment={'id':'segment-123456', 'material_id':'material-a', 'chunk_id':'chunk-a',
                      'text':self.text, 'source':{'material_id':'material-a','filename':'fixture.txt'},
                      'char_start':0, 'char_end':len(self.text), 'chunk_line_start':1,
                      'text_sha256':sha(self.text)}
        self.task={'reader_contract':3,'segments':[self.segment],
                   'asset_catalog':{'material-a':[{'id':'figure-123456'}]},
                   'data_catalog':{'material-a':['data-123456']},'visual_inputs':[]}
        self.finding={'segment_id':'s1','quote':self.text,'summary':self.text,
                      'topic':'结果证据','relevance':4,'source_asset_ids':['figure-123456'],
                      'data_ids':['data-123456']}
        self.value={'read_material_ids':['m1'],'findings':[self.finding],'issues':[]}

    def encoded(self, value):
        return json.dumps(value,ensure_ascii=False)

    def wrapped(self):
        return (self.encoded(self.value).replace('read_material_ids','read_material\n_ids')
                .replace('"relevance"','"re\nlevance"').replace('figure-123456','figure-123\n456')
                .replace('data-123456','data-123\n456').replace('下降27%','下降\n27%'))

    def assert_code(self, code, func):
        with self.assertRaises(PipelineError) as caught:
            func()
        self.assertEqual(caught.exception.code,code)

    def test_reader_restores_schema_ids_and_exact_numeric_quote(self):
        raw=self.wrapped();before=sha(raw)
        result=validate_reader(raw,self.task)
        finding=result['findings'][0]
        self.assertEqual(finding['quote'],self.text)
        self.assertEqual(finding['source_asset_ids'],['figure-123456'])
        self.assertEqual(finding['data_ids'],['data-123456'])
        self.assertEqual(finding['relevance'],4)
        self.assertEqual(finding['quote_sha256'],sha(self.text))
        self.assertEqual((finding['quote_char_start'],finding['quote_char_end']),(0,len(self.text)))
        self.assertEqual(sha(raw),before)

    def test_escaped_identifier_break_and_actual_space_are_not_repaired(self):
        for ident in ('figure-123\n456','figure-123 456'):
            value=deepcopy(self.value);value['findings'][0]['source_asset_ids']=[ident]
            self.assert_code('reading_source_id_mismatch',lambda:validate_reader(self.encoded(value),self.task))

    def test_escaped_key_break_is_not_repaired(self):
        value=deepcopy(self.value);value['findings'][0]['re\nlevance']=value['findings'][0].pop('relevance')
        self.assert_code('invalid_reading',lambda:validate_reader(self.encoded(value),self.task))

    def test_genuine_source_newline_is_preserved(self):
        text='第一段\n第二段下降27%。'
        task=deepcopy(self.task);task['segments'][0].update(text=text,text_sha256=sha(text),char_end=len(text))
        value=deepcopy(self.value);value['findings'][0].update(quote=text,summary=text)
        raw=self.encoded(value).replace('\\n','\n')
        self.assertEqual(validate_reader(raw,task)['findings'][0]['quote'],text)

    def test_foreign_catalog_is_not_used_for_repair(self):
        task=deepcopy(self.task);task['asset_catalog']['other-material']=[{'id':'foreign-123456'}]
        raw=self.encoded(self.value).replace('figure-123456','foreign-123\n456')
        self.assert_code('reading_source_id_mismatch',lambda:validate_reader(raw,task))

    def test_fabricated_number_quote_still_rejected(self):
        raw=self.wrapped().replace('27%','99%')
        self.assert_code('reading_quote_mismatch',lambda:validate_reader(raw,self.task))

    def test_partial_reader_keeps_valid_finding_and_excludes_unknown_image(self):
        value=deepcopy(self.value);bad=deepcopy(self.finding);bad['source_asset_ids']=['foreign-image']
        value['findings'].append(bad)
        result=validate_grounded_reader(self.encoded(value),self.task)
        self.assertEqual(len(result['findings']),1)
        self.assertEqual(result['rejected_findings'][0]['code'],'reading_source_id_mismatch')

    def test_outline_uses_shared_schema_and_known_catalog_repair(self):
        spec={'number':1,'title':'下降27%','kind':'content','copy':'下降27%',
              'points':['下降27%'],'evidence_ids':['evidence-123456'],
              'source_asset_ids':['figure-123456'],'data_ids':['data-123456'],
              'layout':{'structure':'metric_cards','intent':'Exact source'},'role':'body'}
        raw=self.encoded({'slide_specs':[spec]})
        raw=(raw.replace('"title"','"ti\ntle"').replace('evidence_ids','evidence_\nids')
             .replace('evidence-123456','evidence-123\n456').replace('figure-123456','figure-123\n456')
             .replace('下降27%','下降\n27%'))
        value=_outline_json_object(raw,evidence={'evidence-123456':self.text},assets={'figure-123456'},data={'data-123456'})
        self.assertEqual(value['slide_specs'][0]['title'],'下降27%')
        self.assertEqual(value['slide_specs'][0]['evidence_ids'],['evidence-123456'])
        self.assertEqual(value['slide_specs'][0]['source_asset_ids'],['figure-123456'])

    def test_outline_keeps_97_title_contract_and_rejects_confusable_collisions(self):
        self.assertEqual(_outline_json_object('{"slide_specs":[{"title\n":"A"}]}')['slide_specs'][0],{'title':'A'})
        for other,code in [('"title":"B"','invalid_slide_spec'),('"title ":"B"','invalid_slide_spec'),
                           ('"title\\n":"B"','invalid_model_json')]:
            # Raw and escaped LF decode to the same key: the shared parser
            # rejects that duplicate before schema-level alias recovery.
            self.assert_code(code,lambda:_outline_json_object('{"slide_specs":[{"title\n":"A",'+other+'}]}'))

    def test_ordinary_valid_json_prose_is_identical(self):
        value={'slide_specs':[{'title':'Paragraph one\nParagraph two','copy':'industrial solid waste 27%'}]}
        self.assertEqual(_outline_json_object(self.encoded(value)),value)
        self.assertEqual(json_object(self.encoded(value)),value)

    def synthesis(self, *, foreign=False, fake=False, wrapped_tag=False):
        fact={'statement':self.text,'suggestion':self.text,'source':self.segment['source'],
              'segment_id':self.segment['id'],'source_asset_ids':[],'data_ids':[]}
        task={'id':'fixture-task','findings':{'fact-123456':fact},
              'assets':{'figure-123456':{'material_id':'material-a','source':{},'caption':''},
                        'foreign-123456':{'material_id':'other','source':{},'caption':''}}}
        page={'number':1,'title':'下降27%','finding_ids':['fact-123456'],
              'source_asset_ids':['foreign-123456'if foreign else'figure-123456'],
              'visual_form':'image_with_annotations','copy':'下降99%'if fake else'下降27%',
              'points':['下降27%']}
        raw=self.encoded({'pages':[page]}).replace('finding_ids','finding_\nids').replace('fact-123456','fact-123\n456')
        raw=raw.replace('visual_form','visual_\nform').replace('image_with_annotations','image_with_\nannotations').replace('下降27%','下降\n27%')
        if wrapped_tag:
            raw='{"ty\npe":"json_object",'+raw[1:]
        return apply_synthesis(raw,task,{'reader_contexts':1},{'page_count':1,'creation_kind':'pages'})

    def test_synthesis_uses_shared_ids_and_exact_source_copy(self):
        result=self.synthesis();spec=result['slide_specs'][0]
        self.assertEqual(spec['title'],'下降27%')
        self.assertEqual(spec['copy'],'下降27%')
        self.assertEqual(spec['source_asset_ids'],['figure-123456'])
        self.assertTrue(result['content_plan_complete'])

    def test_synthesis_foreign_source_and_invented_number_guards_still_apply(self):
        # op 176: an image from another material is dropped (never re-bound) instead of voiding the plan.
        self.assertEqual(self.synthesis(foreign=True)['slide_specs'][0]['source_asset_ids'],[])
        result=self.synthesis(fake=True)
        self.assertNotIn('99%',result['slide_specs'][0]['copy'])

    def test_synthesis_transport_tag_is_handled_after_shared_key_normalization(self):
        self.assertTrue(self.synthesis(wrapped_tag=True)['content_plan_complete'])

    def partial(self):
        return {'status':'draft_incomplete','coverage_complete':False,'content_plan_complete':True,
                'slide_specs':[{'number':1,'title':'Verified plan'}],
                'evidence_validation':{'accepted_findings':1},'issues':[{'code':'reading_source_id_mismatch'}],
                'draft_revision':2,'source_revision':3}

    def test_model_reference_failure_allows_only_explicit_verified_partial_consent(self):
        value=self.partial();options=continuation_options(value)
        self.assertEqual(options['failure_category'],'source_unread')
        self.assertTrue(options['can_continue_with_omissions'])
        self.assertFalse(accepted_reading(value,3))
        value.update(status='accepted',accepted_with_omissions=True,accepted_draft_revision=2,
                     partial_acceptance={'draft_revision':2,'source_revision':3,'continue_requested':True,'allow_incomplete':True})
        self.assertTrue(accepted_reading(value,3))
        value['partial_acceptance']['allow_incomplete']=False
        self.assertFalse(accepted_reading(value,3))

    def test_no_valid_plan_or_evidence_cannot_continue(self):
        for update in ({'content_plan_complete':False},{'slide_specs':[]},{'evidence_validation':{'accepted_findings':0}}):
            value=self.partial();value.update(update)
            self.assertFalse(continuation_options(value)['can_continue_with_omissions'])

    def test_real_integrity_failures_remain_security(self):
        for code in ('material_hash_mismatch','source_asset_hash_mismatch','unsafe_path','reading_cache_invalid'):
            value=self.partial();value['issues'].append({'code':code})
            options=continuation_options(value)
            self.assertEqual(options['failure_category'],'security')
            self.assertFalse(options['can_continue_with_omissions'])


class CachedResponseNormalizationTests(unittest.TestCase):
    setUp=fixture.RecoveryTests.setUp
    tearDown=fixture.RecoveryTests.tearDown
    raw_reader=staticmethod(fixture.RecoveryTests.raw_reader)
    cache_root=fixture.RecoveryTests.cache_root
    run_cached_reading=fixture.RecoveryTests.run_cached_reading
    assert_cache_unchanged=fixture.RecoveryTests.assert_cache_unchanged
    cache_fixture=fixture.RecoveryTests.cache_fixture

    def test_real_reader_cache_revalidates_raw_wraps_without_provider_or_rewrite(self):
        originals={}
        for task in self.original_plan['tasks']:
            path=self.cache_root(task)/'response.json'
            record=json.loads(path.read_text(encoding='utf-8'))
            raw=record['raw'].replace('"relevance"','"re\nlevance"')
            ident=task['segments'][0]['id'];middle=len(ident)//2
            raw=raw.replace(ident,ident[:middle]+'\n'+ident[middle:])
            record.update(raw=raw,raw_sha256=sha(raw));_write(path,record)
            originals[path]=path.read_bytes()
        body=self.run_cached_reading()  # Existing helper rejects provider construction.
        self.assertTrue(body['reading']['coverage_complete'])
        for path,raw in originals.items():self.assertEqual(path.read_bytes(),raw)

    def test_real_synthesis_cache_revalidates_without_charge_provider_or_rewrite(self):
        body,report,task,path=self.cache_fixture()
        ident=next(iter(task['findings']));fact=task['findings'][ident]
        value={'pages':[{'number':n,'title':'Verified evidence','finding_ids':[ident],
                         'source_asset_ids':[],'visual_form':'qualitative_diagram','copy':fact['statement']}
                        for n in range(1,body['brief']['page_count']+1)]}
        raw=json.dumps(value,ensure_ascii=False).replace('finding_ids','finding_\nids').replace('"title"','"title\n"')
        _write(path,{'task_id':task['id'],'raw':raw,'raw_sha256':sha(raw),'finish_reason':'stop'})
        original=path.read_bytes();job=types.SimpleNamespace(project_id=self.project,id='self-normalization-cache')
        with (patch('services.wuyu_reading_workflow.WorkflowCharge',side_effect=AssertionError('No charge')),
              patch.object(self.service,'_pipeline',side_effect=AssertionError('No provider'))):
            result=self.service._reading_synthesize(job,self.original_plan,report,body['brief'],self.config)
        self.assertTrue(result['content_plan_complete'])
        self.assertEqual(path.read_bytes(),original)


if __name__=='__main__':unittest.main()
