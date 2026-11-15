"""Real executor checks for requirements metadata, partial intake and in-project retargeting."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from PIL import Image
from werkzeug.datastructures import FileStorage
import test_wuyu_relayout as fixture
from models import db, ReferenceFile
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_errors import PipelineError
from services.wuyu_materials import parse_local_material
from services.wuyu_relayout import current_source
from services.wuyu_workflow_service import CancelledWork, WorkflowAPIError


class ConversationRecoveryTests(unittest.TestCase):
    tearDown = fixture.RelayoutTests.tearDown
    prepare = fixture.RelayoutTests.prepare
    run_preparation = fixture.RelayoutTests.run_preparation

    def setUp(self):
        fixture.RelayoutTests.setUp(self)
        catalog = self.root / 'catalog.json'
        catalog.write_text('{"schema_version":"1.0","sources":[]}', encoding='utf-8')
        self.app.config.update(WUYU_TEMPLATE_LIBRARY_PATH=str(catalog),
                               WUYU_TEMPLATE_STORE_ROOT=str(self.root / 'template-store'))

    def change(self, **brief):
        revision = self.service._read(self.project)[0]
        return self.service.execute_action(self.project, 'update_brief', brief, expected_revision=revision)

    def add_prior_outputs(self):
        output = self.root / self.project / 'exports' / 'prior-result.pptx'
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(self.path.read_bytes())
        uri = self.service._record_artifact(self.project, output, 'image_pptx', None)
        image = output.with_suffix('.png')
        Image.new('RGB', (160, 90), (23, 74, 128)).save(image)
        image_uri = self.service._record_artifact(self.project, image, 'slide_image', None,
                                                  {'number': 1, 'version': 1})
        def mark(body):
            body['workflow']['pages'][0].update(image_uri=image_uri, generation_status='ready')
        self.service._mutate(self.project, mark)
        return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in [self.path, output, image]}, uri

    def assert_outputs_unchanged(self, records):
        for path, expected in records.items():
            self.assertTrue(path.is_file())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
        _, body = self.service._read(self.project)
        referenced = {item['relative_path'] for item in body['artifacts']}
        for path in records:
            if path != self.path:
                self.assertIn(path.relative_to(self.root).as_posix(), referenced)

    def test_metadata_only_revision_does_not_reset_reader_template_or_epoch(self):
        def reviewed(body):
            body['reading'] = {'status': 'accepted', 'source_revision': body.get('material_revision', 0),
                               'coverage_complete': True, 'draft_revision': 3, 'issues': [],
                               '_reader_brief': deepcopy(body['brief'])}
        self.service._mutate(self.project, reviewed)
        before = self.service._read(self.project)[1]
        self.change(conversation_config_revision=7)
        _, after = self.service._read(self.project)
        self.assertEqual(after['brief']['conversation_config_revision'], 7)
        for key in ['epoch', 'workflow', 'reading', 'material_revision', 'slide_specs', 'artifacts']:
            self.assertEqual(after.get(key), before.get(key), key)
        self.assertEqual(WuyuWorkflowJob.query.count(), 0)

    def test_invalid_metadata_revisions_are_rejected_before_write(self):
        before = self.service._read(self.project)
        for value in [-1, True, 1.5, '2', None]:
            with self.assertRaises(WorkflowAPIError):
                self.change(conversation_config_revision=value)
            self.assertEqual(self.service._read(self.project), before)

    def test_job_requirements_snapshot_is_server_owned_and_immutable(self):
        self.app.config['WUYU_PIPELINE_FACTORY'] = lambda config: types.SimpleNamespace(config=config)
        self.change(conversation_config_revision=4)
        before = self.service._read(self.project)[1]
        forged = {'brief': {'topic': 'forged'}, 'materials': []}
        job_id, _ = self.service._mutate(self.project, lambda body: self.service._new_job_record(
            self.project, '_parse_materials', {'material_ids': [self.material['id']],
            '__runtime_requirements_snapshot': forged}, body))
        job = db.session.get(WuyuWorkflowJob, job_id)
        stored = json.loads(job.payload)['__runtime_requirements_snapshot']
        self.assertEqual(stored['conversation_config_revision'], 4)
        self.assertEqual(stored['brief'], before['brief'])
        self.assertEqual(stored['materials'][0]['sha256'], self.sha)
        self.assertEqual(stored['materials'][0]['id'], self.material['id'])
        payload = job.payload
        self.change(conversation_config_revision=5)
        db.session.refresh(job)
        self.assertEqual(job.payload, payload)
        self.assertFalse(job.cancel_requested)
        self.assertEqual(self.service._read(self.project)[1]['epoch'], before['epoch'])

    def intake(self, parser):
        self.calls = []
        owner = self
        class LocalPipeline:
            def __init__(self, config):
                self.config = config
            def parse_material(self, path, filename=None, **kwargs):
                owner.calls.append(filename)
                return parser(path, self.config, filename, kwargs)
        self.app.config['WUYU_PIPELINE_FACTORY'] = LocalPipeline
        files = [FileStorage(stream=io.BytesIO(b'intentionally invalid office fixture'), filename='broken.docx'),
                 FileStorage(stream=io.BytesIO(b'Second material is retained after the first fails.'), filename='valid.txt')]
        return self.service.add_materials(self.project, files, None, None)

    def test_first_failed_material_does_not_skip_following_valid_material(self):
        def parse(path, config, filename, kwargs):
            if filename == 'broken.docx':
                raise PipelineError('unsupported_material', 'Self-created failed input')
            return parse_local_material(path, config, filename=filename, cancel_check=kwargs['cancel_check'])
        response = self.intake(parse)
        self.service.run_pending(self.project)
        job = db.session.get(WuyuWorkflowJob, response['job']['id'])
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_code, 'material_batch_partial')
        _, body = self.service._read(self.project)
        rows = {row['filename']: row for row in body['materials']}
        self.assertEqual(rows['broken.docx']['status'], 'failed')
        self.assertEqual(rows['valid.txt']['status'], 'completed')
        self.assertIn('Second material', rows['valid.txt']['text'])
        self.assertEqual(self.calls, ['broken.docx', 'valid.txt'])
        for name in ['broken.docx', 'valid.txt']:
            row = rows[name]
            self.assertTrue((self.root / row['relative_path']).is_file())
            self.assertEqual(db.session.get(ReferenceFile, row['id']).parse_status, row['status'])
        self.assertNotIn(rows['broken.docx']['id'], body.get('reading_exclusions', {}))

    def test_cancellation_stops_batch_before_parsing_another_material(self):
        def cancel(path, config, filename, kwargs):
            raise CancelledWork()
        response = self.intake(cancel)
        self.service.run_pending(self.project)
        job = db.session.get(WuyuWorkflowJob, response['job']['id'])
        self.assertEqual(job.status, 'cancelled')
        self.assertEqual(self.calls, ['broken.docx'])
        _, body = self.service._read(self.project)
        rows = [m for m in body['materials'] if m['filename'] in {'broken.docx', 'valid.txt'}]
        self.assertTrue(all(row['status'] == 'pending' for row in rows))
        self.assertTrue(all((self.root / row['relative_path']).is_file() for row in rows))

    def test_task_mode_can_change_in_same_project_while_prior_outputs_remain(self):
        self.run_preparation([2])
        outputs, _ = self.add_prior_outputs()
        before = self.service._read(self.project)[1]
        self.change(task_mode='create')
        _, created = self.service._read(self.project)
        self.assertEqual(created['brief']['task_mode'], 'create')
        self.assertIsNone(current_source(created))
        self.assertNotIn('relayout', created)
        self.assertEqual(created['relayout_history'][-1], before['relayout'])
        self.change(task_mode='relayout')
        _, relayout = self.service._read(self.project)
        self.assertEqual(relayout['brief']['task_mode'], 'relayout')
        self.assertEqual(self.service.snapshot(self.project)['project_id'], self.project)
        self.assert_outputs_unchanged(outputs)

    def test_preparing_again_after_existing_images_keeps_original_and_old_outputs(self):
        self.run_preparation([2])
        outputs, _ = self.add_prior_outputs()
        revision = self.service._read(self.project)[0]
        response = self.service.prepare_relayout(self.project, {'material_id': self.material['id'],
            'selected_pages': [1, 3], 'expected_revision': revision})
        with patch.object(self.service, '_pipeline', side_effect=AssertionError('Local re-preparation only')):
            self.service.run_pending(self.project)
        job = db.session.get(WuyuWorkflowJob, response['job']['id'])
        self.assertEqual(job.status, 'completed', job.error_code)
        _, body = self.service._read(self.project)
        self.assertEqual(body['relayout']['selected_pages'], [1, 3])
        self.assertEqual(body['relayout_history'][-1]['selected_pages'], [2])
        self.assert_outputs_unchanged(outputs)

    def test_extra_source_requires_explicit_exclusion_within_current_project(self):
        extra = deepcopy(self.material)
        extra['id'] = 'extra-self-created-material'
        self.service._mutate(self.project, lambda body: body['materials'].append(extra))
        with self.assertRaises(WorkflowAPIError) as caught:
            self.prepare([2])
        self.assertNotIn('新建', str(caught.exception))
        self.assertTrue(self.path.is_file())
        revision = self.service._read(self.project)[0]
        with patch.object(self.service, '_new_job_record', side_effect=AssertionError('Excluding a source must not dispatch paid reading')):
            self.service.execute_action(self.project, 'exclude_materials', {'material_ids': [extra['id']],
                'reason': 'Explicitly use only the first original'}, expected_revision=revision)
        response = self.prepare([2])
        self.assertEqual(response['project_id'], self.project)
        self.assertTrue(self.path.is_file())

    def test_excluding_an_owned_source_image_never_dispatches_reading(self):
        image = self.root / self.project / 'materials' / 'source-image.png'
        Image.new('RGB', (32, 18), (12, 36, 72)).save(image)
        asset = {'id': 'self-created-source-image', 'path': str(image),
                 'sha256': hashlib.sha256(image.read_bytes()).hexdigest()}
        self.service._mutate(self.project, lambda body: body['materials'][0]['source_assets'].append(asset))
        before = self.service._read(self.project)[1]
        with patch.object(self.service, '_new_job_record', side_effect=AssertionError('Exclusion is not reading authorization')):
            result = self.service.execute_action(self.project, 'exclude_source_assets',
                {'source_asset_ids': [asset['id']], 'reason': 'User chose not to use this source figure'})
        _, body = self.service._read(self.project)
        self.assertEqual(WuyuWorkflowJob.query.count(), 0)
        self.assertIn(asset['id'], body['reading_asset_exclusions'])
        self.assertGreater(body['material_revision'], before['material_revision'])
        self.assertTrue(image.is_file())
        self.assertTrue(self.path.is_file())


if __name__ == '__main__':
    unittest.main()
