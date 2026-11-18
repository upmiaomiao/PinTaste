"""Per-user design habits: explicit, typed and user-managed design memory.

Phase 1 stores what the user states explicitly, an immutable event log and an
injection log. Nothing here calls a model; inference from events is a later
phase. Every owner-facing lookup is filtered by owner_id.
"""
from datetime import datetime
import json
import re

from flask import current_app, has_app_context
from sqlalchemy import and_, delete, event, inspect, or_, update
from sqlalchemy.exc import IntegrityError
from models import db, Project, User
from models.wuyu_design_habits import (DesignEvent, DesignHabit, DesignHabitRevision,
                                       DesignInjectionLog, DesignHabitPreference)
from models.wuyu_design_memory import DesignFeedback, DesignRuleSet
# One vocabulary for "what the deck is for": the same scenario tags the academic skill resolves.
from .wuyu_academic_skill import _ACADEMIC_SCENARIOS, _BUSINESS_SCENARIOS, _tag
from .wuyu_design_memory import DesignMemoryError, dumps


CATEGORIES = ('integration', 'layout', 'expression')
SCOPE_KEYS = ('purpose', 'page_role', 'content_type')
EVENT_KINDS = {'text_feedback', 'manual_edit', 'template_choice', 'form_change',
               'refine_accept', 'refine_reject', 'explicit'}
MAX_RULE, MAX_SCOPE_VALUE, MAX_VALUE_JSON, MAX_EVENT_JSON = 300, 60, 2000, 16000
MAX_LIVE_HABITS, MAX_STORED_HABITS, MAX_ACTIVE_INJECTED, MAX_PAGE_NUMBER = 100, 500, 12, 10000
# Answers travel through the platform bridge, which refuses a response above 1 MiB. Sizes are measured
# as escaped JSON, the largest form the answer can take on the wire.
MAX_LIST_BYTES, EXPORT_PAGE_BYTES, EXPORT_PAGE_ROWS = 800000, 600000, 500
EXPORT_CURSOR = re.compile(r'(h|e)\.(\d{8}T\d{6}\.\d{6})\.([0-9a-f-]{36})')
EXPORT_START = ('99991231T235959.999999', 'ffffffff-ffff-ffff-ffff-ffffffffffff')
# The scope value "purpose" is a code, never free text: generation compares it with the brief's scenario.
PURPOSES = (('research', '科研与学术', _ACADEMIC_SCENARIOS), ('commercial', '商业与企业', _BUSINESS_SCENARIOS))
CATALOGUE = {
    'integration': [
        ('title_style', '标题写法', '例如：每页标题写成一句完整结论'),
        ('points_per_page', '每页要点数', '例如：每页不超过三个要点'),
        ('required_pages', '必备页面', '例如：结尾必须有总结页和致谢页'),
        ('chapter_order', '章节顺序', '例如：先讲结论，再讲背景和依据'),
        ('notes_policy', '备注用法', '例如：较长的解释放进演讲备注'),
        ('text_density', '文字详略', '例如：页面只保留关键词和短句'),
        ('other', '其他', '其他关于内容怎样整合的习惯'),
    ],
    'layout': [
        ('palette', '配色', '例如：主色用深蓝，强调色用橙色'),
        ('fonts', '字体字号', '例如：中文微软雅黑，英文和数字 Arial'),
        ('footer_elements', '页脚元素', '例如：页脚保留页码和单位名称'),
        ('whitespace', '留白', '例如：页面四周保持充足留白'),
        ('image_text_ratio', '图文比例', '例如：图片占页面一半以上'),
        ('alignment', '对齐方式', '例如：正文统一左对齐'),
        ('other', '其他', '其他关于版面样式的习惯'),
    ],
    'expression': [
        ('share_data_form', '占比数据的呈现', '例如：占比用环形图，不用饼图'),
        ('process_form', '流程的呈现', '例如：流程用横向步骤图'),
        ('animation', '动画', '例如：不使用动画'),
        ('real_photos', '真实图片', '例如：配图尽量使用真实照片'),
        ('banned_chart_types', '不使用的图表', '例如：不使用三维图表'),
        ('icon_style', '图标风格', '例如：使用线性小图标'),
        ('other', '其他', '其他关于表达方式的习惯'),
    ],
}
SLOTS = {category: {slot for slot, _, _ in items} for category, items in CATALOGUE.items()}
# These slots hold several independent rules at once; a new habit there never replaces an older one.
MULTI_VALUED_SLOTS = {'other', 'required_pages', 'banned_chart_types'}
ORIGIN_ORDER = {'explicit': 0, 'onboarding': 1, 'inferred': 2}


def _load(text, fallback):
    """Stored JSON is written only by this module; stay tolerant when reading."""
    try:
        value = json.loads(text) if text else fallback
    except ValueError:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


def _stamp(value):
    return value.isoformat() + 'Z' if value else None


def _wire(value):
    return len(json.dumps(value)) + 1


class DesignHabits:
    def __init__(self, app):
        self.app, self.available = app, True

    # ---- helpers -----------------------------------------------------
    def _require(self):
        if not self.available:
            raise DesignMemoryError('设计习惯功能尚未启用。', 'HABITS_UNAVAILABLE', 503)

    @staticmethod
    def _public(row):
        return {'id': row.id, 'category': row.category, 'slot': row.slot, 'scope': _load(row.scope, {}),
                'value': _load(row.value, {}), 'rule': row.rule, 'origin': row.origin, 'status': row.status,
                'pinned': bool(row.pinned), 'paused': bool(row.paused), 'project_count': row.project_count,
                'helpful': row.helpful, 'harmful': row.harmful, 'evidence_count': len(_load(row.evidence, [])),
                'created_at': _stamp(row.created_at), 'updated_at': _stamp(row.updated_at), 'version': row.version}

    @staticmethod
    def _rule(value):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= MAX_RULE:
            raise DesignMemoryError('习惯描述需为 1 到 300 个字。')
        return value.strip()

    @staticmethod
    def _scope(value):
        if value is None:
            return {}
        if not isinstance(value, dict) or not set(value) <= set(SCOPE_KEYS):
            raise DesignMemoryError('适用范围无效。')
        clean = {}
        for key, item in value.items():
            if not isinstance(item, str) or not 1 <= len(item.strip()) <= MAX_SCOPE_VALUE:
                raise DesignMemoryError('适用范围无效。')
            clean[key] = item.strip()
        if 'purpose' in clean and clean['purpose'] not in {code for code, _, _ in PURPOSES}:
            raise DesignMemoryError('适用用途无效。')
        return clean

    @staticmethod
    def _value(value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise DesignMemoryError('习惯取值无效。')
        try:
            # Escaped length: what the value costs in a list answer.
            text = json.dumps(value, allow_nan=False)
        except (TypeError, ValueError):
            raise DesignMemoryError('习惯取值无效。') from None
        if len(text) > MAX_VALUE_JSON:
            raise DesignMemoryError('习惯取值过长。')
        return value

    @staticmethod
    def _flag(value):
        if type(value) is not bool:
            raise DesignMemoryError('开关取值无效。')
        return value

    def _owned(self, owner_id, habit_id):
        row = None
        if isinstance(habit_id, str) and len(habit_id) <= 36:
            row = DesignHabit.query.filter_by(id=habit_id, owner_id=owner_id, layer='personal').first()
        if row is None:
            raise DesignMemoryError('未找到这条设计习惯。', 'NOT_FOUND', 404)
        return row

    def _revise(self, row, action, before, after, actor='user', reason=None):
        db.session.add(DesignHabitRevision(habit_id=row.id, owner_id=row.owner_id, actor=actor, action=action,
            before=dumps(before) if before is not None else None, after=dumps(after) if after is not None else None,
            reason=reason))

    def _supersede(self, row, now):
        """At most one active habit per owner + category + slot + scope, except in multi-valued slots."""
        if row.slot in MULTI_VALUED_SLOTS:
            return
        others = DesignHabit.query.filter(DesignHabit.owner_id == row.owner_id, DesignHabit.layer == 'personal',
            DesignHabit.category == row.category, DesignHabit.slot == row.slot, DesignHabit.scope == row.scope,
            DesignHabit.status == 'active', DesignHabit.id != row.id).all()
        for old in others:
            before = self._public(old)
            old.valid_to, old.status, old.superseded_by = now, 'dormant', row.id
            old.updated_at, old.version = now, old.version + 1
            self._revise(old, 'supersede', before, self._public(old), actor='system', reason='superseded_by:' + row.id)

    def _preference_paused(self, owner_id):
        # Callers may hold unflushed work of their own; a read must not flush it.
        with db.session.no_autoflush:
            row = db.session.get(DesignHabitPreference, owner_id)
        return bool(row and row.paused)

    def _live_limit(self, owner_id):
        mine = DesignHabit.query.filter_by(owner_id=owner_id, layer='personal')
        if mine.filter(DesignHabit.status.in_(['active', 'candidate'])).count() >= MAX_LIVE_HABITS:
            raise DesignMemoryError('设计习惯数量已达上限，请先删除不再需要的习惯。', 'HABIT_LIMIT', 409)
        return mine

    # ---- owner-facing API ----------------------------------------------
    def catalogue(self):
        return {category: [{'slot': slot, 'label': label, 'hint': hint} for slot, label, hint in items]
                for category, items in CATALOGUE.items()}

    def purposes(self):
        return [{'value': code, 'label': label} for code, label, _ in PURPOSES]

    def context_for(self, brief):
        """The retrieval context a project brief provides. Only the purpose is known per deck so far."""
        scenario = _tag(brief.get('scenario')) if isinstance(brief, dict) else ''
        return {'purpose': code for code, _, tags in PURPOSES if scenario and scenario in tags}

    def paused(self, owner_id):
        """True while the owner has paused design habits: nothing is applied and nothing is kept for learning."""
        return bool(self.available and owner_id and self._preference_paused(owner_id))

    def list(self, owner_id):
        self._require()
        rows = DesignHabit.query.filter(DesignHabit.owner_id == owner_id, DesignHabit.layer == 'personal',
            DesignHabit.status != 'revoked').order_by(DesignHabit.created_at.desc(), DesignHabit.id.desc()).all()
        views = [(row.status == 'dormant', self._public(row)) for row in rows]
        # Live habits always fit (their number and every field are capped); shelved ones use the room
        # that is left, newest first, so the answer never outgrows the bridge.
        room = MAX_LIST_BYTES - sum(_wire(view) for shelved, view in views if not shelved)
        items, hidden = [], 0
        for shelved, view in views:
            if shelved:
                room -= _wire(view)
                if room < 0:
                    hidden += 1
                    continue
            items.append(view)
        return {'available': True, 'paused': self._preference_paused(owner_id), 'catalogue': self.catalogue(),
                'purposes': self.purposes(), 'items': items, **({'truncated': True} if hidden else {})}

    def create(self, owner_id, payload):
        self._require()
        if not isinstance(payload, dict) or not {'category', 'slot', 'rule'} <= set(payload) \
                or not set(payload) <= {'category', 'slot', 'rule', 'scope', 'value', 'pinned'}:
            raise DesignMemoryError('请提供习惯的类别、项目和描述。')
        category, slot = payload['category'], payload['slot']
        if not isinstance(category, str) or category not in SLOTS or not isinstance(slot, str) or slot not in SLOTS[category]:
            raise DesignMemoryError('习惯的类别或项目无效。')
        rule, scope, value = self._rule(payload['rule']), self._scope(payload.get('scope')), self._value(payload.get('value'))
        pinned = self._flag(payload['pinned']) if 'pinned' in payload else False
        if self._live_limit(owner_id).count() >= MAX_STORED_HABITS:
            raise DesignMemoryError('设计习惯数量已达上限，请先删除不再需要的习惯。', 'HABIT_LIMIT', 409)
        now = datetime.utcnow()
        row = DesignHabit(layer='personal', owner_id=owner_id, category=category, slot=slot, scope=dumps(scope),
            value=dumps(value), rule=rule, origin='explicit', evidence='[]', project_count=0, helpful=0, harmful=0,
            status='active', pinned=pinned, paused=False, valid_from=now, last_confirmed_at=now,
            created_at=now, updated_at=now, version=1)
        try:
            db.session.add(row)
            db.session.flush()
            self._supersede(row, now)
            self._revise(row, 'create', None, self._public(row))
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return self._public(row)

    def update(self, owner_id, habit_id, payload):
        self._require()
        allowed = {'rule', 'value', 'scope', 'pinned', 'paused', 'status', 'version'}
        if not isinstance(payload, dict) or 'version' not in payload or not set(payload) <= allowed or len(payload) < 2:
            raise DesignMemoryError('请提交当前版本与要修改的内容。')
        if type(payload['version']) is not int:
            raise DesignMemoryError('版本必须是整数。')
        row = self._owned(owner_id, habit_id)
        if row.status == 'revoked':
            raise DesignMemoryError('未找到这条设计习惯。', 'NOT_FOUND', 404)
        if row.version != payload['version']:
            raise DesignMemoryError('这条习惯已更新，请刷新。', 'REVISION_CONFLICT', 409)
        now, before = datetime.utcnow(), self._public(row)
        values = {'updated_at': now, 'version': row.version + 1}
        if 'rule' in payload:
            values['rule'] = self._rule(payload['rule'])
        if 'value' in payload:
            values['value'] = dumps(self._value(payload['value']))
        if 'scope' in payload:
            values['scope'] = dumps(self._scope(payload['scope']))
        if 'pinned' in payload:
            values['pinned'] = self._flag(payload['pinned'])
        if 'paused' in payload:
            values['paused'] = self._flag(payload['paused'])
        action = 'update'
        if 'status' in payload:
            status = payload['status']
            if status == 'revoked':
                values.update(status='revoked', valid_to=now)
                action = 'revoke'
            elif status == 'active' and row.status == 'dormant':
                # A shelved habit comes back into use; in a single-valued slot it then replaces the current one.
                self._live_limit(owner_id)
                values.update(status='active', valid_to=None, superseded_by=None, last_confirmed_at=now)
                action = 'reactivate'
            elif status == 'active':
                values.update(status='active', last_confirmed_at=now)
                action = 'confirm' if row.status == 'candidate' else action
            else:
                raise DesignMemoryError('状态只能改为使用中或撤销。')
        try:
            # Compare-and-set on version: a concurrent writer makes this a conflict, not a lost update.
            result = db.session.execute(update(DesignHabit).where(DesignHabit.id == row.id,
                DesignHabit.owner_id == owner_id, DesignHabit.version == payload['version']).values(**values))
            if result.rowcount != 1:
                raise DesignMemoryError('这条习惯已更新，请刷新。', 'REVISION_CONFLICT', 409)
            db.session.refresh(row)
            if row.status == 'active' and ('scope' in values or action in {'confirm', 'reactivate'}):
                self._supersede(row, now)
            self._revise(row, action, before, self._public(row))
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return self._public(row)

    def delete(self, owner_id, habit_id):
        self._require()
        row = self._owned(owner_id, habit_id)
        try:
            DesignHabitRevision.query.filter_by(habit_id=row.id, owner_id=owner_id).delete(synchronize_session=False)
            db.session.delete(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return {'deleted': True}

    def set_paused(self, owner_id, paused):
        self._require()
        paused = self._flag(paused)
        row = db.session.get(DesignHabitPreference, owner_id)
        try:
            if row is None:
                db.session.add(DesignHabitPreference(owner_id=owner_id, paused=paused, updated_at=datetime.utcnow()))
            else:
                row.paused, row.updated_at = paused, datetime.utcnow()
            db.session.commit()
        except IntegrityError:
            # A concurrent first write created the row; apply this request on top of it.
            db.session.rollback()
            DesignHabitPreference.query.filter_by(owner_id=owner_id).update({'paused': paused, 'updated_at': datetime.utcnow()})
            db.session.commit()
        return {'paused': paused}

    def reset(self, owner_id):
        self._require()
        try:
            deleted = DesignHabit.query.filter_by(owner_id=owner_id, layer='personal').delete(synchronize_session=False)
            DesignHabitRevision.query.filter_by(owner_id=owner_id).delete(synchronize_session=False)
            DesignEvent.query.filter_by(owner_id=owner_id).delete(synchronize_session=False)
            DesignInjectionLog.query.filter_by(owner_id=owner_id).delete(synchronize_session=False)
            # The older keyword memory learned from the same feedback: its raw text and the personal
            # rules condensed from it go too, or "reset" would keep injecting what was learned.
            DesignFeedback.query.filter_by(owner_id=owner_id).delete(synchronize_session=False)
            DesignRuleSet.query.filter_by(owner_id=owner_id, scope='personal').delete(synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise
        return {'deleted': deleted}

    @staticmethod
    def _exported_habit(row):
        return DesignHabits._public(row) | {'valid_from': _stamp(row.valid_from), 'valid_to': _stamp(row.valid_to),
            'superseded_by': row.superseded_by, 'evidence': _load(row.evidence, []),
            'last_used_at': _stamp(row.last_used_at), 'last_confirmed_at': _stamp(row.last_confirmed_at)}

    @staticmethod
    def _exported_event(row):
        return {'id': row.id, 'project_id': row.project_id, 'page_number': row.page_number, 'kind': row.kind,
                'payload': _load(row.payload, {}), 'context': _load(row.context, {}), 'created_at': _stamp(row.created_at)}

    def export(self, owner_id, cursor=None):
        """Everything stored for the owner, newest first: habits, then events.

        One answer holds at most EXPORT_PAGE_BYTES. When more is left the answer carries 'next'; passing it
        back as cursor continues exactly after the last record already delivered.
        """
        self._require()
        stage, position = 'h', None
        if cursor is not None:
            match = EXPORT_CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
            try:
                stage, position = match[1], (datetime.strptime(match[2], '%Y%m%dT%H%M%S.%f'), match[3])
            except (TypeError, ValueError):
                raise DesignMemoryError('导出位置无效，请重新导出。') from None
        result, used = {'habits': [], 'events': []}, 0
        sections = [('h', 'habits', DesignHabit, self._exported_habit), ('e', 'events', DesignEvent, self._exported_event)]
        for kind, name, model, view in sections[stage == 'e':]:
            query = model.query.filter_by(owner_id=owner_id)
            if model is DesignHabit:
                query = query.filter_by(layer='personal')
            if kind == stage and position:
                query = query.filter(or_(model.created_at < position[0],
                    and_(model.created_at == position[0], model.id < position[1])))
            last = EXPORT_START
            for row in query.order_by(model.created_at.desc(), model.id.desc()).limit(EXPORT_PAGE_ROWS + 1).all():
                item = view(row)
                size = _wire(item)
                # A page always carries at least one record, so the cursor always advances.
                if used and (used + size > EXPORT_PAGE_BYTES or len(result[name]) >= EXPORT_PAGE_ROWS):
                    result['next'] = '.'.join((kind,) + last)
                    return result
                result[name].append(item)
                used, last = used + size, (row.created_at.strftime('%Y%m%dT%H%M%S.%f'), row.id)
        return result

    # ---- pipeline-facing API (silent when the schema is absent) ---------------
    def record_event(self, owner_id, project_id, kind, payload, *, context=None, page_number=None, request_key=None, commit=False):
        """Append one event. Validation happens before the session is touched.

        Reads here never flush the caller's pending rows: a database error must surface in the caller's
        own flush, where its transaction handling expects it.
        """
        if not self.available or not owner_id or not project_id or kind not in EVENT_KINDS:
            return None
        if self._preference_paused(owner_id):
            # Paused by the owner: nothing is kept for learning.
            return None
        if not isinstance(payload, dict) or not isinstance(context or {}, dict):
            return None
        if request_key is not None and (not isinstance(request_key, str) or not 1 <= len(request_key) <= 160):
            return None
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            frame = json.dumps(context or {}, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            return None
        if len(body) > MAX_EVENT_JSON:
            # Keep the record valid JSON instead of cutting it mid-token; escaping at most doubles the preview.
            body = json.dumps({'truncated': True, 'preview': body[:7000]}, ensure_ascii=False)
        if len(frame) > 4000:
            frame = '{}'
        if request_key is not None:
            if any(isinstance(item, DesignEvent) and item.request_key == request_key for item in db.session.new):
                return None
            with db.session.no_autoflush:
                if DesignEvent.query.filter_by(request_key=request_key).first():
                    return None
        # Page numbers can come from a client; anything outside a real deck would not fit the column.
        page_number = page_number if type(page_number) is int and 1 <= page_number <= MAX_PAGE_NUMBER else None
        record = DesignEvent(owner_id=owner_id, project_id=project_id, kind=kind, payload=body, context=frame,
            page_number=page_number, request_key=request_key)
        db.session.add(record)
        if commit:
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                return None
        return record

    def active_for(self, owner_id, context):
        if not self.available or not owner_id or self._preference_paused(owner_id):
            return []
        context = context if isinstance(context, dict) else {}
        rows = DesignHabit.query.filter(DesignHabit.owner_id == owner_id, DesignHabit.layer == 'personal',
            DesignHabit.status == 'active', DesignHabit.paused.is_(False), DesignHabit.valid_to.is_(None)).all()
        # A scoped habit applies only when every scoped key is present and equal.
        rows = [row for row in rows if all(context.get(key) == item for key, item in _load(row.scope, {}).items())]
        rows.sort(key=lambda row: (row.created_at, row.id), reverse=True)
        rows.sort(key=lambda row: (not row.pinned, ORIGIN_ORDER.get(row.origin, 9)))
        return [self._public(row) for row in rows[:MAX_ACTIVE_INJECTED]]

    def log_injection(self, owner_id, project_id, habit_ids, rule_ids, request_key=None):
        """Called inside the job-creation transaction: add only, never commit."""
        if not self.available or not owner_id or not project_id:
            return None
        habit_ids = [value for value in habit_ids or [] if isinstance(value, str) and len(value) <= 36][:50]
        rule_ids = [value for value in rule_ids or [] if isinstance(value, str) and len(value) <= 200][:50]
        if not habit_ids and not rule_ids:
            return None
        if request_key is not None and (not isinstance(request_key, str) or len(request_key) > 160):
            request_key = None
        now = datetime.utcnow()
        row = DesignInjectionLog(owner_id=owner_id, project_id=project_id, request_key=request_key,
            habit_ids=dumps(habit_ids), rule_ids=dumps(rule_ids), created_at=now)
        db.session.add(row)
        if habit_ids:
            # A set-based statement: a habit the owner deletes meanwhile is simply not matched, where an
            # ORM attribute write would make the caller's flush fail on the missing row.
            with db.session.no_autoflush:
                db.session.execute(update(DesignHabit).where(DesignHabit.owner_id == owner_id,
                    DesignHabit.id.in_(habit_ids)).values(last_used_at=now).execution_options(synchronize_session=False))
        return row


def _store():
    service = current_app.extensions.get('wuyu_design_habits') if has_app_context() else None
    return service if service is not None and service.available else None


@event.listens_for(Project, 'after_delete')
def _purge_project(mapper, connection, project):
    """What was captured from a project's slides and feedback is deleted with it, in the same transaction."""
    if _store():
        for model in (DesignEvent, DesignInjectionLog):
            connection.execute(delete(model.__table__).where(model.__table__.c.project_id == project.id))


@event.listens_for(User, 'after_delete')
def _purge_owner(mapper, connection, user):
    """A deleted account leaves no habits, history, events or settings behind."""
    if _store():
        for model in (DesignHabitRevision, DesignEvent, DesignInjectionLog, DesignHabitPreference):
            connection.execute(delete(model.__table__).where(model.__table__.c.owner_id == user.id))
        habits = DesignHabit.__table__
        connection.execute(delete(habits).where(habits.c.owner_id == user.id, habits.c.layer == 'personal'))


def init_design_habits(app):
    service = DesignHabits(app)
    app.extensions['wuyu_design_habits'] = service
    with app.app_context():
        models = (DesignEvent, DesignHabit, DesignHabitRevision, DesignInjectionLog, DesignHabitPreference)
        if db.engine.dialect.name == 'sqlite':
            for model in models:
                model.__table__.create(db.engine, checkfirst=True)
        elif not all(inspect(db.engine).has_table(model.__tablename__) for model in models):
            # An un-migrated database still boots; the feature reports itself unavailable.
            service.available = False
            app.logger.warning('DESIGN_HABITS_SCHEMA_MISSING')
    return service
