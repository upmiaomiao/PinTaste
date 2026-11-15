"""Actual middleware/ownership contracts; SQLite and disposable real PG only."""
import unittest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from official import app_main as main, store as db
from official import test_model_budget as admission_fixture
from official.test_pg_worker_regressions import _PostgresFixtureMixin


class EmptyDeleteTests(unittest.TestCase):
    setUp = admission_fixture.ModelBudgetAdmissionTests.setUp
    tearDown = admission_fixture.ModelBudgetAdmissionTests.tearDown
    new_user = admission_fixture.ModelBudgetAdmissionTests.new_user
    conversation = admission_fixture.ModelBudgetAdmissionTests.conversation

    def target(self):
        cid = self.conversation()
        return cid, '/api/conversations/' + cid

    def present(self, cid):
        self.assertEqual(db.query('SELECT deleted FROM conversations WHERE id=?', (cid,), one=True)['deleted'], 0)

    def test_own_empty_delete_without_JSON_header_is_accepted(self):
        cid, path = self.target()
        self.assertEqual(self.client.delete(path, headers=self.user['headers']).status_code, 200)
        self.assertEqual(db.query('SELECT deleted FROM conversations WHERE id=?', (cid,), one=True)['deleted'], 1)

    def test_existing_JSON_delete_stays_accepted(self):
        cid, path = self.target()
        self.assertEqual(self.client.request('DELETE', path, json={}, headers=self.user['headers']).status_code, 200)

    def test_nonempty_nonJSON_delete_stays_rejected(self):
        cid, path = self.target()
        result = self.client.request('DELETE', path, content=b'nonempty', headers=self.user['headers'])
        self.assertEqual(result.status_code, 415);self.present(cid)

    def test_false_zero_content_length_does_not_bypass_body_check(self):
        cid, path = self.target()
        result = self.client.request('DELETE', path, content=b'nonempty', headers={**self.user['headers'], 'Content-Length': '0'})
        self.assertEqual(result.status_code, 415);self.present(cid)

    def test_nonempty_chunked_nonJSON_delete_is_rejected(self):
        cid, path = self.target()
        result = self.client.request('DELETE', path, content=iter([b'first', b'second']), headers=self.user['headers'])
        self.assertEqual(result.status_code, 415);self.present(cid)

    def test_truly_empty_body_does_not_require_a_specific_mime_type(self):
        cid, path = self.target()
        result = self.client.delete(path, headers={**self.user['headers'], 'Content-Type': 'text/plain'})
        self.assertEqual(result.status_code, 200)

    def test_anonymous_empty_delete_still_requires_authentication(self):
        cid, path = self.target()
        self.assertEqual(self.client.delete(path).status_code, 401);self.present(cid)

    def test_cross_origin_empty_delete_remains_forbidden(self):
        cid, path = self.target()
        result = self.client.delete(path, headers={**self.user['headers'], 'Origin': 'https://untrusted.invalid'})
        self.assertEqual(result.status_code, 403);self.present(cid)

    def test_cross_site_empty_delete_remains_forbidden(self):
        cid, path = self.target()
        result = self.client.delete(path, headers={**self.user['headers'], 'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(result.status_code, 403);self.present(cid)

    def test_other_accounts_conversation_remains_hidden_and_unchanged(self):
        cid, path = self.target();other = self.new_user('other-self-fixture')
        self.assertEqual(self.client.delete(path, headers=other['headers']).status_code, 404);self.present(cid)

    def test_nonexistent_conversation_reaches_404_instead_of_415(self):
        result = self.client.delete('/api/conversations/self-created-nonexistent-129', headers=self.user['headers'])
        self.assertEqual(result.status_code, 404)

    def test_POST_PUT_PATCH_still_require_JSON(self):
        for method in ['POST', 'PUT', 'PATCH']:
            with self.subTest(method=method):
                result = self.client.request(method, '/api/conversations', headers=self.user['headers'])
                self.assertEqual(result.status_code, 415)

    def test_invalid_length_does_not_make_empty_DELETE_exception(self):
        cid, path = self.target()
        result = self.client.delete(path, headers={**self.user['headers'], 'Content-Length': '-1', 'Content-Type': 'application/json'})
        self.assertEqual(result.status_code, 415);self.present(cid)

    def test_oversized_DELETE_remains_rejected_before_handler(self):
        cid, path = self.target()
        result = self.client.delete(path, headers={**self.user['headers'], 'Content-Length': '3000001', 'Content-Type': 'application/json'})
        self.assertEqual(result.status_code, 415);self.present(cid)

    def test_collection_DELETE_uses_method_contract_instead_of_upload_limit(self):
        self.assertEqual(self.client.delete('/api/files', headers=self.user['headers']).status_code, 405)

    def test_downstream_reader_still_observes_empty_body(self):
        app = FastAPI();app.middleware('http')(main.guard)
        @app.delete('/self-created-body-fixture')
        async def endpoint(request: Request):
            return {'bytes': len(await request.body())}
        with TestClient(app, base_url='http://127.0.0.1:8920') as client:
            result = client.delete('/self-created-body-fixture')
        self.assertEqual(result.status_code, 200);self.assertEqual(result.json(), {'bytes': 0})


class PGEmptyDeleteTests(_PostgresFixtureMixin, EmptyDeleteTests):
    pass


if __name__ == '__main__':unittest.main()
