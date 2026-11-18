"""Per-user design habits: store, tenant isolation, retrieval and template gate."""
from contextvars import copy_context
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import types
import unittest
import uuid
from unittest.mock import patch

from flask import Flask, request
from sqlalchemy import delete, insert
from sqlalchemy.exc import IntegrityError, OperationalError
from models import db, Project, User
from models.wuyu_design_memory import DesignFeedback, DesignPolicy, DesignRuleSet
from services.wuyu_design_memory import (DEFAULTS, DesignMemoryError, current_design_context, dumps,
                                         init_design_memory)
from services.wuyu_visual_settings import apply_template_visual_settings, template_safe_design_memory
from services.wuyu_workflow_templates import TemplateConfiguration
from controllers.wuyu_design_memory_controller import wuyu_design_memory_bp


class HabitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///' + str(self.root / 'habits.sqlite'),
            SQLALCHEMY_TRACK_MODIFICATIONS=False, UPLOAD_FOLDER=str(self.root))
        db.init_app(self.app)
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        self.memory = init_design_memory(self.app)
        self.habits = self.app.extensions['wuyu_design_habits']
        from models import wuyu_design_habits as models
        self.models = models
        self.project = str(uuid.uuid4())
        db.session.add(Project(id=self.project, user_id='owner-a', idea_prompt='self-made fixture deck'))
        db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.context.pop(); self.temp.cleanup()

    def make(self, owner='owner-a', **changes):
        return self.habits.create(owner, {'category': 'expression', 'slot': 'share_data_form',
                                          'rule': '占比数据用环形图呈现'} | changes)

    def assertCode(self, code, status, call, *args, **kwargs):
        with self.assertRaises(DesignMemoryError) as caught:
            call(*args, **kwargs)
        self.assertEqual((caught.exception.code, caught.exception.status), (code, status))


class StoreTests(HabitFixture):
    def test_catalogue_has_three_categories_with_other(self):
        catalogue = self.habits.catalogue()
        self.assertEqual(set(catalogue), {'integration', 'layout', 'expression'})
        for items in catalogue.values():
            self.assertTrue(5 <= len(items) <= 8)
            self.assertIn('other', [item['slot'] for item in items])
            self.assertTrue(all(set(item) == {'slot', 'label', 'hint'} for item in items))

    def test_create_list_update_delete(self):
        item = self.make(scope={'purpose': 'research'}, value={'chart': 'donut'}, pinned=True)
        self.assertEqual(set(item), {'id', 'category', 'slot', 'scope', 'value', 'rule', 'origin', 'status', 'pinned',
            'paused', 'project_count', 'helpful', 'harmful', 'evidence_count', 'created_at', 'updated_at', 'version'})
        self.assertEqual((item['origin'], item['status'], item['pinned'], item['version']), ('explicit', 'active', True, 1))
        self.assertEqual((item['scope'], item['value']), ({'purpose': 'research'}, {'chart': 'donut'}))
        view = self.habits.list('owner-a')
        self.assertEqual((view['available'], view['paused'], [x['id'] for x in view['items']]), (True, False, [item['id']]))
        self.assertEqual(view['catalogue'], self.habits.catalogue())
        self.assertEqual(set(view), {'available', 'paused', 'catalogue', 'purposes', 'items'})
        # The purpose a habit can be limited to is a fixed code list, served with the list.
        self.assertEqual([entry['value'] for entry in view['purposes']], ['research', 'commercial'])
        self.assertTrue(all(set(entry) == {'value', 'label'} and entry['label'] for entry in view['purposes']))
        changed = self.habits.update('owner-a', item['id'], {'rule': '占比数据用条形图', 'pinned': False, 'version': 1})
        self.assertEqual((changed['rule'], changed['pinned'], changed['version']), ('占比数据用条形图', False, 2))
        revisions = self.models.DesignHabitRevision.query.filter_by(habit_id=item['id']).order_by(self.models.DesignHabitRevision.created_at).all()
        self.assertEqual([row.action for row in revisions], ['create', 'update'])
        self.assertEqual(json.loads(revisions[1].before)['rule'], '占比数据用环形图呈现')
        self.assertEqual(json.loads(revisions[1].after)['rule'], '占比数据用条形图')
        self.assertEqual(self.habits.delete('owner-a', item['id']), {'deleted': True})
        self.assertEqual(self.habits.list('owner-a')['items'], [])
        self.assertEqual(self.models.DesignHabitRevision.query.count(), 0)
        self.assertCode('NOT_FOUND', 404, self.habits.delete, 'owner-a', item['id'])

    def test_validation(self):
        bad = [None, [], {'category': 'expression', 'slot': 'share_data_form'},
               {'category': 'nope', 'slot': 'palette', 'rule': 'x'},
               {'category': 'layout', 'slot': 'share_data_form', 'rule': 'x'},
               {'category': 'layout', 'slot': 'palette', 'rule': '   '},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x' * 301},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'origin': 'inferred'},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'scope': {'owner': 'b'}},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'scope': {'purpose': 7}},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'scope': {'purpose': 'y' * 61}},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'scope': {'purpose': '商业汇报'}},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'scope': {'page_role': 'y' * 61}},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'value': {'k': '值' * 400}},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'value': ['red']},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'value': {'k': 'v' * 3000}},
               {'category': 'layout', 'slot': 'palette', 'rule': 'x', 'pinned': 1}]
        for payload in bad:
            self.assertCode('DESIGN_MEMORY_INVALID', 400, self.habits.create, 'owner-a', payload)
        self.assertEqual(self.models.DesignHabit.query.count(), 0)
        item = self.make()
        for payload in [None, {'version': 1}, {'rule': 'x'}, {'rule': 'x', 'version': '1'}, {'rule': '', 'version': 1},
                        {'status': 'dormant', 'version': 1}, {'status': 'candidate', 'version': 1},
                        {'scope': {'purpose': '答辩'}, 'version': 1},
                        {'origin': 'inferred', 'version': 1}, {'paused': 'yes', 'version': 1}]:
            self.assertCode('DESIGN_MEMORY_INVALID', 400, self.habits.update, 'owner-a', item['id'], payload)
        self.assertEqual(self.habits.list('owner-a')['items'][0]['version'], 1)

    def test_limit_of_live_habits(self):
        now = datetime.utcnow()
        for index in range(100):
            db.session.add(self.models.DesignHabit(layer='personal', owner_id='owner-a', category='layout', slot='other',
                scope=dumps({'purpose': 'p%d' % index}), rule='r', status='active', valid_from=now))
        db.session.commit()
        self.assertCode('HABIT_LIMIT', 409, self.make)
        self.assertEqual(self.make(owner='owner-b')['status'], 'active')

    def test_optimistic_lock(self):
        item = self.make()
        self.habits.update('owner-a', item['id'], {'pinned': True, 'version': 1})
        self.assertCode('REVISION_CONFLICT', 409, self.habits.update, 'owner-a', item['id'], {'rule': '过期写入', 'version': 1})
        current = self.habits.list('owner-a')['items'][0]
        self.assertEqual((current['rule'], current['version']), ('占比数据用环形图呈现', 2))

    def test_tenant_isolation(self):
        item = self.make()
        self.assertEqual(self.habits.list('owner-b')['items'], [])
        self.assertCode('NOT_FOUND', 404, self.habits.update, 'owner-b', item['id'], {'rule': '越权', 'version': 1})
        self.assertCode('NOT_FOUND', 404, self.habits.delete, 'owner-b', item['id'])
        self.assertEqual(self.habits.active_for('owner-b', {}), [])
        self.assertEqual(self.habits.export('owner-b'), {'habits': [], 'events': []})
        self.assertEqual(self.habits.reset('owner-b'), {'deleted': 0})
        self.assertEqual(self.memory.retrieve('owner-b', 'anything')['rules'], [])
        self.habits.log_injection('owner-b', self.project, [item['id']], [])
        db.session.commit()
        self.assertIsNone(db.session.get(self.models.DesignHabit, item['id']).last_used_at)
        mine = self.habits.list('owner-a')['items']
        self.assertEqual([(x['id'], x['rule'], x['version']) for x in mine], [(item['id'], '占比数据用环形图呈现', 1)])

    def test_supersession_on_same_slot_and_scope(self):
        first = self.make()
        scoped = self.make(scope={'purpose': 'research'}, rule='学术汇报时占比用表格')
        second = self.make(rule='占比数据用百分比条')
        rows = {row.id: row for row in self.models.DesignHabit.query.all()}
        old = rows[first['id']]
        self.assertEqual((old.status, old.superseded_by), ('dormant', second['id']))
        self.assertIsNotNone(old.valid_to)
        self.assertEqual((rows[scoped['id']].status, rows[second['id']].status), ('active', 'active'))
        self.assertEqual([x['id'] for x in self.habits.active_for('owner-a', {})], [second['id']])
        actions = [row.action for row in self.models.DesignHabitRevision.query.filter_by(habit_id=first['id']).all()]
        self.assertEqual(sorted(actions), ['create', 'supersede'])
        # Another owner's identical habit is never touched.
        other = self.make(owner='owner-b')
        self.make(owner='owner-a', rule='第三版')
        self.assertEqual(db.session.get(self.models.DesignHabit, other['id']).status, 'active')

    def test_multi_valued_slots_keep_every_habit(self):
        # Finding: a second, unrelated habit in "other" silently shelved the first one.
        for category, slot in [('layout', 'other'), ('integration', 'required_pages'), ('expression', 'banned_chart_types')]:
            first = self.make(category=category, slot=slot, rule='第一条')
            second = self.make(category=category, slot=slot, rule='第二条')
            rows = {row.id: row for row in self.models.DesignHabit.query.filter_by(slot=slot).all()}
            self.assertEqual([(rows[x['id']].status, rows[x['id']].valid_to, rows[x['id']].superseded_by) for x in (first, second)],
                             [('active', None, None), ('active', None, None)])
        self.assertEqual(len(self.habits.active_for('owner-a', {})), 6)
        self.assertEqual(self.models.DesignHabitRevision.query.filter_by(action='supersede').count(), 0)
        # Changing the scope of a multi-valued habit onto another one's scope shelves nothing either.
        scoped = self.make(category='layout', slot='other', rule='第三条', scope={'purpose': 'research'})
        self.habits.update('owner-a', scoped['id'], {'scope': {}, 'version': 1})
        self.assertEqual(len(self.habits.active_for('owner-a', {})), 7)

    def test_shelved_habit_can_be_brought_back(self):
        first = self.make(rule='占比用环形图')
        second = self.make(rule='占比用百分比条')
        shelved = next(x for x in self.habits.list('owner-a')['items'] if x['id'] == first['id'])
        self.assertEqual((shelved['status'], shelved['version']), ('dormant', 2))
        back = self.habits.update('owner-a', first['id'], {'status': 'active', 'version': 2})
        self.assertEqual((back['status'], back['version']), ('active', 3))
        rows = {row.id: row for row in self.models.DesignHabit.query.all()}
        self.assertEqual((rows[first['id']].valid_to, rows[first['id']].superseded_by), (None, None))
        # The slot is single-valued: the habit that had replaced it is shelved in turn.
        self.assertEqual((rows[second['id']].status, rows[second['id']].superseded_by), ('dormant', first['id']))
        self.assertEqual([x['id'] for x in self.habits.active_for('owner-a', {})], [first['id']])
        actions = [row.action for row in self.models.DesignHabitRevision.query.filter_by(habit_id=first['id']).all()]
        self.assertEqual(sorted(actions), ['create', 'reactivate', 'supersede'])
        # Bringing one back counts against the live limit like adding one.
        now = datetime.utcnow()
        for index in range(99):
            db.session.add(self.models.DesignHabit(layer='personal', owner_id='owner-a', category='layout', slot='other',
                scope='{}', rule='r%d' % index, status='active', valid_from=now))
        db.session.commit()
        self.assertCode('HABIT_LIMIT', 409, self.habits.update, 'owner-a', second['id'], {'status': 'active', 'version': 2})

    def test_confirm_candidate_and_revoke(self):
        now = datetime.utcnow()
        row = self.models.DesignHabit(layer='personal', owner_id='owner-a', category='layout', slot='palette',
            scope='{}', rule='主色用深蓝', origin='inferred', status='candidate', valid_from=now)
        db.session.add(row); db.session.commit()
        self.assertEqual(self.habits.active_for('owner-a', {}), [])
        confirmed = self.habits.update('owner-a', row.id, {'status': 'active', 'version': 1})
        self.assertEqual((confirmed['status'], confirmed['origin']), ('active', 'inferred'))
        self.assertIsNotNone(db.session.get(self.models.DesignHabit, row.id).last_confirmed_at)
        revoked = self.habits.update('owner-a', row.id, {'status': 'revoked', 'version': 2})
        self.assertEqual(revoked['status'], 'revoked')
        self.assertEqual(self.habits.list('owner-a')['items'], [])
        self.assertEqual(self.habits.active_for('owner-a', {}), [])
        self.assertCode('NOT_FOUND', 404, self.habits.update, 'owner-a', row.id, {'status': 'active', 'version': 3})

    def test_pause_item_and_global(self):
        first, second = self.make(), self.make(category='layout', slot='palette', rule='主色用深蓝')
        self.habits.update('owner-a', first['id'], {'paused': True, 'version': 1})
        self.assertEqual([x['id'] for x in self.habits.active_for('owner-a', {})], [second['id']])
        self.assertEqual(self.habits.set_paused('owner-a', True), {'paused': True})
        self.assertEqual(self.habits.active_for('owner-a', {}), [])
        self.assertEqual(self.memory.retrieve('owner-a', 'x')['rules'], [])
        view = self.habits.list('owner-a')
        self.assertEqual((view['paused'], len(view['items'])), (True, 2))
        self.assertEqual(self.habits.list('owner-b')['paused'], False)
        self.assertEqual((self.habits.paused('owner-a'), self.habits.paused('owner-b'), self.habits.paused(None)), (True, False, False))
        self.assertEqual(self.habits.set_paused('owner-a', False), {'paused': False})
        self.assertEqual([x['id'] for x in self.habits.active_for('owner-a', {})], [second['id']])
        self.assertCode('DESIGN_MEMORY_INVALID', 400, self.habits.set_paused, 'owner-a', 'true')

    def test_pause_stops_event_capture(self):
        # Finding: the page promises nothing is learned while paused, yet events were still stored.
        self.habits.set_paused('owner-a', True)
        self.assertIsNone(self.habits.record_event('owner-a', self.project, 'manual_edit', {'changes': [1]}, commit=True))
        self.assertIsNone(self.habits.record_event('owner-a', self.project, 'text_feedback', {'text': 'x'}, request_key='k1'))
        self.assertIsNotNone(self.habits.record_event('owner-b', self.project, 'explicit', {'text': 'b'}, commit=True))
        db.session.commit()
        self.assertEqual([row.owner_id for row in self.models.DesignEvent.query.all()], ['owner-b'])
        self.habits.set_paused('owner-a', False)
        self.assertIsNotNone(self.habits.record_event('owner-a', self.project, 'manual_edit', {'changes': [1]}, commit=True))

    def test_reset_removes_only_this_owner(self):
        self.make(); self.make(category='layout', slot='palette', rule='主色用深蓝')
        kept = self.make(owner='owner-b')
        self.habits.record_event('owner-a', self.project, 'explicit', {'text': 'a'}, commit=True)
        self.habits.record_event('owner-b', self.project, 'explicit', {'text': 'b'}, commit=True)
        self.habits.log_injection('owner-a', self.project, [], ['sparse_copy'])
        # What the older keyword memory learned for the owner is part of "everything".
        rules = dumps({'rules': [{'id': 'sparse_copy', 'rule': '精简'}]})
        for owner in ('owner-a', 'owner-b'):
            db.session.add(DesignFeedback(owner_id=owner, project_id=self.project, request_key='legacy-' + owner,
                action='revise_page', text='文字太多，内部内容请精简', signals=dumps(['sparse_copy']), supported=True))
            db.session.add(DesignRuleSet(scope='personal', owner_id=owner, status='active', body=rules))
        db.session.add(DesignRuleSet(scope='system', owner_id=None, status='active', body=rules))
        db.session.commit()
        self.assertEqual([rule['id'] for rule in self.memory.retrieve('owner-a', '{}')['rules'][2:]], ['sparse_copy'])
        self.assertEqual(self.habits.reset('owner-a'), {'deleted': 2})
        self.assertEqual(self.habits.export('owner-a'), {'habits': [], 'events': []})
        self.assertEqual(self.memory.retrieve('owner-a', '{}')['rules'], [])
        self.assertEqual([row.owner_id for row in DesignFeedback.query.all()], ['owner-b'])
        self.assertEqual(sorted((row.scope, row.owner_id or '') for row in DesignRuleSet.query.all()),
                         [('personal', 'owner-b'), ('system', '')])
        self.assertEqual(self.models.DesignInjectionLog.query.count(), 0)
        self.assertEqual(self.models.DesignHabitRevision.query.filter_by(owner_id='owner-a').count(), 0)
        exported = self.habits.export('owner-b')
        self.assertEqual([x['id'] for x in exported['habits']], [kept['id']])
        self.assertEqual([x['payload'] for x in exported['events']], [{'text': 'b'}])

    def test_export_is_paged_under_the_bridge_limit(self):
        # Finding: about a hundred manual-edit events made the export larger than the 1 MiB the platform
        # bridge accepts, and it then failed for good.
        base = datetime.utcnow() - timedelta(days=1)
        for index in range(6):
            db.session.add(self.models.DesignHabit(layer='personal', owner_id='owner-a', category='layout', slot='other',
                scope='{}', rule='习惯%d' % index, status='active', valid_from=base, created_at=base + timedelta(seconds=index // 2)))
        for index in range(130):
            # Ties on created_at are part of the case: the position must still be exact.
            db.session.add(self.models.DesignEvent(owner_id='owner-a', project_id=self.project, kind='manual_edit',
                payload=dumps({'n': index, 'text': '改' * 2400}), created_at=base + timedelta(seconds=index // 3)))
        db.session.add(self.models.DesignEvent(owner_id='owner-b', project_id=self.project, kind='explicit', payload='{}'))
        db.session.commit()
        pages, cursor = [], None
        while True:
            page = self.habits.export('owner-a', cursor)
            pages.append(page)
            self.assertEqual(set(page) - {'next'}, {'habits', 'events'})
            # Flask escapes non-ASCII: this is the size the bridge sees.
            self.assertLess(len(json.dumps(page)), 700000)
            if 'next' not in page:
                break
            self.assertNotEqual(page['next'], cursor)
            cursor = page['next']
        self.assertGreater(len(json.dumps([event for page in pages for event in page['events']])), 1024 * 1024)
        self.assertGreater(len(pages), 2)
        habits = [item['rule'] for page in pages for item in page['habits']]
        events = [item['payload']['n'] for page in pages for item in page['events']]
        self.assertEqual(sorted(habits), ['习惯%d' % index for index in range(6)])
        self.assertEqual(sorted(events), list(range(130)))  # nothing lost, nothing twice
        stamps = [item['created_at'] for page in pages for item in page['events']]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        # A row cap applies as well, and habits page the same way as events.
        with patch('services.wuyu_design_habits.EXPORT_PAGE_ROWS', 4):
            first = self.habits.export('owner-a')
            self.assertEqual((len(first['habits']), first['events'], first['next'][:2]), (4, [], 'h.'))
            second = self.habits.export('owner-a', first['next'])
            self.assertEqual((len(second['habits']), len(second['events']), second['next'][:2]), (2, 4, 'e.'))
            self.assertFalse({item['id'] for item in first['habits']} & {item['id'] for item in second['habits']})
        # A cursor is a position in the caller's own data, nothing else.
        self.assertEqual(self.habits.export('owner-c', cursor), {'habits': [], 'events': []})
        for bad in ['', 'x', 'e.2026', 7, cursor + "' OR 1=1", 'e.20261301T000000.000000.' + 'f' * 36]:
            self.assertCode('DESIGN_MEMORY_INVALID', 400, self.habits.export, 'owner-a', bad)

    def test_list_stays_under_the_bridge_limit(self):
        # Largest habits the API accepts: the live ones always fit, the oldest shelved ones give way.
        now = datetime.utcnow()
        big = dict(scope=dumps({'page_role': '😀' * 60, 'content_type': '😀' * 60}), rule='😀' * 300, value=dumps({'k': 'v' * 1900}))
        self.habits._value(json.loads(big['value']))
        self.habits._scope(json.loads(big['scope']))
        for index in range(100):
            db.session.add(self.models.DesignHabit(layer='personal', owner_id='owner-a', category='layout', slot='other',
                status='active', valid_from=now, created_at=now, **big))
        for index in range(40):
            db.session.add(self.models.DesignHabit(layer='personal', owner_id='owner-a', category='layout', slot='palette',
                status='dormant', valid_from=now, created_at=now - timedelta(minutes=index + 1), **big | {'value': dumps({'n': index})}))
        db.session.commit()
        view = self.habits.list('owner-a')
        self.assertLess(len(json.dumps(view)), 1024 * 1024 - 20000)
        self.assertTrue(view['truncated'])
        self.assertEqual(sum(item['status'] == 'active' for item in view['items']), 100)
        shelved = [item['value']['n'] for item in view['items'] if item['status'] == 'dormant']
        self.assertTrue(0 < len(shelved) < 40)
        self.assertEqual(shelved, list(range(len(shelved))))  # the newest shelved ones are the ones shown
        self.assertNotIn('truncated', self.habits.list('owner-b'))
        # Everything is still reachable through the paged export and removed by reset.
        self.assertEqual(self.habits.reset('owner-a'), {'deleted': 140})

    def test_deleting_a_project_or_an_account_takes_its_design_data_along(self):
        # Finding: events with slide and feedback text outlived the project and the account.
        other = str(uuid.uuid4())
        db.session.add(Project(id=other, user_id='owner-a', idea_prompt='second fixture deck'))
        user = User(id='owner-a', username='fixture-owner', password_hash='x')
        db.session.add(user); db.session.add(User(id='owner-b', username='fixture-other', password_hash='x'))
        db.session.commit()
        mine, theirs = self.make(), self.make(owner='owner-b')
        self.habits.update('owner-a', mine['id'], {'pinned': True, 'version': 1})
        for owner, project in [('owner-a', self.project), ('owner-a', other), ('owner-b', other)]:
            self.habits.record_event(owner, project, 'manual_edit', {'text': 'slide text'}, commit=True)
            self.habits.log_injection(owner, project, [], ['sparse_copy'])
        self.habits.set_paused('owner-a', True); self.habits.set_paused('owner-b', True)
        count = lambda model, **where: model.query.filter_by(**where).count()
        # Same statements as DELETE /api/projects/<id>.
        db.session.delete(db.session.get(Project, self.project)); db.session.commit()
        self.assertEqual((count(self.models.DesignEvent, project_id=self.project), count(self.models.DesignInjectionLog, project_id=self.project)), (0, 0))
        self.assertEqual((count(self.models.DesignEvent, project_id=other), count(self.models.DesignInjectionLog, project_id=other)), (2, 2))
        self.assertEqual(count(self.models.DesignHabit, owner_id='owner-a'), 1)  # habits belong to the person, not the project
        # A rolled-back deletion deletes nothing.
        db.session.delete(db.session.get(Project, other)); db.session.flush(); db.session.rollback()
        self.assertEqual(count(self.models.DesignEvent, project_id=other), 2)
        # Same statements as the administrator's DELETE /api/admin/users/<id>.
        Project.query.filter_by(user_id='owner-a').update({'user_id': None})
        db.session.delete(user); db.session.commit()
        for model in (self.models.DesignHabit, self.models.DesignHabitRevision, self.models.DesignEvent,
                      self.models.DesignInjectionLog, self.models.DesignHabitPreference):
            self.assertEqual((model.__tablename__, count(model, owner_id='owner-a')), (model.__tablename__, 0))
            self.assertGreater(count(model, owner_id='owner-b'), 0, model.__tablename__)
        self.assertEqual([item['id'] for item in self.habits.list('owner-b')['items']], [theirs['id']])
        # An un-migrated database has no habit tables: deleting projects must keep working there.
        self.habits.available = False
        try:
            with patch('sqlalchemy.engine.Connection.execute', side_effect=AssertionError('no statement expected')):
                from services.wuyu_design_habits import _purge_project, _purge_owner
                _purge_project(None, db.session.connection(), types.SimpleNamespace(id=other))
                _purge_owner(None, db.session.connection(), types.SimpleNamespace(id='owner-b'))
        finally:
            self.habits.available = True

    def test_record_event_is_idempotent_and_bounded(self):
        first = self.habits.record_event('owner-a', self.project, 'manual_edit', {'field': 'title'}, page_number=3,
                                         context={'purpose': 'research'}, request_key='event:one')
        self.assertIsNotNone(first)
        # Idempotent inside one transaction as well, without flushing the caller's pending rows.
        self.assertIsNone(self.habits.record_event('owner-a', self.project, 'manual_edit', {'field': 'again'}, request_key='event:one'))
        self.assertIn(first, db.session.new)
        db.session.commit()
        self.assertIsNone(self.habits.record_event('owner-a', self.project, 'manual_edit', {'field': 'other'}, request_key='event:one', commit=True))
        self.assertIsNone(self.habits.record_event('owner-a', self.project, 'unknown_kind', {}, commit=True))
        self.assertIsNone(self.habits.record_event('owner-a', self.project, 'explicit', 'not a dict', commit=True))
        self.assertIsNone(self.habits.record_event('owner-a', self.project, 'explicit', {'bad': {1, 2}}, commit=True))
        self.habits.record_event('owner-a', self.project, 'explicit', {'text': '"引号\\' * 9000}, commit=True)
        rows = self.models.DesignEvent.query.order_by(self.models.DesignEvent.created_at).all()
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[0].page_number, json.loads(rows[0].payload), json.loads(rows[0].context)),
                         (3, {'field': 'title'}, {'purpose': 'research'}))
        self.assertLessEqual(len(rows[1].payload), 16000)
        self.assertTrue(json.loads(rows[1].payload)['truncated'])

    def test_record_event_keeps_only_real_page_numbers(self):
        # Finding: a client-supplied 2**70 reached the INTEGER column and failed the caller's flush.
        for number in (2 ** 70, 2147483648, 10001, 0, -3, True, '4', 2.0):
            self.habits.record_event('owner-a', self.project, 'text_feedback', {'n': str(number)}, page_number=number)
        self.habits.record_event('owner-a', self.project, 'text_feedback', {'n': 'ok'}, page_number=10000)
        db.session.commit()
        self.assertEqual(sorted(row.page_number or 0 for row in self.models.DesignEvent.query.all()), [0] * 8 + [10000])

    def test_active_for_scope_matching_and_ordering(self):
        general = self.make(rule='通用')
        report = self.make(scope={'purpose': 'research'}, rule='学术汇报专用')
        cover = self.make(scope={'purpose': 'research', 'page_role': 'cover'}, rule='学术汇报封面专用')
        ids = lambda context: {x['id'] for x in self.habits.active_for('owner-a', context)}
        self.assertEqual(ids({}), {general['id']})
        self.assertEqual(ids({'purpose': 'commercial'}), {general['id']})
        self.assertEqual(ids({'purpose': 'research'}), {general['id'], report['id']})
        self.assertEqual(ids({'purpose': 'research', 'page_role': 'cover'}), {general['id'], report['id'], cover['id']})
        self.assertEqual(ids(None), {general['id']})
        # Order: pinned, then explicit before inferred, then newest.
        base = datetime.utcnow() - timedelta(days=1)
        for index, (name, origin, pinned) in enumerate([('old_explicit', 'explicit', False), ('new_explicit', 'explicit', False),
                ('inferred_new', 'inferred', False), ('pinned_inferred', 'inferred', True)]):
            row = self.models.DesignHabit(layer='personal', owner_id='owner-c', category='layout', slot='other',
                scope='{}', rule=name, origin=origin, status='active', pinned=pinned,
                valid_from=base, created_at=base + timedelta(minutes=index))
            db.session.add(row)
        db.session.commit()
        self.assertEqual([x['rule'] for x in self.habits.active_for('owner-c', {})],
                         ['pinned_inferred', 'new_explicit', 'old_explicit', 'inferred_new'])
        for index in range(20):
            db.session.add(self.models.DesignHabit(layer='personal', owner_id='owner-d', category='layout', slot='other',
                scope='{}', rule='r%d' % index, status='active', valid_from=base))
        db.session.add(self.models.DesignHabit(layer='org', owner_id='owner-d', category='layout', slot='other',
            scope='{}', rule='org rule', status='active', pinned=True, valid_from=base))
        db.session.commit()
        capped = self.habits.active_for('owner-d', {})
        self.assertEqual(len(capped), 12)
        self.assertNotIn('org rule', [x['rule'] for x in capped])

    def test_unavailable_schema_is_quiet_for_pipeline_and_503_for_api(self):
        self.habits.available = False
        try:
            for call in [lambda: self.habits.list('owner-a'), lambda: self.make(), lambda: self.habits.export('owner-a'),
                         lambda: self.habits.reset('owner-a'), lambda: self.habits.set_paused('owner-a', True),
                         lambda: self.habits.update('owner-a', 'x', {'rule': 'x', 'version': 1}),
                         lambda: self.habits.delete('owner-a', 'x')]:
                self.assertCode('HABITS_UNAVAILABLE', 503, call)
            self.assertIsNone(self.habits.record_event('owner-a', self.project, 'explicit', {'a': 1}, commit=True))
            self.assertEqual(self.habits.active_for('owner-a', {}), [])
            self.assertFalse(self.habits.paused('owner-a'))
            self.assertIsNone(self.habits.log_injection('owner-a', self.project, ['x'], ['y']))
            self.assertEqual(self.memory.retrieve('owner-a', 'x')['rules'], [])
        finally:
            self.habits.available = True
        self.assertEqual(self.models.DesignEvent.query.count(), 0)


class IntegrationTests(HabitFixture):
    def test_retrieve_prepends_habits_without_consuming_limit(self):
        for key in ['sparse_copy', 'white_canvas']:
            db.session.add(DesignFeedback(owner_id='owner-a', project_id=self.project, request_key=key, action='revise_page',
                text={'sparse_copy': '文字太多', 'white_canvas': '白底'}[key], signals=dumps([key]), supported=True))
        db.session.add(DesignPolicy(id=1, revision=1, body=dumps(DEFAULTS | {'retrieval_limit': 1})))
        db.session.commit()
        general = self.make()
        scoped = self.make(category='integration', slot='title_style', scope={'purpose': 'research'}, rule='标题写成结论句')
        plain = self.memory.retrieve('owner-a', '文字')
        self.assertEqual(set(plain), {'version', 'rules', 'priority'})
        self.assertEqual(plain['rules'][0], {'id': 'habit:' + general['id'], 'rule': '占比数据用环形图呈现', 'rule_set_id': 'habit',
                                             'scope': 'personal', 'category': 'expression', 'slot': 'share_data_form'})
        self.assertEqual(len(plain['rules']), 2)
        self.assertNotIn('category', plain['rules'][1])
        # The brief's scenario is free text; it is resolved to the purpose code the same way the academic skill does.
        for brief in [{'scenario': 'research'}, {'scenario': '科研汇报', 'topic': 't'}, {'scenario': ' Scientific '}]:
            rules = self.memory.retrieve('owner-a', json.dumps(brief, ensure_ascii=False))['rules']
            self.assertEqual({rule['id'] for rule in rules[:2]}, {'habit:' + general['id'], 'habit:' + scoped['id']})
            self.assertEqual(len(rules), 3)
        for brief in [{'scenario': 'commercial'}, {'scenario': '别的场合'}, {'purpose': 'research'}, {'scenario': 7}]:
            self.assertEqual(len(self.memory.retrieve('owner-a', json.dumps(brief, ensure_ascii=False))['rules']), 2)
        self.assertEqual(len(self.memory.retrieve('owner-a', '[1, 2]')['rules']), 2)
        row = db.session.get(DesignPolicy, 1)
        row.body = dumps(DEFAULTS | {'personal_enabled': False}); db.session.commit()
        self.assertEqual(self.memory.retrieve('owner-a', '文字')['rules'], [])

    def test_record_captures_text_feedback_event_inside_caller_transaction(self):
        body = {'workflow': {'project_id': self.project}, 'slide_specs': [{'number': 1}], 'history': [],
                'brief': {'scenario': 'research', 'subscenario': 'academic_report'}}
        payload = {'number': 2, 'feedback': '这一页请换成我们公司的口吻'}
        self.memory.record(body, 'revise_page', payload, 'request-1')
        self.memory.record(body, 'revise_page', payload, 'request-1')
        self.assertEqual(self.models.DesignEvent.query.count(), 1)
        db.session.rollback()  # The caller owns the transaction: nothing was committed.
        self.assertEqual((self.models.DesignEvent.query.count(), DesignFeedback.query.count()), (0, 0))
        self.memory.record(body, 'revise_page', payload, 'request-1'); db.session.commit()
        event = self.models.DesignEvent.query.one()
        feedback = DesignFeedback.query.one()
        self.assertEqual(json.loads(feedback.signals), [])  # No keyword matched, the event is still kept.
        self.assertEqual((event.owner_id, event.project_id, event.kind, event.page_number), ('owner-a', self.project, 'text_feedback', 2))
        self.assertEqual(json.loads(event.payload), {'action': 'revise_page', 'text': payload['feedback']})
        self.assertEqual(event.request_key, 'event:' + feedback.request_key)
        self.assertEqual(json.loads(event.context), {'purpose': 'research'})
        # Existing early returns still apply.
        self.memory.record(body, 'export', payload, 'request-2')
        self.memory.record(body, 'revise_page', {'feedback': '  '}, 'request-3')
        self.memory.record({'workflow': {'project_id': self.project}, 'history': []}, 'update_brief', {'requirements': '首次填写'}, 'request-4')
        self.memory.record(body, 'revise_page', {'number': 'x', 'feedback': '页码不是整数'}, 'request-5')
        # Only a page revision names a page; an oversized client number must not break the caller's flush.
        self.memory.record(body, 'update_outline', {'number': 2, 'feedback': '大纲反馈带了无关的 number'}, 'request-7')
        self.memory.record(body, 'revise_page', {'number': 2 ** 70, 'feedback': '页码超出范围'}, 'request-8')
        db.session.add(DesignPolicy(id=1, revision=1, body=dumps(DEFAULTS | {'capture_enabled': False})))
        self.memory.record(body, 'revise_page', {'feedback': '关闭采集后不记录'}, 'request-6')
        db.session.commit()
        events = self.models.DesignEvent.query.order_by(self.models.DesignEvent.created_at).all()
        self.assertEqual([row.page_number for row in events], [2, None, None, None])

    def test_record_keeps_nothing_while_the_owner_paused(self):
        body = {'workflow': {'project_id': self.project}, 'slide_specs': [{'number': 1}], 'history': []}
        self.habits.set_paused('owner-a', True)
        self.memory.record(body, 'revise_page', {'number': 1, 'feedback': '文字太多'}, 'request-1'); db.session.commit()
        self.assertEqual((self.models.DesignEvent.query.count(), DesignFeedback.query.count()), (0, 0))
        self.habits.set_paused('owner-a', False)
        self.memory.record(body, 'revise_page', {'number': 1, 'feedback': '文字太多'}, 'request-1'); db.session.commit()
        self.assertEqual((self.models.DesignEvent.query.count(), DesignFeedback.query.count()), (1, 1))

    def test_pause_also_holds_back_legacy_personal_rules(self):
        rules = dumps({'rules': [{'id': 'white_canvas', 'rule': '白底'}]})
        db.session.add(DesignFeedback(owner_id='owner-a', project_id=self.project, request_key='k', action='revise_page',
            text='文字太多', signals=dumps(['sparse_copy']), supported=True))
        db.session.add(DesignRuleSet(scope='personal', owner_id='owner-a', status='active', body=rules))
        db.session.add(DesignRuleSet(scope='system', owner_id=None, status='active', body=dumps({'rules': [{'id': 'hd_assets', 'rule': '高清'}]})))
        db.session.add(DesignPolicy(id=1, revision=1, body=dumps(DEFAULTS | {'system_enabled': True})))
        db.session.commit()
        ids = lambda owner: sorted(rule['id'] for rule in self.memory.retrieve(owner, '{}')['rules'])
        self.assertEqual(ids('owner-a'), ['hd_assets', 'sparse_copy', 'white_canvas'])
        self.habits.set_paused('owner-a', True)
        self.assertEqual(ids('owner-a'), ['hd_assets'])  # Approved system rules are not personal habits.
        # The periodic personal compile learns nothing for a paused owner either.
        with patch.object(self.memory, 'compile') as compile_rules, patch.object(self.memory, 'reconcile_interrupted'):
            db.session.get(DesignPolicy, 1).body = dumps(DEFAULTS | {'personal_min_feedback': 2})
            db.session.add(DesignFeedback(owner_id='owner-a', project_id=self.project, request_key='k2', action='revise_page',
                text='白底', signals=dumps(['white_canvas']), supported=True))
            db.session.commit()
            self.memory.tick()
            compile_rules.assert_not_called()
            self.habits.set_paused('owner-a', False)
            self.memory.tick()
            compile_rules.assert_called_once()

    def test_concurrent_duplicate_surfaces_as_integrity_error_to_the_caller(self):
        # Finding: the event dedupe query flushed the caller's pending feedback row inside a try/except; the
        # swallowed IntegrityError left a rolled-back session and the workflow's replay path never ran.
        import services.wuyu_design_memory as module
        body = {'workflow': {'project_id': self.project}, 'slide_specs': [{'number': 1}], 'history': []}
        original = module.signals

        def twin_commits_first(text):
            # Another worker stores the same request between this request's duplicate check and its flush.
            key = __import__('hashlib').sha256((self.project + ':request-1').encode()).hexdigest()
            with db.engine.connect() as connection:
                connection.execute(insert(DesignFeedback.__table__).values(id=str(uuid.uuid4()), owner_id='owner-a',
                    project_id=self.project, request_key=key, action='revise_page', text=text, signals='[]',
                    supported=False, created_at=datetime.utcnow()))
                connection.commit()
            return original(text)
        with patch.object(module, 'signals', twin_commits_first), self.assertNoLogs(level='WARNING'):
            self.memory.record(body, 'revise_page', {'number': 1, 'feedback': '文字太多'}, 'request-1')
        # The duplicate is reported by the caller's own flush, as it was before the habit store existed.
        with self.assertRaises(IntegrityError):
            db.session.flush()
        db.session.rollback()
        self.assertEqual((DesignFeedback.query.count(), self.models.DesignEvent.query.count()), (1, 0))

    def test_record_never_swallows_a_database_error(self):
        body = {'workflow': {'project_id': self.project}, 'slide_specs': [{'number': 1}], 'history': []}
        with patch.object(self.habits, 'record_event', side_effect=OperationalError('s', {}, Exception('down'))):
            with self.assertRaises(OperationalError):
                self.memory.record(body, 'revise_page', {'feedback': '文字太多'}, 'request-9')
        db.session.rollback()

    def test_record_survives_a_failing_habit_store(self):
        body = {'workflow': {'project_id': self.project}, 'slide_specs': [{'number': 1}], 'history': []}
        with patch.object(self.habits, 'record_event', side_effect=RuntimeError('boom')):
            self.memory.record(body, 'revise_page', {'feedback': '文字太多'}, 'request-9')
        db.session.commit()
        self.assertEqual(DesignFeedback.query.count(), 1)

    def test_log_injection_adds_without_commit_and_marks_usage(self):
        item = self.make()
        self.habits.log_injection('owner-a', self.project, [item['id'], 'missing'], ['sparse_copy'], request_key='job-1')
        db.session.rollback()
        self.assertEqual(self.models.DesignInjectionLog.query.count(), 0)
        self.assertIsNone(db.session.get(self.models.DesignHabit, item['id']).last_used_at)
        self.habits.log_injection('owner-a', self.project, [item['id'], 'missing'], ['sparse_copy'], request_key='job-1')
        db.session.commit()
        row = self.models.DesignInjectionLog.query.one()
        self.assertEqual((row.owner_id, row.project_id, row.request_key), ('owner-a', self.project, 'job-1'))
        self.assertEqual((json.loads(row.habit_ids), json.loads(row.rule_ids)), ([item['id'], 'missing'], ['sparse_copy']))
        self.assertIsNotNone(db.session.get(self.models.DesignHabit, item['id']).last_used_at)
        self.assertIsNone(self.habits.log_injection('owner-a', self.project, [], []))

    def test_log_injection_tolerates_a_habit_deleted_meanwhile(self):
        # Finding: usage was marked through ORM attribute writes, so a habit the owner deleted before the
        # job's flush made that flush fail with StaleDataError.
        kept, gone = self.make(), self.make(category='layout', slot='palette', rule='主色用深蓝')
        loaded = db.session.get(self.models.DesignHabit, gone['id'])  # as retrieval leaves it in the session
        self.assertEqual(loaded.rule, '主色用深蓝')
        with db.engine.connect() as connection:
            connection.execute(delete(self.models.DesignHabit.__table__).where(self.models.DesignHabit.id == gone['id']))
            connection.commit()
        self.habits.log_injection('owner-a', self.project, [kept['id'], gone['id']], [])
        # No habit row is left dirty for the caller's flush: usage is marked by one set-based statement.
        self.assertEqual([type(row).__name__ for row in db.session.dirty], [])
        db.session.flush(); db.session.commit()
        self.assertIsNotNone(db.session.get(self.models.DesignHabit, kept['id']).last_used_at)
        self.assertEqual(self.models.DesignInjectionLog.query.count(), 1)

    def test_job_creation_logs_injected_ids(self):
        # The job component reads ids from the retrieved rules; exercise that exact block from source.
        import ast
        source = (Path(__file__).parent / 'services' / 'wuyu_workflow_jobs.py').read_text(encoding='utf-8')
        self.assertIn("habits.log_injection(owner_id, project_id,", source)
        tree = ast.parse(source)
        block = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                     and ast.unparse(node.test) == "memory and '__design_context' not in payload")
        item = self.make()
        db.session.add(DesignFeedback(owner_id='owner-a', project_id=self.project, request_key='k', action='revise_page',
            text='文字太多', signals=dumps(['sparse_copy']), supported=True)); db.session.commit()
        ports = types.SimpleNamespace(app=lambda: self.app, hook__json=lambda value: json.dumps(value, ensure_ascii=False))
        from sqlalchemy.exc import SQLAlchemyError
        scope = {'memory': self.memory, 'payload': {}, 'db': db, 'Project': Project, 'project_id': self.project,
                 'SQLAlchemyError': SQLAlchemyError,
                 'body': {'brief': {'scenario': 'research'}}, 'self': types.SimpleNamespace(ports=ports)}
        exec(compile(ast.Module(body=[block], type_ignores=[]), 'job-design-context', 'exec'), scope)
        db.session.commit()
        self.assertEqual([rule['id'] for rule in scope['payload']['__design_context']['rules']], ['habit:' + item['id'], 'sparse_copy'])
        row = self.models.DesignInjectionLog.query.one()
        self.assertEqual((json.loads(row.habit_ids), json.loads(row.rule_ids)), ([item['id']], ['sparse_copy']))
        with patch.object(self.habits, 'log_injection', side_effect=RuntimeError('boom')):
            scope['payload'] = {}
            exec(compile(ast.Module(body=[block], type_ignores=[]), 'job-design-context', 'exec'), scope)
        self.assertIn('__design_context', scope['payload'])
        # A database error is not swallowed: the job transaction is unusable and its caller must see why.
        with patch.object(self.habits, 'log_injection', side_effect=OperationalError('s', {}, Exception('down'))):
            scope['payload'] = {}
            with self.assertRaises(OperationalError):
                exec(compile(ast.Module(body=[block], type_ignores=[]), 'job-design-context', 'exec'), scope)


class TemplateGateTests(unittest.TestCase):
    CONTEXT = {'version': 1, 'priority': 'p', 'rules': [
        {'id': 'habit:1', 'rule': '标题写成结论句', 'rule_set_id': 'habit', 'scope': 'personal', 'category': 'integration', 'slot': 'title_style'},
        {'id': 'habit:2', 'rule': '主色用深蓝', 'rule_set_id': 'habit', 'scope': 'personal', 'category': 'layout', 'slot': 'palette'},
        {'id': 'habit:3', 'rule': '占比用环形图', 'rule_set_id': 'habit', 'scope': 'personal', 'category': 'expression', 'slot': 'share_data_form'},
        {'id': 'white_canvas', 'rule': '白底', 'rule_set_id': 'feedback:x', 'scope': 'personal'}]}
    LEGACY = {'version': 1, 'priority': 'p', 'rules': [{'id': 'white_canvas', 'rule': '白底', 'rule_set_id': 'x', 'scope': 'personal'}]}

    def test_filter_keeps_only_integration_and_expression(self):
        kept = template_safe_design_memory(self.CONTEXT)
        self.assertEqual((kept['version'], kept['priority'], [rule['id'] for rule in kept['rules']]), (1, 'p', ['habit:1', 'habit:3']))
        for value in [self.LEGACY, {}, None, 'text', {'rules': 'x'}, {'version': 1, 'rules': []}]:
            self.assertEqual(template_safe_design_memory(value), {})

    def test_prompt_preferences_in_template_mode(self):
        prompt = lambda context: json.dumps({'task': 't', 'design_preferences': context}, ensure_ascii=False)
        template, brief = {'style_mode': 'template'}, {'style_mode': 'brief'}
        value = json.loads(apply_template_visual_settings(prompt(self.CONTEXT), template))
        self.assertEqual([rule['id'] for rule in value['design_preferences']['rules']], ['habit:1', 'habit:3'])
        self.assertEqual((value['design_preferences']['version'], value['design_preferences']['priority']), (1, 'p'))
        self.assertEqual(json.loads(apply_template_visual_settings(prompt(self.LEGACY), template))['design_preferences'], {})
        self.assertEqual(json.loads(apply_template_visual_settings(json.dumps({'task': 't'}), template))['design_preferences'], {})
        self.assertEqual(apply_template_visual_settings(prompt(self.CONTEXT), brief), prompt(self.CONTEXT))

    def recipe(self, context, mode):
        ports = types.SimpleNamespace(hook_effective_visual_settings=lambda body, choice: {'style_mode': mode},
            hook_current_template_choice=lambda body: None, app=lambda: types.SimpleNamespace(config={'UPLOAD_FOLDER': tempfile.gettempdir()}))
        host = types.SimpleNamespace(ports=ports)
        body = {'reference_recipe': {}, 'brief': {}, 'workflow': {'project_id': 'p'}, 'materials': []}
        def run():
            current_design_context.set(context)
            try:
                return TemplateConfiguration._pipeline_recipe(host, body)
            except Exception:
                # Later recipe steps need a full workflow; the design decision is already made.
                return None
        with patch('services.wuyu_workflow_templates.deepcopy', side_effect=lambda value: self.seen.append(value) or json.loads(json.dumps(value))):
            copy_context().run(run)

    def test_pipeline_recipe_template_gate(self):
        def memory(context, mode):
            self.seen = []
            self.recipe(context, mode)
            return [value for value in self.seen[1:] if isinstance(value, dict) and 'rules' in value]
        self.assertEqual(memory(self.CONTEXT, 'brief'), [self.CONTEXT])
        kept = memory(self.CONTEXT, 'template')
        self.assertEqual([[rule['id'] for rule in value['rules']] for value in kept], [['habit:1', 'habit:3']])
        self.assertEqual(memory(self.LEGACY, 'template'), [])
        self.assertEqual(memory(self.LEGACY, 'brief'), [self.LEGACY])


class HttpTests(HabitFixture):
    def setUp(self):
        super().setUp()
        self.app.register_blueprint(wuyu_design_memory_bp)
        def load():
            name = request.headers.get('X-Test-User')
            if not name:
                return None, (json.dumps({'success': False}), 401)
            request.current_user = types.SimpleNamespace(id=name, role='user')
            return request.current_user, None
        self.patches = [patch('middleware.auth._load_current_user', side_effect=load), patch('middleware.auth.has_feature', return_value=True)]
        for item in self.patches:
            item.start()
        self.client = self.app.test_client()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        super().tearDown()

    def call(self, method, path, user='owner-a', **kwargs):
        response = getattr(self.client, method)('/api/design-memory' + path, headers={'X-Test-User': user} if user else {}, **kwargs)
        return response.status_code, response.get_json(silent=True)

    def test_routes(self):
        self.assertEqual(self.call('get', '/habits', user=None)[0], 401)
        status, body = self.call('post', '/habits', json={'category': 'layout', 'slot': 'palette', 'rule': '主色用深蓝'})
        self.assertEqual(status, 200)
        habit = body['data']
        self.assertEqual((habit['status'], habit['origin']), ('active', 'explicit'))
        status, body = self.call('get', '/habits')
        self.assertEqual((status, [x['id'] for x in body['data']['items']], body['data']['paused']), (200, [habit['id']], False))
        self.assertEqual(self.call('get', '/habits', user='owner-b')[1]['data']['items'], [])
        self.assertEqual(self.call('post', '/habits', json={'category': 'layout'})[0], 400)
        path = '/habits/' + habit['id']
        self.assertEqual(self.call('patch', path, user='owner-b', json={'rule': '越权', 'version': 1})[0], 404)
        self.assertEqual(self.call('delete', path, user='owner-b')[0], 404)
        status, body = self.call('patch', path, json={'rule': '主色用墨绿', 'version': 1})
        self.assertEqual((status, body['data']['rule'], body['data']['version']), (200, '主色用墨绿', 2))
        status, body = self.call('patch', path, json={'rule': '过期', 'version': 1})
        self.assertEqual((status, body['error']['code']), (409, 'REVISION_CONFLICT'))
        self.assertEqual(self.call('put', '/habits/settings', json={'paused': 'yes'})[0], 400)
        status, body = self.call('put', '/habits/settings', json={'paused': True})
        self.assertEqual((status, body['data']), (200, {'paused': True}))
        self.assertEqual(self.call('post', '/retrieve', json={'query': '配色'})[1]['data']['rules'], [])
        self.call('put', '/habits/settings', json={'paused': False})
        rules = self.call('post', '/retrieve', json={'query': '配色'})[1]['data']['rules']
        self.assertEqual([(rule['id'], rule['category']) for rule in rules], [('habit:' + habit['id'], 'layout')])
        status, body = self.call('get', '/habits/export')
        self.assertEqual((status, [x['id'] for x in body['data']['habits']], body['data']['events']), (200, [habit['id']], []))
        self.assertNotIn('next', body['data'])
        self.assertEqual(self.call('get', '/habits/export/nonsense')[0], 400)
        self.assertEqual(self.call('get', '/habits/export/nonsense', user=None)[0], 401)
        for index in range(90):
            self.habits.record_event('owner-a', self.project, 'manual_edit', {'n': index, 'text': '改' * 2400}, commit=True)
        seen, cursor, largest = [], '', 0
        for _ in range(20):
            response = self.client.get('/api/design-memory/habits/export' + cursor, headers={'X-Test-User': 'owner-a'})
            largest = max(largest, len(response.data))
            data = response.get_json()['data']
            seen += [event['payload']['n'] for event in data['events']]
            if 'next' not in data:
                break
            # The position is a plain path segment: the platform bridge forwards no query strings.
            self.assertRegex(data['next'], r'^[A-Za-z0-9.-]+$')
            self.assertNotIn('..', data['next'])
            cursor = '/' + data['next']
        self.assertEqual(sorted(seen), list(range(90)))
        self.assertGreater(len(json.dumps(seen)) + 90 * 2400 * 6, 1024 * 1024)  # more than one bridge answer in total
        self.assertLess(largest, 1024 * 1024)  # every single answer fits the platform bridge
        self.assertEqual(self.call('post', '/habits/reset', json={})[0], 400)
        self.assertEqual(self.call('post', '/habits/reset', json={'confirm': 'true'})[0], 400)
        self.assertEqual(self.call('post', '/habits/reset', user='owner-b', json={'confirm': True})[1]['data'], {'deleted': 0})
        self.assertEqual(len(self.call('get', '/habits')[1]['data']['items']), 1)
        status, body = self.call('delete', path)
        self.assertEqual((status, body['data']), (200, {'deleted': True}))
        self.assertEqual(self.call('post', '/habits/reset', json={'confirm': True})[1]['data'], {'deleted': 0})
        self.habits.available = False
        status, body = self.call('get', '/habits')
        self.assertEqual((status, body['error']['code']), (503, 'HABITS_UNAVAILABLE'))


if __name__ == '__main__':
    unittest.main()
