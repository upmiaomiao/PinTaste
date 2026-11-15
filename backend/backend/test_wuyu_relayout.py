import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
import uuid
from unittest.mock import patch

from flask import Flask, request
from pptx import Presentation
from pptx.util import Inches
from PIL import Image
from werkzeug.datastructures import FileStorage
from models import db, Project, User
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_workflow_service import WuyuWorkflowService, WorkflowAPIError
from services.wuyu_relayout import inspect_pptx, current_source
from services.wuyu_state import apply_event
from services.wuyu_source_assets import verify_source_assets
from controllers.wuyu_workflow_controller import wuyu_workflow_bp


class RelayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///' + str(self.root / 'isolated.sqlite'), SQLALCHEMY_TRACK_MODIFICATIONS=False,
            UPLOAD_FOLDER=str(self.root), WUYU_WORKFLOW_AUTOSTART=False, WUYU_QUOTA_ENABLED=False,
            WUYU_EXECUTION_MODE='combined', WUYU_READING_ENABLED=True)
        db.init_app(self.app)
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        self.service = WuyuWorkflowService(self.app)
        self.app.extensions['wuyu_workflow'] = self.service
        self.app.register_blueprint(wuyu_workflow_bp)
        @self.app.before_request
        def identity():
            request.current_user = types.SimpleNamespace(id='owner', role='user') if request.headers.get('X-Test-User') == 'owner' else None
            if request.headers.get('X-Test-User') == 'other':
                request.current_user = types.SimpleNamespace(id='other', role='user')
        self.project = str(uuid.uuid4())
        db.session.add(Project(id=self.project, user_id='owner', idea_prompt='重新排版自制验收稿')); db.session.commit()
        self.path = self.root / self.project / 'materials' / 'source.pptx'; self.path.parent.mkdir(parents=True)
        presentation = Presentation(); presentation.slide_width=Inches(13.333333); presentation.slide_height=Inches(7.5)
        for n in range(1, 4):
            slide=presentation.slides.add_slide(presentation.slide_layouts[6])
            box=slide.shapes.add_textbox(Inches(.7), Inches(.6), Inches(11), Inches(2))
            box.text_frame.text=f'原稿第{n}页\nRevenue {n * 24} / 成本 {n * 6}\n保留中文与 English'
            if n == 2:
                table=slide.shapes.add_table(2, 2, Inches(1), Inches(3), Inches(5), Inches(1)).table
                table.cell(0,0).text='指标';table.cell(0,1).text='数值';table.cell(1,0).text='总计';table.cell(1,1).text='48'
        presentation.save(self.path)
        self.sha=hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.material={'id':str(uuid.uuid4()),'filename':'.中文原稿.pptx','relative_path':self.path.relative_to(self.root).as_posix(),
            'sha256':self.sha, 'bytes':self.path.stat().st_size,'role':'material','status':'completed', 'chunks':[], 'source_assets':[], 'text':'fixture'}
        self.service._mutate(self.project,lambda b:b['materials'].append(self.material))
        self.client=self.app.test_client()

    def tearDown(self):
        self.service.stop_event.set()
        if self.service.executor:self.service.executor.shutdown(wait=True)
        db.session.remove();self.context.pop();self.temp.cleanup()

    def prepare(self, selected=None):
        revision,_=self.service._read(self.project)
        payload={'material_id':self.material['id'],'expected_revision':revision}
        if selected is not None:payload['selected_pages']=selected
        return self.service.prepare_relayout(self.project,payload)

    def run_preparation(self, selected=None):
        result=self.prepare(selected)
        with patch.object(self.service,'_pipeline',side_effect=AssertionError('Local preparation must not construct provider')):
            self.service.run_pending(self.project)
        job=db.session.get(WuyuWorkflowJob,result['job']['id'])
        self.assertEqual(job.status,'completed', job.error_code)
        return self.service.snapshot(self.project)

    def test_local_render_three_pages_preserves_source_and_numbers(self):
        result=self.run_preparation([2])
        if Path('/acceptance-output').is_dir():
            Path('/acceptance-output/workflow.json').write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8')
            _,fixture_body=self.service._read(self.project)
            first=next(a for a in fixture_body['artifacts'] if a['kind']=='relayout_source_page')
            Path('/acceptance-output/original-page.png').write_bytes((self.root/first['relative_path']).read_bytes())
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),self.sha)
        self.assertEqual(result['brief']['page_count'],3)
        self.assertEqual(result['relayout']['selected_pages'],[2])
        self.assertEqual(len(result['relayout']['previews']),3)
        self.assertIn('48', result['slide_specs'][1]['copy'])
        self.assertIsNone(result['reading'])
        self.assertTrue(result['template_selection']['required'])
        self.assertEqual([a['kind'] for a in result['artifacts']],['relayout_source_page']*3)
        _,body=self.service._read(self.project)
        for spec in body['slide_specs']:
            self.assertEqual(len(verify_source_assets(spec,self.app.config)),1)
        # Source/previews are not final COS export kinds.
        self.assertFalse(any(a['kind'] in {'pptx','pdf','image_pptx','images_zip'} for a in result['artifacts']))

    def test_render_retry_reuses_verified_cache_and_is_idempotent(self):
        self.run_preparation()
        before=self.service._read(self.project)[0]
        with patch('services.wuyu_relayout.render_exported_pptx',side_effect=AssertionError('Should reuse verified cache')):
            result=self.run_preparation([2])
            same=self.prepare([2])
        self.assertEqual(len(result['artifacts']),3)
        self.assertEqual(same['revision'],result['revision'])

    def test_render_failure_is_terminal_retryable_and_keeps_source(self):
        result=self.prepare()
        with patch('services.wuyu_relayout.render_exported_pptx',return_value={'available':False,'paths':[]}):
            self.service.run_pending(self.project)
        job=db.session.get(WuyuWorkflowJob,result['job']['id'])
        self.assertEqual(job.status,'failed');self.assertEqual(job.error_code,'relayout_render_failed')
        self.assertEqual(self.service.snapshot(self.project)['relayout']['status'],'failed')
        self.assertTrue(self.path.is_file())
        self.run_preparation()

    def test_corrupt_cache_rebuild_keeps_previous_preview_bytes(self):
        self.run_preparation()
        _,body=self.service._read(self.project)
        old=[(self.root/a['relative_path'],a['sha256']) for a in body['artifacts']]
        cache=self.service._path(self.project,'relayout_sources',self.sha)/'manifest.json'
        cache.write_text('[]')
        self.run_preparation([2])
        self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest()==sha for p,sha in old))

    def test_no_model_catalog_or_runtime_freeze_for_intake(self):
        with patch('services.wuyu_runtime_settings.freeze_runtime_config',side_effect=AssertionError('No model settings needed')):
            result=self.prepare()
            self.assertEqual(result['job']['status'],'queued')
            payload=json.loads(db.session.get(WuyuWorkflowJob,result['job']['id']).payload)
            self.assertNotIn('__runtime_snapshot',payload);self.assertNotIn('__template_catalog_binding',payload)

    def test_duplicate_pending_intake_returns_same_job(self):
        a=self.prepare();b=self.prepare()
        self.assertEqual(a['job']['id'],b['job']['id'])
        self.assertEqual(WuyuWorkflowJob.query.count(),1)

    def test_revision_conflict_and_bad_selection_leave_state_unchanged(self):
        revision,_=self.service._read(self.project)
        for selected in [[],[0],[4],[True],[1,1],'all']:
            with self.assertRaises(WorkflowAPIError):self.prepare(selected)
        with self.assertRaises(WorkflowAPIError):
            self.service.prepare_relayout(self.project,{'material_id':self.material['id'],'expected_revision':revision-1})
        self.assertEqual(WuyuWorkflowJob.query.count(),0)

    def test_authentication_ownership_and_validation_are_on_real_route(self):
        url=f'/api/projects/{self.project}/workflow/relayout'
        self.assertEqual(self.client.post(url,json={}).status_code,401)
        self.assertEqual(self.client.post(url,json={},headers={'X-Test-User':'other'}).status_code,403)
        self.assertEqual(self.client.post(url,json={},headers={'X-Test-User':'owner'}).status_code,400)

    def test_source_scope_changes_cannot_bypass_reading(self):
        self.run_preparation()
        _,body=self.service._read(self.project)
        self.assertIsNotNone(current_source(body))
        body['material_revision']+=1
        self.assertIsNone(current_source(body));self.assertTrue(self.service._reading_required(body))
        body['material_revision']-=1;body['materials'].append(dict(self.material,id='other-source'))
        self.assertIsNone(current_source(body));self.assertTrue(self.service._reading_required(body))

    def test_intake_requires_independent_unmodified_project(self):
        self.service._mutate(self.project, lambda b:b['materials'].append(dict(self.material,id='other')))
        with self.assertRaises(WorkflowAPIError):self.prepare()

    def test_original_aspect_page_count_and_hidden_pages_fail_explicitly(self):
        pres=Presentation(self.path);pres.slide_width=Inches(10);pres.save(self.path)
        with self.assertRaises(PipelineErrorForTests):inspect_pptx(self.path,self.app.config)

    def test_selected_pages_preserve_other_pages_with_no_provider_request(self):
        self.run_preparation([2])
        _,body=self.service._read(self.project)
        # A real template approval event supplies existing state preconditions;
        # this fixture does not grant approval on the live website.
        def confirm(b):
            state=apply_event(b['workflow'],{'event_id':str(uuid.uuid4()),'type':'template.selected','payload':{'template_id':'self-created-fixture'}})
            state=apply_event(state,{'event_id':str(uuid.uuid4()),'type':'template.choice_confirmed','payload':{'template_revision':state['template']['revision'],'source_kind':'upload'}})
            b['workflow']=state
        self.service._mutate(self.project,confirm)
        _,body=self.service._read(self.project)
        job=WuyuWorkflowJob(project_id=self.project,action='generate_images',payload='{}',input_epoch=body['epoch'],status='running')
        db.session.add(job);db.session.commit()
        self.service._relayout_seed_unchanged(job)
        _,after=self.service._read(self.project)
        self.assertEqual([p['generation_status'] for p in after['workflow']['pages']],['ready','idle','ready'])
        self.assertFalse(any(p['confirmed_image_version'] for p in after['workflow']['pages']))
        for number in (1,3):
            asset=next(a for a in after['materials'][0]['source_assets'] if a['source']['page']==number)
            self.assertEqual(after['workflow']['pages'][number-1]['artifact']['image_uri'],'artifact:'+asset['artifact_id'])

    def test_prepared_actions_preserve_page_scope_and_template_gate(self):
        self.run_preparation([2])
        _,body=self.service._read(self.project)
        for action,payload in [('analyze_materials',{}),('generate_outline',{}),('update_brief',{'page_count':12}),('update_outline',{'outline':[]}),('revise_page',{'number':1,'feedback':'change'})]:
            with self.assertRaises(WorkflowAPIError):self.service._relayout_validate_action(body,action,payload)
        with self.assertRaises(WorkflowAPIError):
            self.service.execute_action(self.project,'generate_images',{'generation_requested':True})

    def test_native_image_request_contains_original_page_and_new_style_separately(self):
        from services.wuyu_pipeline import WuyuPipeline
        self.run_preparation([2])
        _,body=self.service._read(self.project)
        spec=body['slide_specs'][1]
        pipeline=WuyuPipeline(dict(self.app.config,WUYU_DEMO_MODE=True))
        calls=[]
        def image(prompt, images, cancel):
            payload=json.loads(prompt);calls.append(payload)
            self.assertEqual(payload['slide_spec']['relayout_source']['original_page'],2)
            self.assertIn('relayout_policy',payload['slide_spec'])
            self.assertEqual(len(images),2)
            self.assertEqual(payload['source_content_references'][0]['input_image_index'],0)
            self.assertEqual(payload['style_reference_order'][0]['input_image_index'],1)
            return Image.new('RGB',(1280,720),'#ddeeff')
        pipeline.demo=False;pipeline.gateway=types.SimpleNamespace(image=image,last_response_metadata={})
        style=io.BytesIO();Image.new('RGB',(1280,720),'white').save(style,format='PNG')
        with patch.object(pipeline,'_references',return_value=[{'id':'new-template','sha256':'a'*64,'role':'template','bytes':style.getvalue(),'selection':{'provided_count':1,'used_count':1,'max_references':5,'dropped_reference_ids':[]}}]):
            pipeline.generate_slide(spec,{},str(self.root/'probe.png'))
        self.assertEqual(len(calls),1)
        # Image-only source pages must not print the UI page-number placeholder.
        spec=dict(spec,copy='',title='第2页')
        def image_only(prompt, images, cancel):
            self.assertEqual(json.loads(prompt)['slide_spec']['title'],'')
            self.assertEqual(len(images),1)
            return Image.new('RGB',(1280,720),'white')
        pipeline.gateway.image=image_only
        with patch.object(pipeline,'_references',return_value=[]):
            pipeline.generate_slide(spec,{},str(self.root/'image-only-probe.png'))

    def test_changed_requirements_preserve_direct_original_plan(self):
        self.run_preparation()
        before=self.service.snapshot(self.project)
        after=self.service.execute_action(self.project,'update_brief',{'requirements':'只重新排版，所有数字保留'})
        self.assertEqual(after['slide_specs'],before['slide_specs'])
        self.assertIsNone(after['reading'])
        self.assertEqual(after['brief']['page_count'],3)

    def test_only_selected_page_is_generated_then_three_page_export_opens(self):
        from services.wuyu_template_review import choice_signature
        from services.wuyu_pipeline import WuyuPipeline
        from services.wuyu_render import render_exported_pptx
        self.run_preparation([2])
        def confirm_fixture(b):
            state=apply_event(b['workflow'],{'event_id':str(uuid.uuid4()),'type':'template.selected','payload':{'template_id':'fixture-new-template'}})
            b['workflow']=apply_event(state,{'event_id':str(uuid.uuid4()),'type':'template.choice_confirmed','payload':{'template_revision':state['template']['revision'],'source_kind':'upload'}})
            b['template_choice']={'source_kind':'upload','source_id':'fixture-new-template','confirmed_via':'template_confirmation_ui','signature':choice_signature(b)}
        self.service._mutate(self.project,confirm_fixture)
        _,body=self.service._read(self.project)
        job=WuyuWorkflowJob(project_id=self.project,action='generate_images',payload='{}',input_epoch=body['epoch'],status='running')
        db.session.add(job);db.session.commit()
        pipeline=WuyuPipeline(dict(self.app.config,WUYU_DEMO_MODE=True));calls=[]
        def batch(job, plans, prepare, invoke, accept, reject, workers):
            for number in plans:
                plan=prepare(number);calls.append(number)
                accept(plan,invoke(plan,pipeline,lambda:None))
        with patch.object(self.service,'_pipeline_recipe',return_value={}), patch.object(self.service,'_model_batch',side_effect=batch), patch.object(self.service,'_model_workers',return_value=1):
            self.service._generate_batch(job)
        self.assertEqual(calls,[2])
        _,body=self.service._read(self.project)
        self.assertEqual([p['generation_status'] for p in body['workflow']['pages']],['ready']*3)
        with self.assertRaises(WorkflowAPIError):self.service._export_additional(job,pipeline,'pptx')
        versions={str(p['number']):p['image_version'] for p in body['workflow']['pages']}
        # Exact representation is the existing state module's version snapshot.
        from services.wuyu_state import _image_snapshot
        self.service._event(self.project,'images.confirmed',{'image_versions':_image_snapshot(body['workflow'])},job)
        uri=self.service._export_additional(job,pipeline,'pptx')
        _,body=self.service._read(self.project)
        item=next(a for a in body['artifacts'] if uri=='artifact:'+a['id'])
        exported=self.root/item['relative_path'];deck=Presentation(exported)
        self.assertEqual(len(deck.slides),3)
        rendered=render_exported_pptx(exported,self.app.config,expected_pages=3,output_dir=self.root/'reopened')
        self.assertTrue(rendered['available']);self.assertEqual(len(rendered['paths']),3)
        for number in (1,3):
            original=next(a for a in body['materials'][0]['source_assets'] if a['source']['page']==number)
            self.assertEqual(hashlib.sha256(deck.slides[number-1].shapes[0].image.blob).hexdigest(),original['sha256'])


from services.wuyu_errors import PipelineError as PipelineErrorForTests
if __name__ == '__main__': unittest.main()
