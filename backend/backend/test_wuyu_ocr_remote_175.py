"""op 175: DeepSeek official OCR through the private gateway, with local fallback."""
import io
import unittest
from unittest import mock

from PIL import Image

from services import wuyu_ocr


class FakeResponse:
    def __init__(self, status, value=None):
        self.status_code, self._value = status, value

    def json(self):
        if isinstance(self._value, Exception):
            raise self._value
        return self._value


class FakeClient:
    calls = []

    def __init__(self, response):
        self.response = response

    def __call__(self, **options):
        FakeClient.calls.append(('client', options))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, data=None, files=None):
        FakeClient.calls.append(('post', url, headers, data, files))
        return self.response


def image():
    return Image.new('RGB', (400, 200), 'white')


CONFIG = {'TESTING': True}


class RemoteOcrTests(unittest.TestCase):
    def setUp(self):
        FakeClient.calls = []
        patcher = mock.patch.object(wuyu_ocr, '_remote_transport', return_value=('http://host.docker.internal:18140', 'k' * 40))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_with(self, response, local=None):
        config = {**CONFIG, 'WUYU_OCR_CLIENT_FACTORY': FakeClient(response)}
        local = local or {'available': False, 'status': 'pending', 'lines': [], 'reason': 'local_ocr_executable_unavailable'}
        with mock.patch.object(wuyu_ocr, 'local_ocr_image', return_value=local) as local_call:
            return wuyu_ocr.ocr_image(image(), config), local_call

    def test_remote_lines_from_markdown_and_html_table(self):
        text = '# 标题\n\n* 要点一\n<table>\n<tr>\n<td>项目</td>\n<td>2024-2028</td>\n</tr>\n</table>\n重复\n重复\n'
        result, local = self.run_with(FakeResponse(200, {'pages': [{'index': 0, 'text': text, 'finish_reason': 'stop'}]}))
        self.assertEqual([line['text'] for line in result['lines']], ['标题', '* 要点一', '项目 | 2024-2028', '重复'])
        self.assertEqual(result['engine'], 'deepseek_ocr')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['lines'][0]['region'], [0, 0, 400, 200])
        local.assert_not_called()
        post = [c for c in FakeClient.calls if c[0] == 'post'][0]
        self.assertEqual(post[1], 'http://host.docker.internal:18140/v1/tools/ocr')
        self.assertEqual(post[3], {'purpose': 'text_recognition'})
        self.assertTrue(post[4]['file'][1].startswith(b'\x89PNG'))

    def test_gateway_without_ocr_route_falls_back_to_local(self):
        local = {'available': True, 'status': 'completed', 'lines': [{'text': 'x', 'confidence': .5, 'region': [0, 0, 1, 1]}], 'engine': 'local_tesseract'}
        result, call = self.run_with(FakeResponse(404), local)
        self.assertEqual(result['engine'], 'local_tesseract')
        call.assert_called_once()

    def test_remote_failure_without_local_reports_reason(self):
        result, _ = self.run_with(FakeResponse(500))
        self.assertEqual(result['lines'], [])
        self.assertEqual(result['reason'], 'remote_ocr_failed')

    def test_truncated_page_flagged_and_loop_lines_collapsed(self):
        text = '\n'.join(['同一行'] * 50 + ['下一行'])
        result, _ = self.run_with(FakeResponse(200, {'pages': [{'index': 0, 'text': text, 'finish_reason': 'length'}]}))
        self.assertEqual([line['text'] for line in result['lines']], ['同一行', '下一行'])
        self.assertTrue(result['output_truncated'])

    def test_invalid_json_is_pending(self):
        result, _ = self.run_with(FakeResponse(200, ValueError('bad')))
        self.assertEqual(result['reason'], 'remote_ocr_invalid_response')



class TransportTests(unittest.TestCase):
    def test_local_provider_disables_remote(self):
        self.assertIsNone(wuyu_ocr._remote_transport({'WUYU_OCR_PROVIDER': 'local'}))

    def test_unknown_provider_rejected(self):
        from services.wuyu_errors import PipelineError
        with self.assertRaises(PipelineError):
            wuyu_ocr._remote_transport({'WUYU_OCR_PROVIDER': 'other'})

    def test_no_gateway_means_no_remote(self):
        import os
        keys = {k: '' for k in ('WUYU_MODEL_GATEWAY_FORCE_ORIGIN', 'WUYU_MODEL_GATEWAY_API_KEY')}
        with mock.patch.dict(os.environ, keys):
            self.assertIsNone(wuyu_ocr._remote_transport({}))


if __name__ == '__main__':
    unittest.main()
