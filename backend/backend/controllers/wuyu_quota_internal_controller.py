"""Account-bound text metering for trusted Host processes; never a browser billing API."""
from __future__ import annotations

import json
import hmac
import os
import re

from flask import Blueprint, current_app, request
from models import db
from models.wuyu_quota import QuotaReservation
from middleware.auth import jwt_required
from services.wuyu_quota import QuotaError, get_quota_service, text_points
from utils import error_response, success_response


wuyu_quota_internal_bp = Blueprint('wuyu_quota_internal', __name__, url_prefix='/api/internal/quota')


def _tokens(data, key):
    value = data.get(key)
    if type(value) is not int or not 0 <= value <= 2_000_000:
        raise ValueError('Invalid token count')
    return value


@wuyu_quota_internal_bp.post('/command')
@jwt_required
def quota_command():
    expected = current_app.config.get('WUYU_QUOTA_INTERNAL_KEY') or os.getenv('WUYU_QUOTA_INTERNAL_KEY')
    supplied = request.headers.get('X-Wuyu-Internal-Key', '')
    if not isinstance(expected, str) or len(expected) < 32 or not hmac.compare_digest(expected, supplied):
        return error_response('FORBIDDEN', 'Trusted Host authentication required', 403)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error_response('INVALID_REQUEST', 'Expected an object', 400)
    user_id = request.current_user.id
    service = get_quota_service()
    try:
        action = data.get('action')
        if action == 'reserve':
            if set(data) != {'action', 'operation_key', 'input_tokens', 'output_tokens'}:
                raise ValueError('Unexpected billing fields')
            key = data.get('operation_key')
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9:_-]{1,100}', key):
                raise ValueError('Invalid operation key')
            result = service.reserve(user_id, 'host:' + key, 'text',
                input_tokens=_tokens(data, 'input_tokens'), output_tokens=_tokens(data, 'output_tokens'))
        elif action in {'settle', 'refund'}:
            allowed = {'action', 'reservation_id'} | ({'input_tokens', 'output_tokens'} if action == 'settle' else set())
            if set(data) != allowed:
                raise ValueError('Unexpected billing fields')
            ident = data.get('reservation_id')
            if not isinstance(ident, str) or len(ident) > 64:
                raise ValueError('Invalid reservation')
            reservation = db.session.get(QuotaReservation, ident)
            if reservation is None or reservation.user_id != user_id or reservation.kind != 'text' or not reservation.operation_key.startswith('host:'):
                return error_response('NOT_FOUND', 'Reservation not found', 404)
            if action == 'settle':
                cost = text_points(_tokens(data, 'input_tokens'), _tokens(data, 'output_tokens'), json.loads(reservation.tariff_json) if reservation.tariff_json else None)
                result = service.settle(ident, actual_points=cost)
            else:
                result = service.refund(ident, reason='host_request_incomplete')
        else:
            raise ValueError('Unknown billing command')
        return success_response({'reservation': result})
    except ValueError:
        return error_response('INVALID_REQUEST', 'Invalid metering command', 400)
    except QuotaError as exc:
        return error_response(exc.code, str(exc), exc.status)
