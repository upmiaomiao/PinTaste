import io
import unittest
from werkzeug.datastructures import FileStorage
import test_wuyu_relayout as fixture
from services.wuyu_workflow_service import WorkflowAPIError
from models import ReferenceFile


class TaskModeTests(unittest.TestCase):
    setUp = fixture.RelayoutTests.setUp
    tearDown = fixture.RelayoutTests.tearDown
    prepare = fixture.RelayoutTests.prepare
    run_preparation = fixture.RelayoutTests.run_preparation

    def test_new_empty_project_accepts_task_mode_through_real_executor(self):
        from models import db,Project
        import uuid
        ident=str(uuid.uuid4());db.session.add(Project(id=ident,user_id='owner',idea_prompt='self-created original intake'));db.session.commit()
        view=self.service.execute_action(ident,'update_brief',{'brief':{'task_mode':'relayout','creation_kind':'deck','page_count':2,'output_preference':'image'}})
        self.assertEqual(view['brief']['task_mode'],'relayout')
        self.assertIsNone(view['brief']['page_count'])
        self.assertEqual(view['workflow']['phase'],'relayout_intake')

    def test_relayout_intake_has_no_default_twelve_page_plan(self):
        self.service._mutate(self.project, lambda b:b['brief'].update(task_mode='relayout'))
        view=self.service.snapshot(self.project)
        self.assertIsNone(view['brief']['page_count'])
        self.assertEqual(view['workflow']['pages'],[])
        self.assertEqual(view['workflow']['phase'],'relayout_intake')
        self.assertIsNone(view['reading'])

    def test_relayout_cannot_start_models_before_original_preparation(self):
        self.service._mutate(self.project, lambda b:b['brief'].update(task_mode='relayout'))
        for action,payload in [('analyze_materials',{}),('generate_outline',{}),('generate_images',{'generation_requested':True}),('generate_samples',{})]:
            with self.assertRaises(WorkflowAPIError)as caught:
                self.service.execute_action(self.project,action,payload)
            self.assertEqual(caught.exception.code,'RELAYOUT_SOURCE_REQUIRED')

    def test_legacy_failed_intake_cannot_fall_back_to_paid_new_deck(self):
        self.service._mutate(self.project,lambda b:b.update(relayout={'status':'failed'}))
        with self.assertRaises(WorkflowAPIError)as caught:
            self.service.execute_action(self.project,'analyze_materials',{})
        self.assertEqual(caught.exception.code,'RELAYOUT_SOURCE_REQUIRED')

    def test_relayout_rejects_armed_search_before_starting_research(self):
        from unittest.mock import patch
        self.service._mutate(self.project,lambda b:b['brief'].update(task_mode='relayout'))
        with patch.object(self.service,'_start_armed_research',side_effect=AssertionError('No search dispatch')):
            with self.assertRaises(WorkflowAPIError)as caught:
                self.service.execute_action(self.project,'request_research',{})
            self.assertEqual(caught.exception.code,'RELAYOUT_RESEARCH_UNAVAILABLE')

    def test_existing_material_project_can_change_task_without_replacing_its_source(self):
        import hashlib
        before = self.service._read(self.project)[1]
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        result = self.service.execute_action(self.project, 'update_brief', {'task_mode': 'relayout'})
        self.assertEqual(result['project_id'], self.project)
        self.assertEqual(result['brief']['task_mode'], 'relayout')
        self.assertEqual(self.service._read(self.project)[1]['materials'], before['materials'])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), digest)
        self.assertEqual(result['materials'][0]['sha256'], self.sha)

    def test_relayout_upload_refuses_second_original_and_text_evidence(self):
        self.service._mutate(self.project,lambda b:b['brief'].update(task_mode='relayout'))
        for name in ['another.pptx','wrong.docx']:
            f=FileStorage(stream=io.BytesIO(b'self-created non-sensitive fixture'),filename=name)
            with self.assertRaises(WorkflowAPIError)as caught:
                self.service.add_materials(self.project,[f],None,None,role='material')
            self.assertEqual(caught.exception.code,'RELAYOUT_SOURCE_INVALID')
        self.assertEqual(ReferenceFile.query.count(),0)
        with self.assertRaises(WorkflowAPIError):
            self.service.add_materials(self.project,None,{'text':'fake original'},None,role='material')

    def test_explicit_source_preparation_can_adopt_a_legacy_upload(self):
        result=self.run_preparation([2])
        self.assertEqual(result['brief']['task_mode'],'relayout')
        self.assertEqual(result['brief']['page_count'],3)
        self.assertEqual(result['relayout']['selected_pages'],[2])


if __name__=='__main__':unittest.main()
