"""First-run profile from the Work questionnaire (op201).

Stores only fixed codes: the fields people work in, what they mostly do, and the deck types they make
most often (the same vocabulary as wuyu_deck_types). The first deck type is a weak default for template
recommendations when a project's own brief names no type; the project's words always win.
Never shown to other users; nothing here reaches a model prompt.
"""
from datetime import datetime
import json

from sqlalchemy import inspect

from models import db
from .wuyu_deck_types import CODES as DECK_CODES, label as deck_label

INDUSTRIES = ('solid_waste', 'environment', 'energy', 'engineering', 'research', 'education',
              'government', 'enterprise', 'other')
ROLES = ('research', 'student', 'engineering', 'management', 'consulting', 'teaching', 'other')
MAX_PICKS = 3
FIELDS = {'status', 'industries', 'role', 'deck_types'}


class UserProfileError(ValueError):
    def __init__(self, message, code='PROFILE_INVALID', status=400):
        super().__init__(message)
        self.code, self.status = code, status


def normalize(payload):
    """Validated profile dict, or UserProfileError. 'skipped' carries no answers."""
    if not isinstance(payload, dict) or not set(payload) <= FIELDS or payload.get('status') not in ('done', 'skipped'):
        raise UserProfileError('问卷内容无效。')
    if payload['status'] == 'skipped':
        if any(payload.get(key) for key in ('industries', 'role', 'deck_types')):
            raise UserProfileError('跳过的问卷不能带答案。')
        return {'version': 1, 'status': 'skipped', 'industries': [], 'role': None, 'deck_types': []}

    def picks(key, allowed):
        values = payload.get(key, [])
        if not isinstance(values, list) or len(values) > MAX_PICKS or len(set(values)) != len(values) \
                or any(not isinstance(v, str) or v not in allowed for v in values):
            raise UserProfileError('选项无效或超过 %d 个。' % MAX_PICKS)
        return list(values)
    role = payload.get('role')
    if role is not None and role not in ROLES:
        raise UserProfileError('选项无效。')
    profile = {'version': 1, 'status': 'done', 'industries': picks('industries', INDUSTRIES), 'role': role,
               'deck_types': picks('deck_types', DECK_CODES)}
    if not profile['industries'] and role is None and not profile['deck_types']:
        profile['status'] = 'skipped'
    return profile


class UserProfiles:
    def __init__(self, app):
        self.app, self.available = app, True

    def _model(self):
        from models.wuyu_user_profile import WuyuUserProfile
        return WuyuUserProfile

    def get(self, owner_id):
        """{'available', 'profile'}; profile is None until the questionnaire was answered or skipped."""
        if not self.available:
            return {'available': False, 'profile': None}
        row = db.session.get(self._model(), owner_id)
        if row is None:
            return {'available': True, 'profile': None}
        try:
            profile = normalize({k: v for k, v in json.loads(row.value).items() if k in FIELDS})
        except (ValueError, AttributeError):
            return {'available': True, 'profile': None}
        profile['updated_at'] = row.updated_at.isoformat() + 'Z'
        profile['deck_type_labels'] = [deck_label(code) for code in profile['deck_types']]
        return {'available': True, 'profile': profile}

    def save(self, owner_id, payload):
        if not self.available:
            raise UserProfileError('使用偏好暂不可用。', 'PROFILE_UNAVAILABLE', 503)
        profile = normalize(payload)
        model = self._model()
        row = db.session.get(model, owner_id)
        if row is None:
            row = model(owner_id=owner_id)
            db.session.add(row)
        row.value = json.dumps(profile, ensure_ascii=False, separators=(',', ':'))
        row.updated_at = datetime.utcnow()
        db.session.commit()
        return self.get(owner_id)

    def default_deck_type(self, owner_id):
        """The first deck type the owner said they make most often, or None. Never raises."""
        if not self.available or not owner_id:
            return None
        try:
            row = db.session.get(self._model(), owner_id)
            types = json.loads(row.value).get('deck_types') if row is not None else None
        except Exception:  # a hint only; recommendations must not fail because of it
            return None
        return types[0] if isinstance(types, list) and types and types[0] in DECK_CODES else None

    def delete(self, owner_id):
        if self.available:
            self._model().query.filter_by(owner_id=owner_id).delete()


def _register_purge():
    """A deleted account leaves no profile behind (same transaction as the user row)."""
    from sqlalchemy import delete, event
    from models import User

    @event.listens_for(User, 'after_delete')
    def _purge_profile(mapper, connection, user):
        model = UserProfiles(None)._model()
        if inspect(connection).has_table(model.__tablename__):
            connection.execute(delete(model.__table__).where(model.__table__.c.owner_id == user.id))
    return _purge_profile


_purge_profile = None


def init_user_profiles(app):
    global _purge_profile
    if _purge_profile is None:
        _purge_profile = _register_purge()
    service = UserProfiles(app)
    app.extensions['wuyu_user_profiles'] = service
    with app.app_context():
        model = service._model()
        if db.engine.dialect.name == 'sqlite':
            model.__table__.create(db.engine, checkfirst=True)
        elif not inspect(db.engine).has_table(model.__tablename__):
            # An un-migrated database still boots; the questionnaire is simply not offered.
            service.available = False
            app.logger.warning('USER_PROFILE_SCHEMA_MISSING')
    return service
