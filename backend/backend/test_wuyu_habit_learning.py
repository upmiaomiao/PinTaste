"""op196: learning design habits from revision requests, promotion rules, billing and notices."""
from datetime import datetime, timedelta
import json
import types
import unittest
import uuid
from unittest.mock import patch

from models import db, Project
from models.wuyu_design_memory import DesignFeedback, DesignCompileRun
from services import wuyu_quota
from services.wuyu_habit_learning import HabitLearner, get_learner
from services.wuyu_design_memory import DEFAULTS, DesignMemoryError
import test_wuyu_design_habits as habit_fixture

CFG = dict(DEFAULTS)


class FakeQuota:
    def __init__(self, fail=None):
        self.fail, self.reserved, self.settled, self.refunded = fail, [], [], []

    def policy(self):
        return {'settings': wuyu_quota.DEFAULT_POLICY}

    def reserve(self, user_id, key, kind, **kwargs):
        if self.fail:
            raise self.fail
        hold = {'id': 'hold-%d' % len(self.reserved), 'held_points': 3, 'tariff': None, 'key': key}
        self.reserved.append(hold)
        return hold

    def settle(self, ident, actual_points=None):
        self.settled.append((ident, actual_points))

    def refund(self, ident, reason=''):
        self.refunded.append(ident)


class FakeGateway:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []
        self.last_response_metadata = {'usage': {'prompt_tokens': 4000, 'completion_tokens': 300}}

    def text(self, role, prompt, **kwargs):
        self.prompts.append(json.loads(prompt))
        value = self.answer(self.prompts[-1]) if callable(self.answer) else self.answer
        return json.dumps(value, ensure_ascii=False)


class LearnerFixture(habit_fixture.HabitFixture):
    def setUp(self):
        super().setUp()
        self.learner = get_learner(self.app)
        self.project_b = str(uuid.uuid4())
        db.session.add(Project(id=self.project_b, user_id='owner-a', idea_prompt='second deck'))
        db.session.commit()
        self.quota = FakeQuota()
        for target, value in (('services.wuyu_quota.get_quota_service', lambda app: self.quota),
                              ('services.wuyu_identity.authorize_paid', lambda *a, **k: None)):
            p = patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def feedback(self, text, project=None, owner='owner-a', minutes_ago=60):
        row = DesignFeedback(owner_id=owner, project_id=project or self.project, request_key=str(uuid.uuid4()),
                             action='revise_page', text=text, created_at=datetime.utcnow() - timedelta(minutes=minutes_ago))
        db.session.add(row)
        db.session.commit()
        return row

    def preference(self, row, quote, slot='icon_style', category='expression', rule='使用平面线条图标，不用3D图标',
                   same_as=None, generalize=False):
        return {'feedback_id': row.id, 'slot_key': category + '.' + slot, 'rule': rule,
                'polarity': 'avoid', 'quote': quote, 'generalize': generalize, 'same_as': same_as, 'purpose': None}

    def learn_with(self, rows, items, project=None, cfg=CFG):
        gateway = FakeGateway({'items': items})
        return self.learner.learn('owner-a', project or self.project, rows, cfg, gateway=gateway), gateway

    def habit_rows(self):
        return self.models.DesignHabit.query.filter_by(owner_id='owner-a').all()


class ValidationTests(LearnerFixture):
    def test_only_grounded_catalogue_preferences_survive(self):
        row = self.feedback('第1页不要有太多的3D图标，改用平面线条')
        known = []
        raw = {'items': [
            self.preference(row, '不要有太多的3D图标'),                               # kept
            self.preference(row, '这句话原文里没有'),                                  # quote not verbatim
            dict(self.preference(row, '改用平面线条'), slot_key='expression.not_a_slot'),  # unknown slot
            dict(self.preference(row, '改用平面线条'), kind='one_off'),                # one-off instruction
            dict(self.preference(row, '改用平面线条'), feedback_id='other'),           # unknown feedback
            dict(self.preference(row, '改用平面线条'), same_as='made-up-id'),          # unknown habit -> None
        ]}
        out = self.learner.validate(raw, [row], known)
        self.assertEqual([i['quote'] for i in out], ['不要有太多的3D图标', '改用平面线条'])
        self.assertIsNone(out[1]['same_as'])

    def test_generalizing_words_force_generalize(self):
        row = self.feedback('以后都用微软雅黑，正文黑色')
        out = self.learner.validate({'items': [dict(self.preference(row, '以后都用微软雅黑', slot='fonts', category='layout',
                                                                     rule='中文用微软雅黑'), generalize=False)]}, [row], [])
        self.assertTrue(out[0]['generalize'])

    def test_model_generalize_flag_is_ignored_and_two_per_request(self):
        # Dry run 2026-11-23: "严格按用户提供的15页逐页文稿制作" came back with generalize=true.
        row = self.feedback('严格按用户提供的15页逐页文稿制作，微软雅黑，正文黑色，不新增谢谢页，控制版式复杂度')
        items = [dict(self.preference(row, '微软雅黑', slot='fonts', category='layout', rule='中文用微软雅黑'), generalize=True),
                 dict(self.preference(row, '正文黑色', slot='fonts', category='layout', rule='正文用黑色'), generalize=True),
                 dict(self.preference(row, '控制版式复杂度', slot='other', category='layout', rule='版式简洁'), generalize=True)]
        out = self.learner.validate({'items': items}, [row], [])
        self.assertEqual([i['rule'] for i in out], ['中文用微软雅黑', '正文用黑色'])
        self.assertFalse(any(i['generalize'] for i in out))

    def test_slot_key_and_real_model_shapes(self):
        # Seen in the 2026-11-23 dry run: the label in "category" and the code in "slot".
        rows = [self.feedback('第1页不要有太多的3D图标，改用平面线条') for _ in range(5)]
        base = {'rule': '不用3D图标', 'quote': '不要有太多的3D图标', 'polarity': 'avoid'}
        shapes = [dict(base, feedback_id=rows[0].id, slot_key='expression.icon_style'),       # the format we ask for
                  dict(base, feedback_id=rows[1].id, category='图标风格', slot='icon_style'),   # label in category
                  dict(base, feedback_id=rows[2].id, slot_key='icon_style'),                  # bare unique slot
                  dict(base, feedback_id=rows[3].id, category='版式风格', slot='other'),        # ambiguous -> dropped
                  dict(base, feedback_id=rows[4].id, slot_key='expression.not_a_slot')]
        out = self.learner.validate({'items': shapes}, rows, [])
        self.assertEqual([(i['category'], i['slot']) for i in out], [('expression', 'icon_style')] * 3)

    def test_garbage_output_yields_nothing(self):
        row = self.feedback('随便')
        for raw in (None, [], {'items': 'x'}, {'items': [1, 'a', None]}):
            self.assertEqual(self.learner.validate(raw, [row], []), [])


class PromotionTests(LearnerFixture):
    def test_one_project_makes_a_candidate_two_projects_activate(self):
        a = self.feedback('第1页不要有太多的3D图标，改用平面线条')
        result, gateway = self.learn_with([a], [self.preference(a, '不要有太多的3D图标')])
        self.assertEqual((result['status'], result['candidates'], result['activated']), ('completed', 1, 0))
        habit = self.habit_rows()[0]
        self.assertEqual((habit.origin, habit.status, habit.project_count), ('inferred', 'candidate', 1))
        self.assertIsNone(self.learner.notice_for(self.project, {'deck_type_source': 'stated'}))
        # The prompt carries the catalogue, the existing habits and the feedback, nothing else about the user.
        self.assertEqual(set(gateway.prompts[0]), {'task', 'examples', 'output_schema', 'slot_keys', 'deck_type', 'known_habits', 'feedback', 'return'})
        self.assertEqual(len(gateway.prompts[0]['slot_keys']), 21)

        b = self.feedback('封面别用3D立体图标', project=self.project_b)
        result, gateway = self.learn_with([b], [self.preference(b, '别用3D立体图标', same_as=habit.id)], project=self.project_b)
        self.assertEqual(gateway.prompts[0]['known_habits'][0]['id'], habit.id)
        db.session.refresh(habit)
        self.assertEqual((result['activated'], habit.status, habit.project_count), (1, 'active', 2))
        self.assertIsNone(habit.last_confirmed_at)
        notice = self.learner.notice_for(self.project_b, {'deck_type_source': 'stated'})
        self.assertEqual([item['id'] for item in notice['learned']], [habit.id])
        self.assertEqual(notice['learned'][0]['quotes'], ['不要有太多的3D图标', '别用3D立体图标'])
        self.assertIn('respond_design_habit_notice', notice['instruction'])
        # Active learned habits are applied like any other habit.
        self.assertEqual([h['id'] for h in self.habits.active_for('owner-a', {})], [habit.id])

    def test_same_project_twice_is_still_one_project(self):
        a1 = self.feedback('不要3D图标')
        self.learn_with([a1], [self.preference(a1, '不要3D图标')])
        habit = self.habit_rows()[0]
        a2 = self.feedback('还是不要3D图标')
        self.learn_with([a2], [self.preference(a2, '不要3D图标', same_as=habit.id)])
        db.session.refresh(habit)
        self.assertEqual((habit.status, habit.project_count, len(json.loads(habit.evidence))), ('candidate', 1, 2))

    def test_generalized_request_activates_at_once_and_supersedes_single_slot(self):
        old = self.habits.create('owner-a', {'category': 'layout', 'slot': 'fonts', 'rule': '中文用宋体'})
        row = self.feedback('以后都用微软雅黑')
        result, _ = self.learn_with([row], [self.preference(row, '以后都用微软雅黑', slot='fonts', category='layout', rule='中文用微软雅黑')])
        self.assertEqual(result['activated'], 1)
        statuses = {h.rule: h.status for h in self.habit_rows()}
        self.assertEqual(statuses, {'中文用宋体': 'dormant', '中文用微软雅黑': 'active'})
        self.assertEqual(self.models.DesignHabit.query.get(old['id']).superseded_by,
                         next(h.id for h in self.habit_rows() if h.rule == '中文用微软雅黑'))

    def test_undo_is_final(self):
        row = self.feedback('以后都不要人物头像')
        self.learn_with([row], [self.preference(row, '以后都不要人物头像', slot='real_photos', rule='不使用人物头像照片')])
        habit = self.habit_rows()[0]
        out = self.learner.respond('owner-a', [habit.id], 'undo')
        self.assertEqual(out['changed'][0]['status'], 'revoked')
        again = self.feedback('以后都不要人物头像', project=self.project_b)
        result, _ = self.learn_with([again], [self.preference(again, '以后都不要人物头像', slot='real_photos', rule='不使用人物头像照片',
                                                       same_as=habit.id)], project=self.project_b)
        db.session.refresh(habit)
        self.assertEqual((result['skipped_revoked'], habit.status), (1, 'revoked'))
        self.assertEqual(len(self.habit_rows()), 1)
        self.assertEqual(self.habits.active_for('owner-a', {}), [])

    def test_announced_once(self):
        row = self.feedback('以后每页都不超过三个要点')
        self.learn_with([row], [self.preference(row, '以后每页都不超过三个要点', slot='points_per_page', category='integration',
                                         rule='每页不超过三个要点')])
        habit = self.habit_rows()[0]
        self.assertTrue(self.learner.notice_for(self.project, {'deck_type_source': 'stated'})['learned'])
        self.learner.respond('owner-a', [habit.id], 'announced')
        self.assertIsNone(self.learner.notice_for(self.project, {'deck_type_source': 'stated'}))
        self.assertEqual(self.habit_rows()[0].status, 'active')

    def test_respond_is_owner_scoped_and_validated(self):
        row = self.feedback('以后都不用动画')
        self.learn_with([row], [self.preference(row, '以后都不用动画', slot='animation', rule='不使用动画')])
        habit = self.habit_rows()[0]
        self.assertEqual(self.learner.respond('owner-b', [habit.id], 'undo')['changed'], [])
        for ids, action in (([habit.id], 'delete'), ([], 'undo'), ('x', 'undo'), ([1], 'undo')):
            with self.assertRaises(DesignMemoryError):
                self.learner.respond('owner-a', ids, action)


class BillingAndSchedulingTests(LearnerFixture):
    def test_charged_settled_and_processed_once(self):
        row = self.feedback('不要3D图标')
        result, _ = self.learn_with([row], [self.preference(row, '不要3D图标')])
        self.assertEqual(len(self.quota.reserved), 1)
        self.assertTrue(self.quota.reserved[0]['key'].startswith('design-memory:'))
        self.assertEqual(self.quota.settled, [('hold-0', result['points'])])
        self.assertEqual(self.learner.pending(CFG), [])
        again, _ = self.learn_with([row], [self.preference(row, '不要3D图标')])
        self.assertTrue(again['reused'])
        self.assertEqual(len(self.quota.reserved), 1)

    def test_no_points_learns_nothing_and_charges_nothing(self):
        self.quota.fail = wuyu_quota.QuotaError('QUOTA_INSUFFICIENT', 'no points')
        row = self.feedback('以后都不要3D图标')
        result, gateway = self.learn_with([row], [self.preference(row, '以后都不要3D图标')])
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual((gateway.prompts, self.habit_rows(), self.quota.settled), ([], [], []))

    def test_model_failure_refunds(self):
        row = self.feedback('不要3D图标')
        broken = FakeGateway(None)
        broken.text = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('provider down'))
        result = self.learner.learn('owner-a', self.project, [row], CFG, gateway=broken)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.quota.refunded, ['hold-0'])
        self.assertEqual(self.habit_rows(), [])

    def test_daily_budget_defers_without_recording(self):
        row = self.feedback('不要3D图标')
        result, gateway = self.learn_with([row], [self.preference(row, '不要3D图标')], cfg=dict(CFG, personal_daily_points=1))
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual((gateway.prompts, DesignCompileRun.query.count()), ([], 0))
        self.assertEqual(len(self.learner.pending(CFG)), 1)        # still waiting for another day

    def test_pending_waits_for_quiet_projects_and_respects_pause(self):
        self.feedback('不要3D图标', minutes_ago=5)
        self.assertEqual(self.learner.pending(CFG), [])
        self.feedback('页面留白多一点', project=self.project_b, minutes_ago=60)
        self.assertEqual([p for _, p, _ in self.learner.pending(CFG)], [self.project_b])
        self.habits.set_paused('owner-a', True)
        self.assertEqual(self.learner.pending(CFG), [])
        self.assertEqual(self.learner.tick(dict(CFG, habit_learning_enabled=False)), [])

    def test_paused_owner_is_never_charged(self):
        row = self.feedback('不要3D图标')
        self.habits.set_paused('owner-a', True)
        result, gateway = self.learn_with([row], [self.preference(row, '不要3D图标')])
        self.assertEqual((result['status'], gateway.prompts, self.quota.reserved), ('paused', [], []))


class NoticeTests(LearnerFixture):
    def test_applied_habits_and_deck_type_block(self):
        habit = self.habits.create('owner-a', {'category': 'layout', 'slot': 'palette', 'rule': '主色用深蓝'})
        self.habits.log_injection('owner-a', self.project, [habit['id']], [])
        db.session.commit()
        notice = self.learner.notice_for(self.project, {'deck_type': 'grant', 'deck_type_source': 'inferred'})
        self.assertEqual(notice['applied'], ['主色用深蓝'])
        self.assertEqual((notice['deck_type']['value'], notice['deck_type']['label'], notice['deck_type']['source']),
                         ('grant', '项目申请', 'inferred'))
        self.assertEqual(len(notice['deck_type']['options']), 13)
        self.assertNotIn('learned', notice)
        stated = self.learner.notice_for(self.project, {'deck_type': 'grant', 'deck_type_source': 'stated'})
        self.assertNotIn('deck_type', stated)
        self.habits.set_paused('owner-a', True)
        self.assertIsNone(self.learner.notice_for(self.project, {}))


if __name__ == '__main__':
    unittest.main()
