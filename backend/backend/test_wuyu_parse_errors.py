"""Local material failure diagnostics, real executor, disposable SQLite only."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from werkzeug.datastructures import FileStorage
import test_workflow_execution as fixture
from models import db, Project, ReferenceFile, Task
from models.wuyu_workflow import WuyuWorkflowJob
from services.wuyu_errors import PipelineError
from services.wuyu_workflow_service import CancelledWork, WorkflowAPIError, _failure_message
from services.wuyu_workflow_errors import (
    MATERIAL_PARSE_CODES, failure_message, material_error_code, material_parse_error_code,
)


SENTINEL = 'SELF_CREATED_PRIVATE_MARKER api_key=unused password=unused /private/self-created.txt'
GOOD_TEXT = b'Self-created delivery notes preserve valid inputs and accepted work.'
EXPECTED_PARSE_CODES = {
    'archive_limit', 'encrypted_material', 'image_limit', 'invalid_csv', 'invalid_encoding',
    'invalid_image', 'invalid_office', 'invalid_pdf', 'material_limit', 'material_not_found',
    'material_timeout', 'unsafe_archive', 'unsafe_path', 'unsupported_material', 'logo_format',
    'template_render_failed',
}


class ParseErrorTests(unittest.TestCase):
    tearDown = fixture.WorkflowExecutionTests.tearDown
    service = fixture.WorkflowExecutionTests.service

    def setUp(self):
        fixture.WorkflowExecutionTests.setUp(self)
        self.root = Path(self.app.config['UPLOAD_FOLDER'])
        self.root.mkdir(parents=True)
        catalog = self.root / 'catalog.json'
        catalog.write_text('{"schema_version":"1.0","sources":[]}', encoding='utf-8')
        self.app.config.update(WUYU_PIPELINE_FACTORY=self.pipeline, WUYU_READING_ENABLED=True,
            WUYU_TEMPLATE_LIBRARY_PATH=str(catalog), WUYU_TEMPLATE_STORE_ROOT=str(self.root / 'template-store'))
        self.workflow = self.service('combined')
        self.project = str(uuid.uuid4())
        db.session.add(Project(id=self.project, project_title='Self-created local parsing checks'))
        db.session.commit()
        self.parser_override = None
        self.calls = []
        self.original_bytes = {}

    def pipeline(self, config):
        from services.wuyu_pipeline import WuyuPipeline
        pipeline = object.__new__(WuyuPipeline)
        pipeline.config, pipeline.demo, pipeline.gateway = dict(config), True, None
        pipeline._hybrid_page_qa_cache = {}
        actual_parse = pipeline.parse_material
        def parse(path, filename=None, **kwargs):
            self.calls.append(filename)
            if self.parser_override is not None:
                return self.parser_override(actual_parse, path, filename, kwargs)
            return actual_parse(path, filename=filename, **kwargs)
        pipeline.parse_material = parse
        return pipeline

    def upload(self, files):
        supplied = [FileStorage(stream=io.BytesIO(raw), filename=name) for name, raw in files]
        result = self.workflow.add_materials(self.project, supplied, None, None)
        for name, raw in files:
            item = next(row for row in self.body()['materials'] if row['filename'] == name)
            self.original_bytes[item['id']] = (self.root / item['relative_path'], raw)
        return db.session.get(WuyuWorkflowJob, result['job']['id'])

    def run_job(self, job):
        self.assertTrue(self.workflow._claim(job.id))
        # Parsing must never construct a gateway or reserve paid model work.
        with (patch('services.wuyu_providers.WuyuGateway', side_effect=AssertionError('No provider construction')),
              patch('services.wuyu_workflow_quota.WorkflowCharge.reserve', side_effect=AssertionError('No model charge'))):
            self.workflow._run(job.id)
        db.session.expire_all()
        return db.session.get(WuyuWorkflowJob, job.id)

    def body(self):
        return self.workflow._read(self.project)[1]

    def row(self, filename):
        return next(row for row in self.body()['materials'] if row['filename'] == filename)

    def public_row(self, filename):
        return next(row for row in self.workflow.snapshot(self.project)['materials'] if row['filename'] == filename)

    def assert_originals_preserved(self):
        for ident, (path, raw) in self.original_bytes.items():
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_bytes(), raw)
            item = next(row for row in self.body()['materials'] if row['id'] == ident)
            self.assertEqual(item['sha256'], hashlib.sha256(raw).hexdigest())

    def assert_failed_batch(self, job):
        self.assertEqual(job.status, 'failed', job.error_code)
        self.assertEqual(job.error_code, 'material_batch_partial')
        task = db.session.get(Task, job.task_id)
        self.assertEqual(task.status, 'FAILED')
        self.assertEqual(task.error_message, 'material_batch_partial')
        public = self.workflow.get_job(self.project, job.id)
        self.assertEqual(public['error_message'], failure_message('material_batch_partial'))
        self.assertNotIn('模型处理未完成', public['error_message'])
        self.assertNotIn(SENTINEL, json.dumps(public, ensure_ascii=False))
        self.assert_originals_preserved()

    def assert_file_reason(self, filename, code):
        item, public = self.row(filename), self.public_row(filename)
        self.assertEqual(item['status'], 'failed')
        self.assertEqual(item['error_code'], code)
        self.assertEqual(public['error_code'], code)
        self.assertEqual(public['error_message'], failure_message(code))
        self.assertNotIn('模型处理未完成', public['error_message'])
        ref = db.session.get(ReferenceFile, item['id'])
        self.assertEqual((ref.parse_status, ref.error_message), ('failed', failure_message(code)))
        self.assertNotIn(SENTINEL, ref.error_message)

    def assert_good_text(self, filename='valid.txt'):
        item, public = self.row(filename), self.public_row(filename)
        self.assertEqual(item['status'], 'completed')
        self.assertIn(GOOD_TEXT.decode(), item['text'])
        self.assertNotIn('error_message', public)
        self.assertNotIn('error_code', item)
        ref = db.session.get(ReferenceFile, item['id'])
        self.assertEqual(ref.parse_status, 'completed')
        self.assertIsNone(ref.error_message)
        self.assertIn(GOOD_TEXT.decode(), ref.markdown_content)

    def test_real_corrupt_docx_keeps_office_reason_and_valid_text(self):
        job = self.run_job(self.upload([('broken.docx', b'not an Office ZIP'), ('valid.txt', GOOD_TEXT)]))
        self.assert_failed_batch(job)
        self.assert_file_reason('broken.docx', 'invalid_office')
        self.assert_good_text()
        self.assertEqual(self.calls, ['broken.docx', 'valid.txt'])

    def test_real_corrupt_pdf_keeps_pdf_reason_and_valid_text(self):
        job = self.run_job(self.upload([('broken.pdf', b'%PDF-1.4\nnot a PDF object graph'), ('valid.txt', GOOD_TEXT)]))
        self.assert_failed_batch(job)
        self.assert_file_reason('broken.pdf', 'invalid_pdf')
        self.assert_good_text()
        self.assertEqual(self.calls, ['broken.pdf', 'valid.txt'])

    def test_real_encrypted_pdf_is_local_file_error_without_model_dispatch(self):
        from PyPDF2 import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=160, height=90)
        writer.encrypt('self-created-test-password')
        encrypted = io.BytesIO()
        writer.write(encrypted)
        job = self.run_job(self.upload([('encrypted.pdf', encrypted.getvalue()), ('valid.txt', GOOD_TEXT)]))
        self.assert_failed_batch(job)
        self.assert_file_reason('encrypted.pdf', 'encrypted_material')
        self.assert_good_text()

    def test_all_bad_files_keep_separate_reasons_and_failed_batch_task(self):
        job = self.run_job(self.upload([('broken.docx', b'not zip'), ('broken.pdf', b'%PDF-1.4\nbroken')]))
        self.assert_failed_batch(job)
        self.assert_file_reason('broken.docx', 'invalid_office')
        self.assert_file_reason('broken.pdf', 'invalid_pdf')

    def test_typed_parser_codes_are_allowlisted_and_case_insensitive(self):
        self.assertEqual(set(MATERIAL_PARSE_CODES), EXPECTED_PARSE_CODES)
        for code in EXPECTED_PARSE_CODES:
            with self.subTest(code=code):
                for variant in (code, code.upper(), code.swapcase()):
                    self.assertEqual(material_parse_error_code(PipelineError(variant, SENTINEL)), code)
                    self.assertEqual(material_error_code(variant), code)
                    self.assertEqual(failure_message(variant), failure_message(code))
                self.assertNotIn('模型处理未完成', failure_message(code))
                self.assertNotIn(SENTINEL, failure_message(code))

    def test_untyped_exceptions_cannot_supply_a_trusted_parser_code(self):
        class FakeParserError(Exception):
            code = 'invalid_pdf'
        class FakeCapacityError(Exception):
            code = 'MATERIAL_PROJECT_LIMIT'
        for error in (RuntimeError(SENTINEL), FakeParserError(SENTINEL), FakeCapacityError(SENTINEL), None):
            with self.subTest(error_type=type(error).__name__):
                self.assertEqual(material_parse_error_code(error), 'MATERIAL_PARSE_FAILED')

    def test_unknown_exception_message_is_absent_from_material_ref_task_job_and_logs(self):
        def fail(actual, path, filename, kwargs):
            if filename == 'failed.txt':
                raise RuntimeError(SENTINEL)
            return actual(path, filename=filename, **kwargs)
        self.parser_override = fail
        job = self.upload([('failed.txt', b'Self-created failed text'), ('valid.txt', GOOD_TEXT)])
        with self.assertLogs(self.app.logger, level='WARNING') as logs:
            job = self.run_job(job)
        self.assert_failed_batch(job)
        self.assert_file_reason('failed.txt', 'MATERIAL_PARSE_FAILED')
        self.assert_good_text()
        task = db.session.get(Task, job.task_id)
        self.assertNotIn(SENTINEL, task.error_message)
        self.assertNotIn(SENTINEL, '\n'.join(logs.output))
        self.assertNotIn(SENTINEL, json.dumps(self.workflow.snapshot(self.project), ensure_ascii=False))

    def test_unknown_typed_error_is_generic_and_never_echoes_upstream_message(self):
        def fail(actual, path, filename, kwargs):
            if filename == 'failed.txt':
                raise PipelineError('untrusted_parser_detail', SENTINEL)
            return actual(path, filename=filename, **kwargs)
        self.parser_override = fail
        job = self.run_job(self.upload([('failed.txt', b'Self-created failed text'), ('valid.txt', GOOD_TEXT)]))
        self.assert_failed_batch(job)
        self.assert_file_reason('failed.txt', 'MATERIAL_PARSE_FAILED')
        self.assert_good_text()
        self.assertNotIn('untrusted_parser_detail', json.dumps(self.workflow.snapshot(self.project), ensure_ascii=False))

    def test_legacy_failed_material_codes_project_to_fixed_safe_text(self):
        for raw, expected in (('MATERIAL_PARSE_FAILED', 'MATERIAL_PARSE_FAILED'),
                              ('material_parse_failed', 'MATERIAL_PARSE_FAILED'),
                              ('InVaLiD_OfFiCe', 'invalid_office'),
                              ('MATERIAL_PROJECT_LIMIT', 'MATERIAL_PROJECT_LIMIT'),
                              ('untrusted_parser_detail', 'MATERIAL_PARSE_FAILED'),
                              (None, 'MATERIAL_PARSE_FAILED')):
            with self.subTest(code=raw):
                item = {'id': 'self-created-legacy', 'filename': 'legacy.txt', 'status': 'failed',
                    'error_code': raw, 'error_message': SENTINEL, 'chunks': [], 'source_assets': []}
                public = self.workflow._public_material(self.project, item)
                self.assertEqual(public['error_code'], expected)
                self.assertEqual(public['error_message'], failure_message(expected))
                self.assertNotIn(SENTINEL, json.dumps(public, ensure_ascii=False))

    def test_research_review_mapping_and_old_entry_are_reachable_in_all_cases(self):
        expected = '请先在资料调研中选择范围；联网调研完成后，查看来源与总结并确认使用。'
        self.assertIs(_failure_message, failure_message)
        for code in ('RESEARCH_REVIEW_REQUIRED', 'research_review_required', 'Research_Review_Required'):
            with self.subTest(code=code):
                self.assertEqual(_failure_message(code), expected)
                self.assertEqual(failure_message(code), expected)

    def test_successful_reparse_clears_old_reason_with_original_bytes_unchanged(self):
        def timeout(actual, path, filename, kwargs):
            if filename == 'recover.txt':
                raise PipelineError('material_timeout', SENTINEL)
            return actual(path, filename=filename, **kwargs)
        self.parser_override = timeout
        failed = self.run_job(self.upload([('recover.txt', GOOD_TEXT), ('valid.txt', GOOD_TEXT)]))
        self.assert_file_reason('recover.txt', 'material_timeout')
        successful = deepcopy(self.row('valid.txt'))
        self.parser_override = None
        self.calls.clear()
        result = self.workflow.execute_action(self.project, 'retry', {'job_id': failed.id})
        retried = self.run_job(db.session.get(WuyuWorkflowJob, result['job']['id']))
        self.assertEqual(retried.status, 'completed', retried.error_code)
        self.assertEqual(db.session.get(Task, retried.task_id).status, 'COMPLETED')
        self.assertEqual(self.calls, ['recover.txt'])
        self.assertEqual(self.row('valid.txt'), successful)
        self.assert_good_text('recover.txt')
        self.assert_good_text()
        self.assert_originals_preserved()

    def test_cancelled_parser_is_not_retyped_and_later_file_is_not_parsed(self):
        def cancel(actual, path, filename, kwargs):
            raise CancelledWork()
        self.parser_override = cancel
        job = self.run_job(self.upload([('first.txt', b'Self-created input'), ('later.txt', GOOD_TEXT)]))
        self.assertEqual(job.status, 'cancelled')
        self.assertEqual(job.error_code, 'CANCELLED_OR_INPUT_CHANGED')
        self.assertEqual(self.calls, ['first.txt'])
        for name in ('first.txt', 'later.txt'):
            item = self.row(name)
            self.assertEqual(item['status'], 'pending')
            self.assertNotIn('error_code', item)
            self.assertNotIn('error_message', self.public_row(name))
        self.assert_originals_preserved()

    def test_typed_capacity_sources_keep_capacity_message_and_valid_material(self):
        errors = (PipelineError('material_project_capacity', SENTINEL),
                  WorkflowAPIError(SENTINEL, 'MATERIAL_PROJECT_LIMIT'))
        for index, error in enumerate(errors):
            name, valid = 'capacity-' + str(index) + '.txt', 'valid-' + str(index) + '.txt'
            def fail(actual, path, filename, kwargs):
                if filename == name:
                    raise error
                return actual(path, filename=filename, **kwargs)
            self.parser_override = fail
            with self.subTest(error_type=type(error).__name__):
                job = self.run_job(self.upload([(name, b'Self-created capacity input'), (valid, GOOD_TEXT)]))
                self.assert_failed_batch(job)
                self.assert_file_reason(name, 'MATERIAL_PROJECT_LIMIT')
                self.assert_good_text(valid)

    def test_successful_projection_does_not_expose_stale_error_message(self):
        for status in ('completed', 'pending', 'processing'):
            with self.subTest(status=status):
                item = {'id': 'self-created-stale', 'filename': 'stale.txt', 'status': status,
                    'error_message': SENTINEL, 'error_code': 'invalid_pdf', 'chunks': [], 'source_assets': []}
                public = self.workflow._public_material(self.project, item)
                self.assertNotIn('error_message', public)
                self.assertNotIn(SENTINEL, json.dumps(public, ensure_ascii=False))

    def test_unknown_job_fallback_is_preserved_but_parse_fallback_is_local(self):
        self.assertEqual(failure_message('unknown_job_failure'),
            '模型处理未完成。材料和已完成页面保留；请根据失败原因处理后继续。')
        self.assertEqual(material_error_code('unknown_parser_failure'), 'MATERIAL_PARSE_FAILED')
        self.assertNotIn('模型处理未完成', failure_message('MATERIAL_PARSE_FAILED'))


if __name__ == '__main__':
    unittest.main()
