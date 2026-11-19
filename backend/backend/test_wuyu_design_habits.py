"""Per-user design habits: store, tenant isolation, retrieval and template gate."""
from contextvars import copy_context
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
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
                         lambda: self.habits.seed_onboarding('owner-a', [{'category': 'layout', 'slot': 'palette', 'rule': 'x'}]),
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


class OnboardingTests(HabitFixture):
    PICKS = [{'category': 'layout', 'slot': 'palette', 'rule': '主色用深蓝'},
             {'category': 'integration', 'slot': 'title_style', 'rule': '标题写成结论句', 'scope': {'purpose': 'research'},
              'value': {'form': 'conclusion'}},
             {'category': 'expression', 'slot': 'banned_chart_types', 'rule': '不使用三维图表'}]

    def rows(self, owner='owner-a', **where):
        return self.models.DesignHabit.query.filter_by(owner_id=owner, **where).all()

    def test_seed_creates_active_onboarding_habits(self):
        result = self.habits.seed_onboarding('owner-a', self.PICKS)
        self.assertEqual((set(result), result['skipped']), ({'created', 'skipped'}, []))
        self.assertEqual([(x['category'], x['slot'], x['rule'], x['scope'], x['value'], x['origin'], x['status'], x['pinned'], x['version'])
                          for x in result['created']],
                         [('layout', 'palette', '主色用深蓝', {}, {}, 'onboarding', 'active', False, 1),
                          ('integration', 'title_style', '标题写成结论句', {'purpose': 'research'}, {'form': 'conclusion'}, 'onboarding', 'active', False, 1),
                          ('expression', 'banned_chart_types', '不使用三维图表', {}, {}, 'onboarding', 'active', False, 1)])
        self.assertEqual(set(result['created'][0]), set(self.make(owner='owner-b')))
        self.assertEqual(sorted(x['id'] for x in self.habits.list('owner-a')['items']), sorted(x['id'] for x in result['created']))
        revisions = self.models.DesignHabitRevision.query.filter_by(owner_id='owner-a').all()
        self.assertEqual(sorted((row.habit_id, row.actor, row.action, row.before) for row in revisions),
                         sorted((x['id'], 'user', 'onboarding', None) for x in result['created']))
        self.assertEqual(json.loads(revisions[0].after)['origin'], 'onboarding')
        # The normal create route never takes an origin from the client.
        self.assertCode('DESIGN_MEMORY_INVALID', 400, self.habits.create, 'owner-a',
                        {'category': 'layout', 'slot': 'fonts', 'rule': 'x', 'origin': 'onboarding'})
        self.assertEqual(self.make(category='layout', slot='fonts')['origin'], 'explicit')

    def test_seed_is_idempotent(self):
        first = self.habits.seed_onboarding('owner-a', self.PICKS)
        again = self.habits.seed_onboarding('owner-a', self.PICKS)
        self.assertEqual(again['created'], [])
        self.assertEqual(again['skipped'], [{'category': pick['category'], 'slot': pick['slot'], 'reason': 'unchanged'} for pick in self.PICKS])
        self.assertEqual(sorted(row.id for row in self.rows()), sorted(x['id'] for x in first['created']))
        self.assertEqual({(row.status, row.version) for row in self.rows()}, {('active', 1)})
        self.assertEqual(self.models.DesignHabitRevision.query.count(), 3)
        # A repeated pick inside one submission counts once; surrounding spaces do not make a new habit.
        twice = self.habits.seed_onboarding('owner-b', [self.PICKS[0], dict(self.PICKS[0], rule=' 主色用深蓝 '), self.PICKS[2], self.PICKS[2]])
        self.assertEqual(([x['slot'] for x in twice['created']], twice['skipped']), (['palette', 'banned_chart_types'], []))
        # A multi-valued slot takes a further pick next to the earlier one.
        more = self.habits.seed_onboarding('owner-b', [{'category': 'expression', 'slot': 'banned_chart_types', 'rule': '不使用饼图'}])
        self.assertEqual(len(more['created']), 1)
        self.assertEqual(sorted(row.status for row in self.rows('owner-b', slot='banned_chart_types')), ['active', 'active'])

    def test_seed_never_replaces_what_the_owner_set_or_what_was_learned(self):
        mine = self.make(category='layout', slot='palette', rule='主色用墨绿')
        learned = self.models.DesignHabit(layer='personal', owner_id='owner-a', category='integration', slot='title_style',
            scope=dumps({'purpose': 'research'}), rule='标题用短语', origin='inferred', status='active', valid_from=datetime.utcnow())
        # A suggestion that is not confirmed yet and a shelved habit do not hold the slot.
        pending = self.models.DesignHabit(layer='personal', owner_id='owner-a', category='layout', slot='fonts',
            scope='{}', rule='中文用宋体', origin='inferred', status='candidate', valid_from=datetime.utcnow())
        db.session.add_all([learned, pending]); db.session.commit()
        result = self.habits.seed_onboarding('owner-a', self.PICKS + [
            {'category': 'layout', 'slot': 'fonts', 'rule': '中文用微软雅黑'},
            {'category': 'layout', 'slot': 'palette', 'rule': '商业汇报主色用红', 'scope': {'purpose': 'commercial'}}])
        self.assertEqual(result['skipped'], [{'category': 'layout', 'slot': 'palette', 'reason': 'explicit'},
                                             {'category': 'integration', 'slot': 'title_style', 'reason': 'inferred'}])
        self.assertEqual([(x['slot'], x['scope']) for x in result['created']],
                         [('banned_chart_types', {}), ('fonts', {}), ('palette', {'purpose': 'commercial'})])
        rows = {row.id: row for row in self.rows()}
        self.assertEqual([(rows[key].status, rows[key].rule, rows[key].version, rows[key].superseded_by) for key in (mine['id'], learned.id, pending.id)],
                         [('active', '主色用墨绿', 1, None), ('active', '标题用短语', 1, None), ('candidate', '中文用宋体', 1, None)])
        self.assertEqual(self.models.DesignHabitRevision.query.filter_by(action='supersede').count(), 0)
        # An explicit habit added later replaces the onboarding pick, as it replaces any other habit.
        later = self.make(category='layout', slot='fonts', rule='中文用黑体')
        fonts = {row.origin: (row.status, row.superseded_by) for row in self.rows(slot='fonts') if row.status != 'candidate'}
        self.assertEqual(fonts, {'explicit': ('active', None), 'onboarding': ('dormant', later['id'])})

    def test_seed_supersedes_an_earlier_different_pick(self):
        first = self.habits.seed_onboarding('owner-a', self.PICKS)['created']
        changed = [dict(self.PICKS[0], rule='主色用墨绿'), self.PICKS[1], dict(self.PICKS[2], rule='不使用饼图')]
        result = self.habits.seed_onboarding('owner-a', changed)
        self.assertEqual([x['rule'] for x in result['created']], ['主色用墨绿', '不使用饼图'])
        self.assertEqual(result['skipped'], [{'category': 'integration', 'slot': 'title_style', 'reason': 'unchanged'}])
        rows = {row.id: row for row in self.rows()}
        old = rows[first[0]['id']]
        self.assertEqual((old.status, old.superseded_by, old.version), ('dormant', result['created'][0]['id'], 2))
        self.assertIsNotNone(old.valid_to)
        # Multi-valued slots are exempt: both picks stay in use.
        self.assertEqual((rows[first[2]['id']].status, rows[first[2]['id']].superseded_by), ('active', None))
        revisions = self.models.DesignHabitRevision.query.filter_by(action='supersede').all()
        self.assertEqual([(row.habit_id, row.actor, row.reason) for row in revisions],
                         [(old.id, 'system', 'superseded_by:' + result['created'][0]['id'])])
        self.assertEqual(sorted(x['rule'] for x in self.habits.active_for('owner-a', {'purpose': 'research'})),
                         sorted(['主色用墨绿', '标题写成结论句', '不使用三维图表', '不使用饼图']))
        # The same rule with another value is a different pick too.
        valued = self.habits.seed_onboarding('owner-a', [dict(self.PICKS[1], value={'form': 'question'})])
        self.assertEqual(db.session.get(self.models.DesignHabit, first[1]['id']).superseded_by, valued['created'][0]['id'])

    def test_seed_keeps_a_pick_the_owner_has_changed(self):
        first = {x['slot']: x for x in self.habits.seed_onboarding('owner-a', self.PICKS + [
            {'category': 'layout', 'slot': 'fonts', 'rule': '中文用微软雅黑'},
            {'category': 'layout', 'slot': 'alignment', 'rule': '正文左对齐'},
            {'category': 'layout', 'slot': 'whitespace', 'rule': '四周多留白'}])['created']}
        # The owner rewords and pins one pick, only pins a second, only pauses a third; none changes its origin.
        mine = self.habits.update('owner-a', first['palette']['id'], {'rule': '主色用墨绿（我改的）', 'pinned': True, 'version': 1})
        self.habits.update('owner-a', first['fonts']['id'], {'pinned': True, 'version': 1})
        self.habits.update('owner-a', first['alignment']['id'], {'paused': True, 'version': 1})
        self.assertEqual((mine['origin'], mine['version']), ('onboarding', 2))
        again = self.habits.seed_onboarding('owner-a', [self.PICKS[0], {'category': 'layout', 'slot': 'fonts', 'rule': '中文用微软雅黑'},
            {'category': 'layout', 'slot': 'alignment', 'rule': '正文居中'}, {'category': 'layout', 'slot': 'whitespace', 'rule': '页面紧凑'}])
        self.assertEqual(again['skipped'], [{'category': 'layout', 'slot': 'palette', 'reason': 'edited'},
                                            {'category': 'layout', 'slot': 'fonts', 'reason': 'unchanged'},
                                            {'category': 'layout', 'slot': 'alignment', 'reason': 'edited'}])
        # Only the pick nobody touched is replaced.
        self.assertEqual([x['rule'] for x in again['created']], ['页面紧凑'])
        rows = {row.id: row for row in self.rows()}
        self.assertEqual([(rows[first[slot]['id']].status, rows[first[slot]['id']].rule, rows[first[slot]['id']].pinned,
                           rows[first[slot]['id']].paused, rows[first[slot]['id']].superseded_by) for slot in ('palette', 'fonts', 'alignment')],
                         [('active', '主色用墨绿（我改的）', True, False, None), ('active', '中文用微软雅黑', True, False, None),
                          ('active', '正文左对齐', False, True, None)])
        self.assertEqual((rows[first['whitespace']['id']].status, rows[first['whitespace']['id']].superseded_by),
                         ('dormant', again['created'][0]['id']))
        self.assertEqual([row.habit_id for row in self.models.DesignHabitRevision.query.filter_by(action='supersede').all()],
                         [first['whitespace']['id']])
        # A shelved pick the owner brings back is the owner's decision too: the newer pick does not return.
        self.habits.update('owner-a', first['whitespace']['id'], {'status': 'active', 'version': 2})
        back = self.habits.seed_onboarding('owner-a', [{'category': 'layout', 'slot': 'whitespace', 'rule': '页面紧凑'}])
        self.assertEqual((back['created'], back['skipped']), ([], [{'category': 'layout', 'slot': 'whitespace', 'reason': 'edited'}]))
        self.assertEqual(db.session.get(self.models.DesignHabit, first['whitespace']['id']).status, 'active')
        # Even if the check were passed, the shelving step itself leaves a changed pick alone.
        with patch.object(self.habits, '_seed_obstacle', return_value=None):
            forced = self.habits.seed_onboarding('owner-a', [self.PICKS[0]])['created'][0]
        self.assertEqual((db.session.get(self.models.DesignHabit, first['palette']['id']).status, forced['status']), ('active', 'active'))

    def test_seed_stores_again_what_the_owner_removed(self):
        # No record of a finished setup is kept: a removed pick comes back only when the caller submits it again.
        first = self.habits.seed_onboarding('owner-a', self.PICKS[:2])['created']
        self.habits.update('owner-a', first[0]['id'], {'status': 'revoked', 'version': 1})
        self.habits.delete('owner-a', first[1]['id'])
        again = self.habits.seed_onboarding('owner-a', self.PICKS[:2])
        self.assertEqual(([x['rule'] for x in again['created']], again['skipped']), (['主色用深蓝', '标题写成结论句'], []))
        self.assertEqual(db.session.get(self.models.DesignHabit, first[0]['id']).status, 'revoked')

    def test_seed_in_a_multi_valued_slot_skips_only_the_same_rule(self):
        self.make(category='layout', slot='other', rule='页脚放单位 logo')
        self.make(category='expression', slot='banned_chart_types', rule='不使用三维图表')
        db.session.add(self.models.DesignHabit(layer='personal', owner_id='owner-a', category='integration', slot='required_pages',
            scope='{}', rule='结尾有致谢页', origin='inferred', status='active', valid_from=datetime.utcnow()))
        db.session.commit()
        result = self.habits.seed_onboarding('owner-a', [
            {'category': 'layout', 'slot': 'other', 'rule': '标题一律加粗'}, self.PICKS[2],
            {'category': 'integration', 'slot': 'required_pages', 'rule': '结尾有致谢页'},
            {'category': 'integration', 'slot': 'required_pages', 'rule': '开头有目录页'},
            {'category': 'expression', 'slot': 'banned_chart_types', 'rule': '不使用三维图表', 'value': {'kind': '3d'}}])
        # Nothing is replaced in these slots, so an unrelated rule of the owner blocks nothing.
        self.assertEqual([(x['slot'], x['rule']) for x in result['created']],
                         [('other', '标题一律加粗'), ('required_pages', '开头有目录页'), ('banned_chart_types', '不使用三维图表')])
        self.assertEqual(result['skipped'], [{'category': 'expression', 'slot': 'banned_chart_types', 'reason': 'explicit'},
                                             {'category': 'integration', 'slot': 'required_pages', 'reason': 'inferred'}])
        self.assertEqual({row.status for row in self.rows()}, {'active'})
        self.assertEqual(sorted(row.rule for row in self.rows(slot='other')), ['标题一律加粗', '页脚放单位 logo'])
        # The outcome no longer depends on which came first.
        self.habits.seed_onboarding('owner-b', [{'category': 'layout', 'slot': 'other', 'rule': '标题一律加粗'}])
        self.make(owner='owner-b', category='layout', slot='other', rule='页脚放单位 logo')
        self.assertEqual(sorted(row.rule for row in self.rows('owner-b', status='active')), ['标题一律加粗', '页脚放单位 logo'])
        # An edited pick stays next to the pick as it was first worded.
        edited = self.habits.update('owner-a', result['created'][0]['id'], {'rule': '标题加粗并放大', 'version': 1})
        again = self.habits.seed_onboarding('owner-a', [{'category': 'layout', 'slot': 'other', 'rule': '标题一律加粗'}])
        self.assertEqual((len(again['created']), db.session.get(self.models.DesignHabit, edited['id']).status), (1, 'active'))

    def test_seed_keeps_the_submitted_order_and_fits_one_generation(self):
        from services.wuyu_design_habits import MAX_ACTIVE_INJECTED, MAX_ONBOARDING_ITEMS
        self.assertEqual(MAX_ONBOARDING_ITEMS, MAX_ACTIVE_INJECTED)
        picks = [{'category': 'layout', 'slot': 'other', 'rule': '初始习惯 %02d' % number} for number in range(1, MAX_ONBOARDING_ITEMS + 1)]
        for attempt in range(3):  # ids are random: an order that only holds by chance fails within a few rounds
            owner = 'owner-%d' % attempt
            created = self.habits.seed_onboarding(owner, picks)['created']
            wanted = [pick['rule'] for pick in picks]
            self.assertEqual([x['rule'] for x in created], wanted)
            self.assertEqual(len({x['created_at'] for x in created}), len(picks))
            # Everything stored by one submission is applied, and listed, in the order it was picked.
            self.assertEqual([x['rule'] for x in self.habits.active_for(owner, {})], wanted)
            self.assertEqual([x['rule'] for x in self.habits.list(owner)['items']], wanted)
            self.assertEqual([x['rule'] for x in self.habits.export(owner)['habits']], wanted)
        self.assertCode('DESIGN_MEMORY_INVALID', 400, self.habits.seed_onboarding, 'owner-9', picks + [dict(picks[0], rule='多出的一条')])
        self.assertEqual(self.rows('owner-9'), [])
        # A later submission is newer as a whole than an earlier one.
        self.habits.seed_onboarding('owner-0', [{'category': 'layout', 'slot': 'palette', 'rule': '后来选的'}])
        self.assertEqual(self.habits.list('owner-0')['items'][0]['rule'], '后来选的')

    def _held(self, method, work):
        """Run work() in a second session while method of the habit service is paused inside its transaction."""
        inside, release, result, real = threading.Event(), threading.Event(), {}, getattr(self.habits, method)

        def paused(*args, **kwargs):
            inside.set(); release.wait(10)
            return real(*args, **kwargs)

        def first(call):
            with self.app.app_context():
                try:
                    with patch.object(self.habits, method, side_effect=paused):
                        result['first'] = call()
                finally:
                    db.session.remove()

        def second():
            with self.app.app_context():
                try:
                    result['second'] = work()
                finally:
                    db.session.remove()
        return inside, release, result, first, second

    def test_writers_of_one_owner_take_turns(self):
        # Separate sessions on the file database: the second writer must wait for the first one's commit.
        pick = [self.PICKS[0]]
        for name, work, expected in (
                ('same picks twice', lambda: self.habits.seed_onboarding('owner-a', pick),
                 lambda first, second: (len(first['created']), second['created'], second['skipped'])
                 == (1, [], [{'category': 'layout', 'slot': 'palette', 'reason': 'unchanged'}])),
                ('explicit habit during setup', lambda: self.make(category='layout', slot='palette', rule='我自己设的主色：墨绿'),
                 lambda first, second: len(first['created']) == 1 and second['status'] == 'active')):
            with self.subTest(name):
                self.models.DesignHabit.query.delete(); self.models.DesignHabitRevision.query.delete(); db.session.commit()
                db.session.remove()
                # _live_limit runs after the slot was read as free and before the pick is inserted.
                inside, release, result, first, second = self._held('_live_limit', work)
                one = threading.Thread(target=first, args=(lambda: self.habits.seed_onboarding('owner-a', pick),))
                one.start(); self.assertTrue(inside.wait(10))
                two = threading.Thread(target=second)
                two.start(); two.join(0.5)
                waited = two.is_alive()
                release.set(); one.join(10); two.join(10)
                self.assertTrue(waited, 'the second writer went ahead inside the first one\'s transaction')
                self.assertTrue(expected(result['first'], result['second']), result)
                rows = sorted((row.origin, row.status, row.superseded_by is not None) for row in self.rows())
                self.assertEqual(rows, [('onboarding', 'active', False)] if name == 'same picks twice'
                                 else [('explicit', 'active', False), ('onboarding', 'dormant', True)])

    def test_owner_lock_statements(self):
        from services import wuyu_design_habits as module
        calls = []

        class Connection:
            dialect = types.SimpleNamespace(name='postgresql')

            def execute(self, statement, parameters):
                calls.append((str(statement), parameters))
                if fail and 'pg_advisory_xact_lock' in str(statement):
                    raise OperationalError('s', {}, Exception('lock timeout'))
        for fail in (False, True):
            with patch.object(db.session, 'connection', return_value=Connection()):
                if fail:
                    self.assertCode('DESIGN_MEMORY_STORAGE_UNAVAILABLE', 503, self.habits._serialize, 'owner-a')
                else:
                    self.habits._serialize('owner-a'); self.habits._serialize('owner-a'); self.habits._serialize('owner-b')
        locks = [parameters for statement, parameters in calls if statement == 'SELECT pg_advisory_xact_lock(:namespace, :key)']
        self.assertEqual(len(calls), 8)
        self.assertEqual([statement for statement, _ in calls[:2]], ["SELECT set_config('lock_timeout', :timeout, true)",
                                                                     'SELECT pg_advisory_xact_lock(:namespace, :key)'])
        self.assertEqual(calls[0][1], {'timeout': '10000ms'})
        # One key per owner, stable across processes, inside the 32-bit range of the two-key lock function.
        self.assertEqual({lock['namespace'] for lock in locks}, {module.PG_OWNER_LOCK_NAMESPACE})
        self.assertEqual((locks[0]['key'], locks[0]['key'] == locks[2]['key']), (locks[1]['key'], False))
        self.assertTrue(all(-2 ** 31 <= lock['key'] < 2 ** 31 for lock in locks))
        self.assertNotEqual((module.PG_OWNER_LOCK_NAMESPACE, locks[0]['key']), (0x57555955, 0x44455347))
        # SQLite: the write lock is taken once, and a transaction that already wrote is left alone.
        self.habits._serialize('owner-a')
        self.assertTrue(db.session.connection().connection.driver_connection.in_transaction)
        self.habits._serialize('owner-a')
        db.session.rollback()
        db.session.add(self.models.DesignHabitPreference(owner_id='owner-z', paused=False)); db.session.flush()
        self.habits._serialize('owner-a')
        db.session.rollback()
        # Every writer that decides on a slot takes the lock first.
        with patch.object(self.habits, '_serialize', side_effect=self.habits._serialize) as lock:
            item = self.make()
            self.habits.update('owner-a', item['id'], {'pinned': True, 'version': 1})
            self.habits.seed_onboarding('owner-a', self.PICKS)
        self.assertEqual([call.args for call in lock.call_args_list], [('owner-a',)] * 3)

    def test_seed_validation_is_all_or_nothing(self):
        good = self.PICKS[0]
        for items in [None, {}, 'x', [], [good] * 13, [dict(good, rule='r%d' % n, scope={'page_role': str(n)}) for n in range(13)], [good, None], [good, {'category': 'layout', 'slot': 'palette'}],
                      [good, {'category': 'nope', 'slot': 'palette', 'rule': 'x'}],
                      [good, {'category': 'layout', 'slot': 'share_data_form', 'rule': 'x'}],
                      [good, {'category': 'layout', 'slot': 'fonts', 'rule': '  '}],
                      [good, {'category': 'layout', 'slot': 'fonts', 'rule': 'x' * 301}],
                      [good, {'category': 'layout', 'slot': 'fonts', 'rule': 'x', 'scope': {'purpose': '答辩'}}],
                      [good, {'category': 'layout', 'slot': 'fonts', 'rule': 'x', 'value': {'k': 'v' * 3000}}],
                      # What a client must not choose: not pinned, no origin, status or owner.
                      [good, {'category': 'layout', 'slot': 'fonts', 'rule': 'x', 'pinned': True}],
                      [good, {'category': 'layout', 'slot': 'fonts', 'rule': 'x', 'origin': 'explicit'}],
                      [good, {'category': 'layout', 'slot': 'fonts', 'rule': 'x', 'status': 'candidate'}],
                      [good, {'category': 'layout', 'slot': 'fonts', 'rule': 'x', 'owner_id': 'owner-b'}],
                      # Two different answers for one single-valued slot and scope.
                      [good, dict(good, rule='主色用墨绿')]]:
            self.assertCode('DESIGN_MEMORY_INVALID', 400, self.habits.seed_onboarding, 'owner-a', items)
        self.assertEqual((self.models.DesignHabit.query.count(), self.models.DesignHabitRevision.query.count()), (0, 0))
        # The same slot under different scopes is not a contradiction.
        result = self.habits.seed_onboarding('owner-a', [good, dict(good, rule='学术汇报主色用灰', scope={'purpose': 'research'})])
        self.assertEqual(len(result['created']), 2)
        # A failure while storing keeps nothing either, including what was already shelved in the same call.
        with patch.object(self.habits, '_revise', side_effect=[None, None, RuntimeError('boom')]):
            with self.assertRaises(RuntimeError):
                self.habits.seed_onboarding('owner-a', [dict(good, rule='主色用墨绿'), self.PICKS[2]])
        self.assertEqual(sorted((row.rule, row.status) for row in self.rows()), [('主色用深蓝', 'active'), ('学术汇报主色用灰', 'active')])

    def test_seed_tenant_isolation(self):
        self.make(owner='owner-b', category='layout', slot='palette', rule='主色用墨绿')
        mine = self.habits.seed_onboarding('owner-a', self.PICKS)
        # Another owner's explicit habit neither blocks this owner's pick nor is touched by it.
        self.assertEqual((len(mine['created']), mine['skipped']), (3, []))
        theirs = self.habits.seed_onboarding('owner-b', [dict(self.PICKS[0], rule='主色用橙'), self.PICKS[2]])
        self.assertEqual(theirs['skipped'], [{'category': 'layout', 'slot': 'palette', 'reason': 'explicit'}])
        self.assertEqual([x['slot'] for x in theirs['created']], ['banned_chart_types'])
        self.assertEqual({row.status for row in self.rows()}, {'active'})
        self.assertEqual(sorted(x['rule'] for x in self.habits.list('owner-b')['items']), ['不使用三维图表', '主色用墨绿'])
        self.assertEqual(len(self.habits.list('owner-a')['items']), 3)
        self.assertEqual({row.owner_id for row in self.models.DesignHabitRevision.query.filter_by(action='onboarding').all()}, {'owner-a', 'owner-b'})
        self.assertFalse({x['id'] for x in mine['created']} & {x['id'] for x in self.habits.list('owner-b')['items']})

    def test_seed_respects_limits(self):
        now = datetime.utcnow()
        fill = lambda count, status: db.session.add_all([self.models.DesignHabit(layer='personal', owner_id='owner-a',
            category='layout', slot='other', scope=dumps({'page_role': '%s%d' % (status, index)}), rule='r', status=status,
            valid_from=now) for index in range(count)])
        fill(98, 'active'); db.session.commit()
        # Room for two, three asked: nothing is stored.
        self.assertCode('HABIT_LIMIT', 409, self.habits.seed_onboarding, 'owner-a', self.PICKS)
        self.assertEqual((len(self.rows()), self.models.DesignHabitRevision.query.count()), (98, 0))
        self.assertEqual(len(self.habits.seed_onboarding('owner-a', self.PICKS[:2])['created']), 2)
        self.assertCode('HABIT_LIMIT', 409, self.habits.seed_onboarding, 'owner-a', self.PICKS[2:])
        # Picks that create nothing are answered even at the limit, and other owners are not affected.
        self.assertEqual(len(self.habits.seed_onboarding('owner-a', self.PICKS[:2])['skipped']), 2)
        self.assertEqual(len(self.habits.seed_onboarding('owner-b', self.PICKS)['created']), 3)
        # Shelved habits count against what may be stored in total.
        self.models.DesignHabit.query.filter_by(owner_id='owner-a').delete(); db.session.commit()
        fill(499, 'dormant'); db.session.commit()
        self.assertCode('HABIT_LIMIT', 409, self.habits.seed_onboarding, 'owner-a', self.PICKS[:2])
        self.assertEqual(len(self.rows(status='active')), 0)
        self.assertEqual(len(self.habits.seed_onboarding('owner-a', self.PICKS[:1])['created']), 1)

    def test_seed_while_paused_stores_but_applies_nothing(self):
        # Same as create(): the global pause holds habits back from use, it does not refuse storing them.
        self.habits.set_paused('owner-a', True)
        self.assertEqual(self.make(category='layout', slot='fonts')['status'], 'active')
        result = self.habits.seed_onboarding('owner-a', self.PICKS)
        self.assertEqual([(x['status'], x['paused']) for x in result['created']], [('active', False)] * 3)
        self.assertEqual(self.habits.active_for('owner-a', {}), [])
        self.habits.set_paused('owner-a', False)
        self.assertEqual(len(self.habits.active_for('owner-a', {'purpose': 'research'})), 4)

    def test_active_for_orders_explicit_then_onboarding_then_inferred(self):
        base = datetime.utcnow() - timedelta(days=1)
        # Inferred is the newest and explicit the oldest, so only the origin can produce the expected order.
        self.make(category='layout', slot='fonts', rule='explicit')
        self.models.DesignHabit.query.update({'created_at': base})
        db.session.add(self.models.DesignHabit(layer='personal', owner_id='owner-a', category='layout', slot='whitespace',
            scope='{}', rule='inferred', origin='inferred', status='active', valid_from=base, created_at=datetime.utcnow() + timedelta(hours=1)))
        db.session.commit()
        seeded = self.habits.seed_onboarding('owner-a', [{'category': 'layout', 'slot': 'palette', 'rule': 'onboarding'},
                                                        {'category': 'layout', 'slot': 'alignment', 'rule': 'onboarding pinned later'}])
        self.assertEqual([x['rule'] for x in self.habits.active_for('owner-a', {})][:1] + [x['origin'] for x in self.habits.active_for('owner-a', {})],
                         ['explicit', 'explicit', 'onboarding', 'onboarding', 'inferred'])
        # Pinning still comes first, whatever the origin; an onboarding habit is managed like any other.
        self.habits.update('owner-a', seeded['created'][1]['id'], {'pinned': True, 'version': 1})
        self.assertEqual([x['rule'] for x in self.habits.active_for('owner-a', {})],
                         ['onboarding pinned later', 'explicit', 'onboarding', 'inferred'])
        context = self.memory.retrieve('owner-a', '{}')['rules']
        self.assertEqual([rule['rule'] for rule in context], ['onboarding pinned later', 'explicit', 'onboarding', 'inferred'])


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
        picks = [{'category': 'layout', 'slot': 'palette', 'rule': '主色用橙'}, {'category': 'layout', 'slot': 'fonts', 'rule': '中文用微软雅黑'}]
        self.assertEqual(self.call('post', '/habits/onboarding', user=None, json={'items': picks})[0], 401)
        for body in (None, [], {}, picks, {'items': picks, 'origin': 'explicit'}, {'items': picks, 'owner_id': 'owner-b'}):
            status, answer = self.call('post', '/habits/onboarding', json=body)
            self.assertEqual((status, answer['error']['code']), (400, 'INVALID_REQUEST'))
        for body in ({'items': []}, {'items': 'x'}, {'items': picks + [{'category': 'layout'}]}, {'items': picks * 7},
                     {'items': [dict(picks[1], origin='explicit')]}):
            status, answer = self.call('post', '/habits/onboarding', json=body)
            self.assertEqual((status, answer['error']['code']), (400, 'DESIGN_MEMORY_INVALID'))
        status, body = self.call('post', '/habits/onboarding', json={'items': picks})
        self.assertEqual((status, body['data']['skipped']), (200, [{'category': 'layout', 'slot': 'palette', 'reason': 'explicit'}]))
        self.assertEqual([(x['slot'], x['origin'], x['status']) for x in body['data']['created']], [('fonts', 'onboarding', 'active')])
        seeded = body['data']['created'][0]['id']
        self.assertEqual(self.call('post', '/habits/onboarding', json={'items': picks})[1]['data']['created'], [])
        self.assertEqual(sorted(x['origin'] for x in self.call('get', '/habits')[1]['data']['items']), ['explicit', 'onboarding'])
        self.assertEqual(self.call('get', '/habits', user='owner-b')[1]['data']['items'], [])
        self.assertEqual(self.call('delete', '/habits/' + seeded, user='owner-b')[0], 404)
        self.assertEqual(self.call('delete', '/habits/' + seeded)[1]['data'], {'deleted': True})
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
