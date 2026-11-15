"""Cloud editing (ONLYOFFICE Docs) routes.

User routes sit under /api/projects/<id>/workflow/office (global JWT and
ownership checks apply). /office/editor/<id> is a page reached through the
authenticated /agent-api/ proxy and re-checks the user itself. /office-bridge/*
is called only by the Document Server over the internal network and is
authorized by short-lived signed bridge tokens, never by user cookies.
"""
from flask import Blueprint, Response, current_app, request, send_file, jsonify
from models import db, Project
from utils import success_response, error_response
from controllers.wuyu_workflow_controller import _service
from services.wuyu_office import (OfficeError, editor_config, editor_page, read_bridge_token, verify_callback,
                                  download_saved, document_key, FILE_AUDIENCE, CALLBACK_AUDIENCE,
                                  SAVE_STATUSES, NOOP_STATUSES, ERROR_STATUSES)

wuyu_office_bp = Blueprint('wuyu_office', __name__)


def _office_error(error):
    return error_response(error.code, str(error), error.status)


@wuyu_office_bp.get('/api/projects/<project_id>/workflow/office/status')
def office_status(project_id):
    service, error = _service(project_id)
    if error is not None:
        return error
    return success_response(service.office_status(project_id))


@wuyu_office_bp.get('/api/projects/<project_id>/workflow/office/config')
def office_config(project_id):
    service, error = _service(project_id)
    if error is not None:
        return error
    try:
        body, (artifact, _kind) = service.office_source(project_id)
        from services.wuyu_delivery_names import delivery_name
        title = delivery_name(artifact, body) or 'presentation.pptx'
        return success_response(editor_config(current_app.config, project_id, artifact, title, request.current_user))
    except OfficeError as exc:
        return _office_error(exc)


@wuyu_office_bp.get('/office/editor/<project_id>')
def office_editor(project_id):
    from middleware.auth import _load_current_user
    user, error = _load_current_user()
    if error is not None:
        return error
    project = db.session.get(Project, project_id)
    if not project or (user.role != 'admin' and project.user_id != user.id):
        return error_response('FORBIDDEN', 'Access denied', 403)
    response = Response(editor_page(project_id), mimetype='text/html')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


@wuyu_office_bp.get('/office-bridge/file/<token>')
def office_file(token):
    try:
        claims = read_bridge_token(current_app.config, token, FILE_AUDIENCE)
        service = current_app.extensions['wuyu_workflow']
        _, body = service._read(claims['pid'])
        artifact = next((a for a in body['artifacts'] if a['id'] == claims['aid']), None)
        if not artifact or document_key(claims['pid'], artifact) != claims['key']:
            raise OfficeError('Document is no longer available', 'OFFICE_SOURCE_MISSING', 404)
        path, _mime = service.artifact_path(claims['pid'], claims['aid'])
        return send_file(path, mimetype='application/vnd.openxmlformats-officedocument.presentationml.presentation')
    except OfficeError as exc:
        return _office_error(exc)


@wuyu_office_bp.post('/office-bridge/callback/<token>')
def office_callback(token):
    # ONLYOFFICE expects {"error": 0} on success; any other value is retried/reported.
    try:
        claims = read_bridge_token(current_app.config, token, CALLBACK_AUDIENCE)
        payload = verify_callback(current_app.config, request.get_json(silent=True) or {}, request.headers.get('Authorization'))
        if payload.get('key') != claims['key']:
            raise OfficeError('Callback key mismatch', 'OFFICE_CALLBACK_KEY', 403)
        state = payload.get('status')
        if state in NOOP_STATUSES:
            return jsonify({'error': 0})
        if state in ERROR_STATUSES:
            current_app.logger.warning('ONLYOFFICE save error: project=%s status=%s', claims['pid'], state)
            return jsonify({'error': 0})
        if state not in SAVE_STATUSES:
            raise OfficeError('Unknown callback status', 'OFFICE_CALLBACK_STATUS', 400)
        data = download_saved(current_app.config, payload.get('url'))
        service = current_app.extensions['wuyu_workflow']
        result = service.save_office_edit(claims['pid'], claims, data, user_id=claims.get('uid'))
        current_app.logger.info('ONLYOFFICE save: project=%s saved=%s current=%s', claims['pid'],
                                result.get('saved'), result.get('current'))
        return jsonify({'error': 0})
    except OfficeError as exc:
        current_app.logger.warning('ONLYOFFICE callback rejected: code=%s', exc.code)
        return jsonify({'error': 1, 'message': exc.code}), exc.status
    except Exception as exc:  # never echo provider/server details to the caller
        current_app.logger.warning('ONLYOFFICE callback failed: type=%s', type(exc).__name__)
        return jsonify({'error': 1}), 500
