import unittest
from unittest.mock import patch
import test_wuyu_relayout as fixture
from services.wuyu_deep_research import signature, research_public, PROFILES
from services.wuyu_workflow_service import WorkflowAPIError
from models.wuyu_workflow import WuyuWorkflowJob

class RecoveryTests(unittest.TestCase):
    setUp=fixture.RelayoutTests.setUp
    tearDown=fixture.RelayoutTests.tearDown

    def failed_research(self,body):
        body['research']={'requested':True,'signature':signature(body),'mode':'web','status':'failed',
            'report_id':'self-report','prompt':'self-created public topic','profile':PROFILES['web'],
            'error_code':'research_plan_invalid','progress':{'source_count':0},'partial_sources':[]}

    def test_failed_research_can_select_materials_without_a_job_or_losing_upload(self):
        self.service._mutate(self.project,self.failed_research)
        revision,before=self.service._read(self.project)
        self.assertFalse(research_public(before)['ready_for_planning'])
        with patch.object(self.service,'_research_ui'):
            result=self.service.execute_action(self.project,'confirm_research',
                {'mode':'materials','prompt':'self-created public topic','acknowledge_cost':False},expected_revision=revision)
        self.assertTrue(result['research']['ready_for_planning'])
        self.assertEqual(result['research']['mode'],'materials')
        self.assertEqual(WuyuWorkflowJob.query.count(),0)
        self.assertTrue(self.path.is_file())
        self.assertEqual(result['materials'][0]['id'],before['materials'][0]['id'])

    def test_failed_file_can_be_explicitly_excluded_in_the_same_project(self):
        def fail(body):body['materials'][0].update(status='failed',error_code='MATERIAL_PARSE_FAILED')
        self.service._mutate(self.project,fail)
        revision,body=self.service._read(self.project);ident=body['materials'][0]['id']
        result=self.service.execute_action(self.project,'exclude_materials',{'material_ids':[ident],'reason':'User chose to replace the corrupt original'},expected_revision=revision)
        self.assertFalse(result['materials'][0]['selected_for_content'])
        self.assertEqual(result['project_id'],self.project)
        self.assertEqual(WuyuWorkflowJob.query.count(),0)
        self.assertTrue(self.path.is_file())

    def test_relayout_still_rejects_switch_to_research_material_mode(self):
        def fail(body):
            self.failed_research(body);body['brief']['task_mode']='relayout'
        self.service._mutate(self.project,fail)
        with patch.object(self.service,'_research_ui'):
            with self.assertRaises(WorkflowAPIError)as caught:
                self.service.execute_action(self.project,'confirm_research',{'mode':'materials','prompt':'public topic','acknowledge_cost':False})
        self.assertEqual(caught.exception.code,'RELAYOUT_RESEARCH_UNAVAILABLE')
