"""op195: deck type in the brief, template choice history and same-type template recommendation."""
import json
import types
import unittest
from unittest.mock import patch

from models import db, Task
from services import wuyu_deck_types as deck
from services.wuyu_template_review import template_key, record_template_choice
from services.wuyu_template_library import TemplateLibrary
from services.wuyu_workflow_service import WorkflowAPIError
import test_wuyu_design_habits as habit_fixture
import test_wuyu_lifecycle as fixture


class DeckTypeInferenceTests(unittest.TestCase):
    def kind(self, **brief):
        return deck.infer_deck_type(brief)

    def test_real_feedback_wording(self):
        self.assertEqual(self.kind(topic='无隅智循BP第23页「商业项目」业绩页重做'), 'business_plan')
        self.assertEqual(self.kind(goal='博士论文答辩，30页'), 'doctoral_defense')
        self.assertEqual(self.kind(topic='开题报告：固废资源化'), 'proposal_defense')
        self.assertEqual(self.kind(topic='组会文献分享'), 'academic_report')
        self.assertEqual(self.kind(topic='国自然项目申请书汇报'), 'grant')
        self.assertEqual(self.kind(topic='2026年终总结与述职'), 'work_report')
        self.assertEqual(self.kind(goal='面向技术团队的系统架构总览'), 'technical_proposal')

    def test_specific_before_generic_and_first_mention_wins(self):
        self.assertEqual(self.kind(topic='博士论文答辩'), 'doctoral_defense')   # not plain 论文答辩
        self.assertEqual(self.kind(topic='企业介绍，附融资计划'), 'company_intro')
        self.assertEqual(self.kind(topic='融资路演，含企业介绍'), 'business_plan')

    def test_occasion_outranks_degree(self):
        # Seen on staging: these are proposal defenses of a doctoral thesis, not final defenses.
        self.assertEqual(self.kind(topic='博士学位论文开题答辩PPT——难点三'), 'proposal_defense')
        self.assertEqual(self.kind(topic='难点三（博士学位论文开题答辩）'), 'proposal_defense')
        self.assertEqual(self.kind(topic='博士资格考核汇报'), 'qualifying_exam')
        self.assertEqual(self.kind(topic='硕士论文答辩'), 'master_defense')

    def test_topic_and_goal_outrank_requirements(self):
        self.assertEqual(self.kind(topic='公司介绍', requirements='参考我们的技术方案文档'), 'company_intro')
        self.assertEqual(self.kind(topic='新产品', requirements='做成商业计划书的结构'), 'business_plan')

    def test_latin_markers_are_whole_words(self):
        self.assertEqual(self.kind(topic='BP 路演'), 'business_plan')
        self.assertIsNone(self.kind(topic='abps 系统说明'))
        self.assertIsNone(self.kind(topic='master数据清洗'))  # 'master thesis' only

    def test_no_guess_when_nothing_names_a_type(self):
        self.assertIsNone(self.kind(topic='固废资源化利用', goal='讲清楚现状和问题'))
        self.assertIsNone(deck.effective_deck_type({'topic': '固废', 'subscenario': 'technical_proposal'}))
        self.assertEqual(deck.effective_deck_type({'topic': '固废', 'subscenario': 'proposal_defense'}), 'proposal_defense')

    def test_apply_to_brief_stated_wins_and_null_clears(self):
        brief = {'topic': '博士论文答辩'}
        deck.apply_to_brief(brief, {})
        self.assertEqual((brief['deck_type'], brief['deck_type_source']), ('doctoral_defense', 'inferred'))
        deck.apply_to_brief(brief, {'deck_type': 'business_plan'})
        self.assertEqual((brief['deck_type'], brief['deck_type_source']), ('business_plan', 'stated'))
        brief['topic'] = '硕士论文答辩'
        deck.apply_to_brief(brief, {'topic': brief['topic']})
        self.assertEqual(brief['deck_type'], 'business_plan')        # inference never overrides a stated value
        deck.apply_to_brief(brief, {'deck_type': None})
        self.assertNotIn('deck_type', brief)
        self.assertIsNone(deck.effective_deck_type(brief))           # explicitly cleared stays cleared
        with self.assertRaises(ValueError):
            deck.apply_to_brief(brief, {'deck_type': 'made_up'})

    def test_inference_follows_topic_until_stated(self):
        brief = {'topic': '开题答辩'}
        deck.apply_to_brief(brief, {})
        brief['topic'] = '固废利用'
        deck.apply_to_brief(brief, {'topic': '固废利用'})
        self.assertNotIn('deck_type', brief)
        self.assertNotIn('deck_type_source', brief)


class TemplateKeyTests(unittest.TestCase):
    def body(self, material):
        return {'materials': [material], 'workflow': {'project_id': 'p1'}}

    def test_keys_are_stable_across_projects(self):
        sha = 'a' * 64
        upload = self.body({'id': 'm1', 'role': 'template', 'sha256': sha})
        self.assertEqual(template_key(upload, {'source_kind': 'upload', 'source_id': 'material:m1'}), 'upload:' + sha)
        reused = self.body({'id': 'm2', 'role': 'template', 'sha256': sha, 'reused_from': {'id': 'm1', 'sha256': sha}})
        self.assertEqual(template_key(reused, {'source_kind': 'upload', 'source_id': 'material:m2'}), 'upload:' + sha)
        saved = self.body({'id': 'm3', 'role': 'template', 'personal_template_id': 'd1', 'sha256': 'b' * 64})
        self.assertEqual(template_key(saved, {'source_kind': 'upload', 'source_id': 'material:m3'}), 'personal:d1')
        self.assertEqual(template_key(self.body({}), {'source_kind': 'library', 'source_id': 'figure75-001'}),
                         'library:figure75-001')
        self.assertIsNone(template_key(upload, {'source_kind': 'upload', 'source_id': 'material:missing'}))
        self.assertIsNone(template_key(upload, None))


class ChoiceHistoryTests(habit_fixture.HabitFixture):
    def choose(self, key_material, deck_type, owner='owner-a', n=1, kind='upload'):
        for i in range(n):
            body = {'workflow': {'project_id': self.project}, 'brief': {'deck_type': deck_type, 'deck_type_source': 'stated'},
                    'materials': [{'id': 'm', 'role': 'template', 'sha256': key_material}],
                    'template_choice': {'source_kind': kind, 'source_id': 'material:m' if kind == 'upload' else key_material,
                                        'title': 't'}}
            record_template_choice(self.app, owner, body, request_key='k:%s:%s:%s:%d' % (owner, key_material, deck_type, i))
        db.session.commit()

    def history(self, owner, deck_type):
        return TemplateLibrary._choice_history(types.SimpleNamespace(app=self.app), owner, deck_type)

    def test_same_type_history_most_used_then_latest(self):
        a, b, c = 'a' * 64, 'b' * 64, 'c' * 64
        self.choose(a, 'business_plan', n=2)
        self.choose(b, 'business_plan')
        self.choose(c, 'doctoral_defense', n=3)
        self.choose('figure75-001', 'business_plan', kind='library')
        self.assertEqual(self.history('owner-a', 'business_plan'),
                         [('upload:' + a, 2), ('library:figure75-001', 1), ('upload:' + b, 1)])
        self.assertEqual(self.history('owner-a', 'doctoral_defense'), [('upload:' + c, 3)])
        self.assertEqual(self.history('owner-b', 'business_plan'), [])           # owner isolation
        self.assertEqual(dict(self.history('owner-a', None))['upload:' + c], 3)  # no type: all choices

    def test_paused_owner_neither_records_nor_gets_history(self):
        self.choose('d' * 64, 'business_plan')
        self.habits.set_paused('owner-a', True)
        self.assertEqual(self.history('owner-a', 'business_plan'), [])
        self.choose('e' * 64, 'business_plan')
        self.habits.set_paused('owner-a', False)
        self.assertEqual(self.history('owner-a', 'business_plan'), [('upload:' + 'd' * 64, 1)])

    def test_event_payload(self):
        self.choose('f' * 64, 'grant')
        row = self.models.DesignEvent.query.filter_by(kind='template_choice').one()
        self.assertEqual(json.loads(row.payload), {'template_key': 'upload:' + 'f' * 64, 'source_kind': 'upload',
                                                   'title': 't', 'deck_type': 'grant'})
        self.assertEqual(json.loads(row.context), {'deck_type': 'grant'})


class BriefDeckTypeActionTests(unittest.TestCase):
    def setUp(self):
        fixture.LifecycleTests.setUp(self)
        for target in ('services.wuyu_providers.WuyuGateway', 'services.wuyu_workflow_quota.WorkflowCharge.reserve'):
            blocker = patch(target, side_effect=AssertionError('update_brief must not dispatch or reserve'))
            blocker.start()
            self.addCleanup(blocker.stop)

    def test_stated_deck_type_is_metadata_only(self):
        before = self.body()
        self.action('update_brief', {'deck_type': 'grant'})
        after = self.body()
        self.assertEqual((after['brief']['deck_type'], after['brief']['deck_type_source']), ('grant', 'stated'))
        for key in ('outline', 'slide_specs', 'materials', 'artifacts', 'workflow', 'epoch'):
            self.assertEqual(after[key], before[key], key)

    def test_topic_change_infers_and_unknown_type_is_refused(self):
        self.action('update_brief', {'topic': '博士论文答辩：固废资源化'})
        self.assertEqual(self.body()['brief'].get('deck_type'), 'doctoral_defense')
        self.assertEqual(self.body()['brief'].get('deck_type_source'), 'inferred')
        with self.assertRaises(WorkflowAPIError):
            self.action('update_brief', {'deck_type': 'not_a_type'})


# Borrow every helper of the lifecycle fixture (but none of its tests).
for _name, _value in vars(fixture.LifecycleTests).items():
    if not _name.startswith(('test_', '__')) and _name != 'setUp':
        setattr(BriefDeckTypeActionTests, _name, _value)


def _record(ident):
    return {'id': ident, 'title': ident, 'review_status': 'approved', 'reference_sufficient': True,
            'pages': [{'selected': True}] * 5, 'tags': {'purpose': []}}


class RecommendOrderTests(unittest.TestCase):
    """Personal same-type history, then the administrator's type mapping, then own templates, then library."""

    def recommend(self, brief, history=(), mapping=None, own=(), has_history=True):
        from contextlib import contextmanager
        records = {ident: _record(ident) for ident in ('L1', 'L2', 'L3', 'L4', 'L5')}

        @contextmanager
        def state():
            yield {'sources': records}
        memory = types.SimpleNamespace(policy=lambda: {'settings': {'template_by_deck_type': mapping or {}}})
        stub = types.SimpleNamespace(
            app=types.SimpleNamespace(extensions={'wuyu_design_memory': memory}, config={'UPLOAD_FOLDER': '/tmp'}),
            prepare=lambda: None, _state=state, _card=lambda record, detail=False: {'id': record['id']},
            _choice_history=lambda owner, deck_type: list(history),
            _has_template_history=lambda owner: has_history)
        with patch('services.wuyu_uploaded_templates.UploadedTemplates.keyed', return_value=list(own)):
            return TemplateLibrary.recommend(stub, types.SimpleNamespace(id='u1'), brief)

    def test_full_order_and_reasons(self):
        own = [('upload:' + 'a' * 64, {'id': 'uploaded:x'}), ('upload:' + 'b' * 64, {'id': 'uploaded:y'})]
        result = self.recommend({'topic': 'PinTaste 商业计划书'},
                                history=[('upload:' + 'a' * 64, 2), ('library:L1', 1)],
                                mapping={'business_plan': ['L2', 'L3', 'L4']}, own=own)
        self.assertEqual((result['deck_type'], result['deck_type_label']), ('business_plan', '商业计划'))
        self.assertEqual([(i['id'], i['recommendation_reason']) for i in result['items']], [
            ('uploaded:x', '你做商业计划常用的模板（已用2次）'),
            ('L1', '你上次做商业计划时用的模板'),
            ('L2', '推荐用于商业计划的模板'),
            ('L3', '推荐用于商业计划的模板')])

    def test_without_history_or_mapping_own_templates_then_library(self):
        own = [('upload:' + 'a' * 64, {'id': 'uploaded:x'}), ('upload:' + 'b' * 64, {'id': 'uploaded:y'}),
               ('upload:' + 'c' * 64, {'id': 'uploaded:z'})]
        result = self.recommend({'topic': '年终总结'}, own=own)
        ids = [i['id'] for i in result['items']]
        self.assertEqual(ids[:2], ['uploaded:x', 'uploaded:y'])          # still at most two personal cards
        self.assertEqual(len(ids), 4)
        self.assertTrue(all(i['recommendation_reason'].startswith('适合工作汇报；') for i in result['items'][2:]))

    def test_unknown_type_ignores_mapping_and_labels_history_generically(self):
        result = self.recommend({'topic': '固废资源化'}, history=[('library:L5', 1)],
                                mapping={'business_plan': ['L2']})
        self.assertIsNone(result['deck_type'])
        self.assertEqual(result['items'][0], {'id': 'L5', 'recommendation_reason': '你最近用过的模板'})
        self.assertNotIn('推荐用于', ''.join(i['recommendation_reason'] for i in result['items']))


    def test_first_template_flag(self):
        self.assertFalse(self.recommend({'topic': '年终总结'})['first_template'])
        self.assertTrue(self.recommend({'topic': '年终总结'}, has_history=False)['first_template'])


class FirstTemplateTests(habit_fixture.HabitFixture):
    """op200: whether the owner has ever confirmed a template decides if 推荐 is shown first."""
    choose = ChoiceHistoryTests.choose

    def has(self, owner='owner-a'):
        return TemplateLibrary._has_template_history(types.SimpleNamespace(app=self.app), owner)

    def workflow(self, choice, owner='owner-a'):
        from models import Project
        from models.wuyu_workflow import WuyuWorkflow
        import uuid
        project = str(uuid.uuid4())
        db.session.add(Project(id=project, user_id=owner, idea_prompt='fixture'))
        db.session.add(WuyuWorkflow(project_id=project, revision=1,
                                    body=json.dumps({'workflow': {'project_id': project}, 'template_choice': choice})))
        db.session.commit()

    def test_new_owner_then_after_a_choice(self):
        self.assertFalse(self.has())
        self.choose('a' * 64, 'grant')
        self.assertTrue(self.has())
        self.assertFalse(self.has('owner-b'))

    def test_paused_owner_falls_back_to_own_workflows(self):
        self.habits.set_paused('owner-a', True)
        self.workflow(None)                      # key present but no choice (e.g. reset after material change)
        self.assertFalse(self.has())
        self.workflow({'source_kind': 'library', 'source_id': 'L1', 'title': 't'}, owner='owner-b')
        self.assertFalse(self.has())             # someone else's project does not count
        self.workflow({'source_kind': 'library', 'source_id': 'L1', 'title': 't'})
        self.assertTrue(self.has())

    def test_events_count_even_while_paused(self):
        self.choose('b' * 64, 'grant')
        self.habits.set_paused('owner-a', True)
        self.assertTrue(self.has())

    def test_unavailable_habits_use_workflows_and_errors_keep_todays_view(self):
        app = types.SimpleNamespace(extensions={})
        self.assertFalse(TemplateLibrary._has_template_history(types.SimpleNamespace(app=app), 'owner-a'))
        self.workflow({'source_kind': 'upload', 'source_id': 'material:m', 'title': 't'})
        self.assertTrue(TemplateLibrary._has_template_history(types.SimpleNamespace(app=app), 'owner-a'))
        broken = types.SimpleNamespace(available=True, paused=lambda owner: (_ for _ in ()).throw(RuntimeError()))
        with patch('models.wuyu_design_habits.DesignEvent.query') as query:
            query.filter_by.return_value.first.return_value = None
            app = types.SimpleNamespace(extensions={'wuyu_design_habits': broken})
            self.assertTrue(TemplateLibrary._has_template_history(types.SimpleNamespace(app=app), 'owner-a'))


class TemplateMappingPolicyTests(unittest.TestCase):
    def validate(self, value, options=('L1', 'L2', 'L3', 'L4')):
        from services.wuyu_design_memory import DesignMemoryService
        library = types.SimpleNamespace(global_options=lambda: [{'id': i, 'title': i} for i in options])
        stub = types.SimpleNamespace(app=types.SimpleNamespace(extensions={'wuyu_template_library': library}),
                                     _library=lambda: library)
        return DesignMemoryService._template_mapping(stub, value)

    def test_library_is_created_when_not_yet_registered(self):
        # Live 2026-11-23: the app registers the library lazily (first template request), so the
        # settings page must not depend on it having been registered already.
        from services.wuyu_design_memory import DesignMemoryService
        created = object()
        stub = types.SimpleNamespace(app=types.SimpleNamespace(extensions={}))
        with patch('services.wuyu_template_library.get_template_library', return_value=created) as factory:
            self.assertIs(DesignMemoryService._library(stub), created)
        factory.assert_called_once_with(stub.app)
        stub.app.extensions['wuyu_template_library'] = 'registered'
        self.assertEqual(DesignMemoryService._library(stub), 'registered')

    def test_valid_mapping_and_empty_lists_dropped(self):
        self.assertEqual(self.validate({'grant': ['L1', 'L2'], 'award': []}), {'grant': ['L1', 'L2']})
        self.assertEqual(self.validate({}), {})

    def test_refusals(self):
        from services.wuyu_design_memory import DesignMemoryError
        for bad in ({'not_a_type': ['L1']}, {'grant': ['L1', 'L2', 'L3', 'L4']}, {'grant': ['L1', 'L1']},
                    {'grant': ['unknown']}, {'grant': 'L1'}, ['grant'], {'grant': [1]}):
            with self.assertRaises(DesignMemoryError, msg=repr(bad)):
                self.validate(bad)

    def test_default_round_trips_through_policy_defaults(self):
        from services.wuyu_design_memory import DEFAULTS
        self.assertEqual(DEFAULTS['template_by_deck_type'], {})


if __name__ == '__main__':
    unittest.main()
