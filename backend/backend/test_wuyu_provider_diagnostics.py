"""Provider admission diagnostics: synthetic failures, no outbound SDK calls."""
import json
import unittest
from unittest.mock import patch

from services import wuyu_providers as providers
from services.wuyu_errors import PipelineError
from services.wuyu_providers import WuyuGateway
from services.wuyu_workflow_errors import failure_message
from services.wuyu_workflow_reports import _failure_diagnostics


class SyntheticSDKError(Exception):
    def __init__(self, status, body=None, *, details=None, google=False):
        super().__init__('synthetic-sensitive-message-never-persist')
        if google:
            self.code = status
        else:
            self.status_code = status
        self.body, self.details = body, details


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def gateway_body(code, stage=None):
    return {'error': {'code': code, 'stage': stage,
                      'message': 'synthetic-sensitive-body-never-persist',
                      'headers': {'Authorization': 'synthetic-sensitive-header-never-persist'}}}


class ProviderDiagnosticsTests(unittest.TestCase):
    def failure(self, error, retries=0):
        gateway, calls, clock = WuyuGateway({'OPENAI_MAX_RETRIES': retries}), [], Clock()
        def action():
            calls.append(1)
            raise error
        with patch.object(providers, 'time', clock):
            with self.assertRaises(PipelineError) as caught:
                gateway.call(action)
        self.assertNotIn('synthetic-sensitive', json.dumps(getattr(caught.exception, 'diagnostics', {})))
        self.assertNotIn('synthetic-sensitive', str(caught.exception))
        return caught.exception, len(calls), clock.now

    def test_queue_timeout_uses_local_capacity_code_after_original_retry_count(self):
        error, count, elapsed = self.failure(SyntheticSDKError(504, gateway_body('MODEL_QUEUE_TIMEOUT', 'model_queue')), 2)
        self.assertEqual((error.code, count, error.retryable), ('capacity_queue_timeout', 3, True))
        self.assertAlmostEqual(elapsed, 1.55, delta=.11)
        self.assertEqual(error.diagnostics, {'http_status': 504, 'gateway_code': 'MODEL_QUEUE_TIMEOUT',
                                          'attempt_count': 3, 'stage': 'model_queue'})

    def test_byte_wait_timeout_is_local_and_google_nested_details_work(self):
        error, count, _ = self.failure(SyntheticSDKError(504, details=gateway_body('MODEL_QUEUE_TIMEOUT', 'body_bytes'), google=True))
        self.assertEqual((error.code, count), ('capacity_queue_timeout', 1))
        self.assertEqual(error.diagnostics['stage'], 'body_bytes')

    def test_queue_full_is_not_upstream_rate_limit(self):
        for code, stage in [('MODEL_QUEUE_FULL', 'model_queue'), ('MODEL_BODY_QUEUE_FULL', 'body_bytes')]:
            with self.subTest(code=code):
                error, count, _ = self.failure(SyntheticSDKError(429, gateway_body(code, stage)))
                self.assertEqual((error.code, count), ('capacity_queue_full', 1))
                self.assertTrue(error.retryable)

    def test_upstream_timeout_maps_to_timeout_with_safe_stage(self):
        error, _, _ = self.failure(SyntheticSDKError(504, gateway_body('MODEL_UPSTREAM_TIMEOUT', 'upstream')))
        self.assertEqual(error.code, 'provider_timeout')
        self.assertEqual(error.diagnostics['stage'], 'upstream')

    def test_local_request_body_timeout_keeps_408_no_retry_policy(self):
        error, count, _ = self.failure(SyntheticSDKError(408, gateway_body('MODEL_REQUEST_BODY_TIMEOUT', 'body_read')), 2)
        self.assertEqual((error.code, count, error.retryable), ('capacity_request_body_timeout', 1, False))
        self.assertEqual(error.diagnostics['stage'], 'body_read')

    def test_legacy_ambiguous_timeout_keeps_existing_classification(self):
        error, _, _ = self.failure(SyntheticSDKError(504, gateway_body('MODEL_TIMEOUT', 'upstream')))
        self.assertEqual(error.code, 'provider_unavailable')
        self.assertEqual(error.diagnostics['stage'], 'gateway_unknown')

    def test_openai_unwrapped_body_and_fixed_stage_fallback(self):
        error, _, _ = self.failure(SyntheticSDKError(504, {'code': 'MODEL_QUEUE_TIMEOUT', 'stage': '/tmp/private-sensitive'}))
        self.assertEqual(error.code, 'capacity_queue_timeout')
        self.assertEqual(error.diagnostics['stage'], 'capacity_queue')

    def test_existing_http_classification_is_unchanged(self):
        codes = {400: 'provider_request_rejected', 401: 'provider_auth_failed', 403: 'provider_permission_denied',
                 404: 'provider_model_unavailable', 408: 'provider_failed', 413: 'provider_request_too_large', 422: 'provider_request_rejected',
                 429: 'provider_rate_limit', 500: 'provider_unavailable', 502: 'provider_unavailable',
                 503: 'provider_unavailable', 504: 'provider_unavailable'}
        for status, expected in codes.items():
            with self.subTest(status=status):
                error, _, _ = self.failure(SyntheticSDKError(status, {'code': 'untrusted-sensitive-code'}))
                self.assertEqual(error.code, expected)
                self.assertNotIn('gateway_code', error.diagnostics)

    def test_gateway_markers_require_matching_http_status_for_reclassification(self):
        error, _, _ = self.failure(SyntheticSDKError(400, gateway_body('MODEL_QUEUE_TIMEOUT', 'model_queue')))
        self.assertEqual(error.code, 'provider_request_rejected')

    def test_client_timeout_and_unknown_failures_keep_separate_classifications(self):
        timeout = type('APITimeoutError', (Exception,), {})('synthetic-sensitive-message-never-persist')
        error, _, _ = self.failure(timeout)
        self.assertEqual((error.code, error.diagnostics['stage']), ('provider_timeout', 'client_timeout'))
        error, _, _ = self.failure(Exception('synthetic-sensitive-message-never-persist'))
        self.assertEqual(error.diagnostics, {'attempt_count': 1, 'stage': 'provider'})
        self.assertEqual(error.code, 'provider_failed')

    def test_malformed_body_cannot_leak_or_break_mapping(self):
        for body in [None, 'synthetic-sensitive-body', ['synthetic-sensitive-body'], {'code': []}, {'error': {'code': {}}}]:
            with self.subTest(body_type=type(body).__name__):
                error, _, _ = self.failure(SyntheticSDKError(504, body))
                self.assertEqual(error.code, 'provider_unavailable')
                self.assertNotIn('gateway_code', error.diagnostics)

    def test_pipeline_error_and_cancellation_are_not_reclassified(self):
        original = PipelineError('invalid_image_response', 'fixed fixture')
        error, count, _ = self.failure(original)
        self.assertIs(error, original)
        self.assertEqual(count, 1)
        gateway = WuyuGateway({})
        with self.assertRaises(PipelineError) as caught:
            gateway.call(lambda: self.fail('Cancelled work must not dispatch'), lambda: True)
        self.assertEqual(caught.exception.code, 'cancelled')

    def test_public_diagnostics_allow_only_fixed_values(self):
        value = {'http_status': 504, 'attempt_count': 3, 'gateway_code': 'MODEL_QUEUE_TIMEOUT', 'stage': 'model_queue',
                 'message': 'synthetic-sensitive-message', 'body': {'code': 'arbitrary'}, 'headers': {'Authorization': 'synthetic-sensitive-header'}}
        self.assertEqual(_failure_diagnostics(value), {k: value[k] for k in ('http_status', 'attempt_count', 'gateway_code', 'stage')})
        self.assertEqual(_failure_diagnostics({'http_status': True, 'attempt_count': 99,
            'gateway_code': 'untrusted', 'stage': '/tmp/private'}), {})

    def test_fixed_capacity_guidance_keeps_materials_and_does_not_blame_upstream(self):
        for code in ('capacity_queue_timeout', 'capacity_queue_full'):
            message = failure_message(code)
            self.assertIn('本地', message)
            self.assertIn('保留', message)
            self.assertNotIn('模型服务暂不可用', message)


class ProviderDurableDiagnosticsTests(unittest.TestCase):
    def execute_failure(self, gateway_code, status, expected):
        # Reuse self-created material/template setup without inheriting its tests.
        import test_wuyu_page_retry as fixture
        from models import db, Task
        case = fixture.PageRetryTests('runTest')
        case.setUp()
        self.addCleanup(case.tearDown)
        gateway = WuyuGateway({'OPENAI_MAX_RETRIES': 0})
        def image(*args):
            def action():
                raise SyntheticSDKError(status, gateway_body(gateway_code, 'model_queue' if 'QUEUE' in gateway_code else 'upstream'))
            return gateway.call(action)
        case.image = image
        queued = case.queued_job(case.action('generate_images', {'generation_requested': True}, 'safe-diagnostic-fixture'))
        with self.assertLogs(case.app.logger, level='WARNING') as logs:
            job = case.run_job(queued)
        self.assertEqual((job.status, job.error_code), ('failed', expected))
        task = db.session.get(Task, job.task_id)
        self.assertEqual((task.status, task.error_message), ('FAILED', expected))
        public = case.workflow.get_job(case.project, job.id)
        self.assertEqual(public['diagnostics']['gateway_code'], gateway_code)
        self.assertEqual(public['diagnostics']['attempt_count'], 1)
        self.assertEqual(json.loads(job.progress)['error']['diagnostics'], public['diagnostics'])
        self.assertNotIn('synthetic-sensitive', json.dumps(public))
        self.assertNotIn('synthetic-sensitive', '\n'.join(logs.output))
        self.assertIn('job=' + job.id, '\n'.join(logs.output))

    def test_local_queue_diagnostics_survive_job_failure_and_public_projection(self):
        self.execute_failure('MODEL_QUEUE_TIMEOUT', 504, 'capacity_queue_timeout')

    def test_upstream_timeout_keeps_distinct_persisted_cause(self):
        self.execute_failure('MODEL_UPSTREAM_TIMEOUT', 504, 'provider_timeout')


if __name__ == '__main__':
    unittest.main()
