#!/usr/bin/env python3
"""Run inside the existing backend container AFTER deployment is switched.

Only login, project CRUD, workflow reads, local TXT parsing, reference selection
and protected downloads are allowed. Never calls any model/provider endpoint.
Credentials are read from the existing process environment, or an explicitly
named private JSON file; their values and API responses are never printed.

Example (backend container, working directory /app/backend):
  uv run python scripts/wuyu_deployment_smoke.py --report /tmp/wuyu-smoke.json
Environment: BANANA_AGENT_USERNAME / BANANA_AGENT_PASSWORD. Optional existing
non-admin isolation account: BANANA_SMOKE_OTHER_USERNAME / ..._PASSWORD.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class SmokeFailure(Exception):
    """Only constant, safe messages may be emitted to the report."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base_url, timeout=15):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or parsed.username or parsed.password or parsed.path not in {'', '/'}
                or parsed.query or parsed.fragment):
            raise SmokeFailure('base_url_must_be_container_loopback_http')
        self.base = base_url.rstrip('/')
        self.timeout = timeout
        self.token = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, data=None, *, raw=None, content_type=None, auth=True, expected=(200,)):
        if not path.startswith('/') or path.startswith('//') or '\\' in path:
            raise SmokeFailure('unsafe_request_path')
        if method == 'POST':
            if path.endswith('/workflow/action'):
                if not isinstance(data, dict) or data.get('action') not in {'update_brief', 'select_references'}:
                    raise SmokeFailure('generating_action_prohibited')
            elif not (path == '/api/auth/login' or path == '/api/projects' or path.endswith('/workflow/materials')):
                raise SmokeFailure('unexpected_post_prohibited')
        headers = {'Accept': 'application/json'}
        if auth and self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        if data is not None:
            raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
            content_type = 'application/json'
        if content_type:
            headers['Content-Type'] = content_type
        req = urllib.request.Request(self.base + path, data=raw, method=method, headers=headers)
        try:
            response = self.opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            response = exc
        except (OSError, urllib.error.URLError):
            raise SmokeFailure('backend_connection_failed') from None
        with response:
            status = response.code
            body = response.read(4 * 1024 * 1024 + 1)
            content = response.headers.get('Content-Type', '')
        if status not in expected:
            raise SmokeFailure('unexpected_http_status_' + str(status))
        if len(body) > 4 * 1024 * 1024:
            raise SmokeFailure('response_size_limit')
        if 'application/json' in content:
            try:
                return status, json.loads(body)
            except (ValueError, UnicodeError):
                raise SmokeFailure('invalid_json_response') from None
        return status, body

    def data(self, method, path, data=None, **kwargs):
        _, body = self.request(method, path, data, **kwargs)
        if not isinstance(body, dict) or body.get('success') is not True or 'data' not in body:
            raise SmokeFailure('unexpected_api_envelope')
        return body['data']


def credentials(prefix, private_file=None, optional=False):
    username = os.environ.get(prefix + '_USERNAME', '')
    password = os.environ.get(prefix + '_PASSWORD', '')
    if private_file:
        path = Path(private_file)
        if not path.is_file() or path.stat().st_size > 128 * 1024:
            raise SmokeFailure('invalid_private_credentials_file')
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
            username, password = value['username'], value['password']
        except (OSError, ValueError, KeyError, TypeError):
            raise SmokeFailure('invalid_private_credentials_file') from None
    if not username and not password and optional:
        return None
    if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
        raise SmokeFailure('existing_account_credentials_required')
    return username, password


def login(client, account):
    result = client.data('POST', '/api/auth/login', {'username': account[0], 'password': account[1]}, auth=False)
    if not isinstance(result, dict) or not isinstance(result.get('token'), str) or not result.get('user', {}).get('id'):
        raise SmokeFailure('invalid_login_result')
    client.token = result['token']
    return result['user']


def builtin_reference(explicit_catalog=None):
    configured = explicit_catalog or next((os.environ[k] for k in
        ('WUYU_TEMPLATE_LIBRARY_PATH', 'WUYU_TEMPLATE_LIBRARY_ROOT', 'WUYU_TEMPLATE_CATALOG', 'WUYU_LIBRARY_ROOT')
        if os.environ.get(k)), None)
    candidates = [Path(configured)] if configured else [Path('/app/template_library')] + [
        ancestor / 'template_library' for ancestor in Path(__file__).resolve().parents]
    for candidate in candidates:
        catalog = candidate if candidate.suffix.lower() == '.json' else candidate / 'catalog.json'
        if not catalog.is_file():
            continue
        try:
            value = json.loads(catalog.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise SmokeFailure('invalid_builtin_catalog') from None
        root = catalog.parent.resolve()
        for source in value.get('sources', []):
            for page in source.get('pages', []):
                if not isinstance(page.get('image'), str):
                    continue
                image = (root / page['image']).resolve()
                if (image.is_relative_to(root) and image.is_file() and source.get('id') and page.get('id')
                        and image.suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}):
                    return {'source_id': source['id'], 'page_id': page['id']}, len(value.get('sources', []))
        raise SmokeFailure('no_rendered_builtin_reference')
    raise SmokeFailure('builtin_catalog_not_mounted')


def safe_snapshot(snapshot, secrets):
    text = json.dumps(snapshot, ensure_ascii=False)
    if any(secret and len(secret) >= 8 and secret in text for secret in secrets):
        raise SmokeFailure('credential_in_snapshot')
    if re.search(r'(?:[A-Za-z]:[\\/]|/(?:app|home|root|data)/)', text):
        raise SmokeFailure('private_disk_path_in_snapshot')
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in {'api_key', 'password', 'secret', 'authorization', 'access_token', 'refresh_token'} and item:
                    raise SmokeFailure('secret_field_in_snapshot')
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(snapshot)


def run(args, report):
    client = Client(args.base_url)
    account = credentials(args.credential_prefix, args.credential_file)
    other_account = credentials('BANANA_SMOKE_OTHER', optional=True)
    user = login(client, account)
    me = client.data('GET', '/api/auth/me')
    if me.get('id') != user['id']:
        raise SmokeFailure('login_owner_mismatch')
    report['checks']['existing_account_login'] = True
    reference, count = builtin_reference(args.catalog)
    report['builtin_source_count'] = count
    report['checks']['builtin_reference_readable'] = True
    marker = 'WUYU_DEPLOY_SMOKE_' + uuid.uuid4().hex
    project_id = None
    try:
        project = client.data('POST', '/api/projects', {'creation_type': 'idea', 'idea_prompt': marker}, expected=(201, 200))
        project_id = project.get('project_id') or project.get('id')
        if not isinstance(project_id, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', project_id):
            raise SmokeFailure('invalid_created_project_id')
        project = client.data('GET', '/api/projects/' + project_id)
        if project.get('user_id') != user['id']:
            raise SmokeFailure('created_project_wrong_owner')
        url = '/api/projects/' + project_id
        workflow_url = url + '/workflow'
        snapshot = client.data('GET', workflow_url)
        safe_snapshot(snapshot, [account[1], client.token])
        report['checks']['workflow_initialized_without_generation'] = not snapshot['jobs'] and not snapshot['artifacts']
        if not report['checks']['workflow_initialized_without_generation']:
            raise SmokeFailure('fresh_workflow_has_unexpected_work')
        snapshot = client.data('POST', workflow_url + '/action', {
            'action': 'update_brief', 'payload': {'brief': {'topic': marker, 'scenario': 'commercial',
                'audience': '部署验证', 'page_count': 3, 'style': '内置无隅模板',
                'requirements': '部署后本地检查，不生成演示文稿。'}, 'review_policy': 'guided'},
            'expected_revision': snapshot['revision'], 'request_id': uuid.uuid4().hex})
        snapshot = client.data('POST', workflow_url + '/action', {'action': 'select_references',
            'payload': {'reference_recipe': {'style_anchor_id': reference['page_id'],
                'source_ids': [reference['source_id']], 'page_refs': [reference]}},
            'expected_revision': snapshot['revision'], 'request_id': uuid.uuid4().hex})
        if snapshot['jobs'] or not snapshot['reference_recipe'].get('style_anchor'):
            raise SmokeFailure('reference_save_started_unexpected_work')
        report['checks']['save_brief_and_builtin_reference'] = True
        payload = ('Deployment smoke local evidence.\n' + marker + '\n').encode('utf-8')
        boundary = 'wuyu-smoke-' + uuid.uuid4().hex
        multipart = ('--' + boundary + '\r\nContent-Disposition: form-data; name="role"\r\n\r\nmaterial\r\n'
            + '--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="deployment-smoke.txt"\r\n'
            + 'Content-Type: text/plain\r\n\r\n').encode() + payload + ('\r\n--' + boundary + '--\r\n').encode()
        client.request('POST', workflow_url + '/materials', raw=multipart,
            content_type='multipart/form-data; boundary=' + boundary, expected=(200, 201, 202))
        deadline = time.monotonic() + args.wait_seconds
        while True:
            snapshot = client.data('GET', workflow_url)
            if any(job['action'] != '_parse_materials' for job in snapshot['jobs']):
                raise SmokeFailure('unexpected_provider_job_created')
            if any(job['status'] in {'failed', 'interrupted', 'cancelled'} for job in snapshot['jobs']):
                raise SmokeFailure('local_material_parse_failed')
            materials = snapshot['materials']
            if materials and all(item['status'] == 'completed' for item in materials) and all(
                    job['status'] == 'completed' for job in snapshot['jobs']):
                break
            if time.monotonic() >= deadline:
                raise SmokeFailure('local_material_parse_timeout')
            time.sleep(.4)
        safe_snapshot(snapshot, [account[1], client.token])
        if not any(marker in chunk.get('text', '') for item in materials for chunk in item.get('chunks', [])):
            raise SmokeFailure('local_evidence_text_missing')
        material = materials[0]
        artifact = next((a for a in snapshot['artifacts'] if a['id'] == material['artifact_id']), None)
        if not artifact or artifact.get('kind') != 'source_material':
            raise SmokeFailure('source_artifact_missing')
        download = artifact['download_url']
        if not download.startswith(workflow_url + '/artifacts/'):
            raise SmokeFailure('source_artifact_scope_invalid')
        _, downloaded = client.request('GET', download)
        if downloaded != payload or hashlib.sha256(downloaded).hexdigest() != material['sha256']:
            raise SmokeFailure('downloaded_source_mismatch')
        client.request('GET', download, auth=False, expected=(401,))
        client.request('GET', workflow_url, auth=False, expected=(401,))
        client.request('GET', '/files/' + project_id + '/materials/deployment-smoke.txt', auth=False, expected=(401,))
        client.request('GET', workflow_url + '/artifacts/' + uuid.uuid4().hex, expected=(404,))
        report['checks'].update(local_txt_parse=True, owner_download_integrity=True,
            anonymous_workflow_denied=True, anonymous_artifact_denied=True,
            anonymous_legacy_file_denied=True, unknown_artifact_denied=True, snapshot_private=True,
            no_provider_jobs=True)
        if other_account:
            other = Client(args.base_url)
            other_user = login(other, other_account)
            if other_user.get('role') == 'admin' or other_user.get('id') == user['id']:
                raise SmokeFailure('isolation_account_must_be_distinct_existing_employee')
            other.request('GET', workflow_url, expected=(403,))
            other.request('GET', download, expected=(403,))
            report['checks']['cross_user_owner_isolation'] = True
        else:
            report['not_checked'].append('cross_user_owner_isolation_no_existing_employee_credentials')
    finally:
        # Never delete by listing, guessed IDs or a caller-supplied project ID.
        # Re-read both the exact nonce marker and the authenticated owner first.
        if project_id:
            project = client.data('GET', '/api/projects/' + project_id)
            if project.get('idea_prompt') != marker or project.get('user_id') != user['id']:
                raise SmokeFailure('cleanup_refused_marker_or_owner_mismatch')
            client.request('DELETE', '/api/projects/' + project_id)
            client.request('GET', '/api/projects/' + project_id, expected=(404,))
            report['checks']['only_created_project_deleted'] = True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--base-url', default='http://127.0.0.1:5000')
    parser.add_argument('--credential-prefix', default='BANANA_AGENT')
    parser.add_argument('--credential-file', help='Existing private JSON with username/password; values never printed')
    parser.add_argument('--catalog', help='Mounted catalog.json or library directory')
    parser.add_argument('--wait-seconds', type=int, default=60)
    parser.add_argument('--report', default='/tmp/wuyu-deployment-smoke.json')
    args = parser.parse_args(argv)
    if not 1 <= args.wait_seconds <= 180:
        parser.error('wait-seconds must be between 1 and 180')
    report = {'schema_version': 1, 'started_at': datetime.now(timezone.utc).isoformat(),
        'status': 'failed', 'provider_calls': 0, 'checks': {}, 'not_checked': [],
        'scope': 'loopback existing-account local-material workflow smoke'}
    try:
        run(args, report)
        report['status'] = 'passed'
    except SmokeFailure as exc:
        report['error_code'] = str(exc)
    except Exception:
        report['error_code'] = 'unexpected_smoke_error_details_suppressed'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    try:
        path = Path(args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.chmod(path, 0o600)
    except OSError:
        print('wuyu-deployment-smoke: report_write_failed', file=sys.stderr)
        return 1
    print('wuyu-deployment-smoke: ' + report['status'] + '; provider_calls=0; checks=' + str(len(report['checks'])))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
