"""Page locking: state invariants, action validation and partial re-planning.

Pure tests: no database, provider or Flask application is needed.
"""
import importlib
import itertools
import sys
import types
import unittest
from copy import deepcopy
from pathlib import Path

if 'services' not in sys.modules:
    package = types.ModuleType('services')
    package.__path__ = [str(Path(__file__).resolve().parent / 'services')]
    sys.modules['services'] = package

state_mod = importlib.import_module('services.wuyu_state')
lock = importlib.import_module('services.wuyu_page_lock')
action_state = importlib.import_module('services.wuyu_action_state')
WorkflowError, apply_event, new_state, phase = (state_mod.WorkflowError, state_mod.apply_event,
                                                state_mod.new_state, state_mod.phase)

_ids = itertools.count()


def eid():
    return 'e%d' % next(_ids)


def ev(state, kind, **payload):
    return apply_event(state, {'event_id': eid(), 'type': kind, 'payload': payload})


class APIError(Exception):
    def __init__(self, message, code='WORKFLOW_ERROR', status=400):
        super().__init__(message)
        self.code, self.status = code, status


def deps():
    return types.SimpleNamespace(error_type=APIError, apply_event=apply_event, event_id=eid, phase=phase)


def generated_deck(count=4):
    s = new_state('p1', 'batch', 'template_and_final')
    for key, value in [('scenario', 'research'), ('audience', 'a'), ('page_count', count), ('style', 's')]:
        s = ev(s, 'requirement.selected', key=key, value=value)
    s = ev(s, 'template.selected', template_id='lib')
    s = ev(s, 'template.choice_confirmed', template_revision=s['template']['revision'], source_kind='library')
    for page in range(1, count + 1):
        attempt = eid()
        s = ev(s, 'page.generate_started', number=page, attempt_id=attempt)
        s = ev(s, 'page.generated', number=page, attempt_id=attempt, copy='c%d' % page, image_uri='artifact:img%d' % page)
    return s


def body_for(state):
    count = len(state['pages'])
    return {'workflow': state, 'brief': {'creation_kind': 'deck', 'page_count': count, 'requirements': '原需求'},
            'slide_specs': [{'number': n, 'title': 'T%d' % n, 'copy': 'C%d' % n} for n in range(1, count + 1)],
            'paused': False}


class StateTests(unittest.TestCase):
    def test_lock_requires_current_image(self):
        s = new_state('p1', 'batch', 'template_and_final')
        for key, value in [('scenario', 'r'), ('audience', 'a'), ('page_count', 3), ('style', 's')]:
            s = ev(s, 'requirement.selected', key=key, value=value)
        s = ev(s, 'template.selected', template_id='lib')
        s = ev(s, 'template.choice_confirmed', template_revision=s['template']['revision'], source_kind='library')
        with self.assertRaises(WorkflowError):
            ev(s, 'pages.locked', numbers=[1], locked=True)

    def test_locked_page_rejects_change_and_regeneration(self):
        s = ev(generated_deck(), 'pages.locked', numbers=[2], locked=True, source='user_ui')
        self.assertEqual(state_mod.locked_numbers(s), [2])
        with self.assertRaises(WorkflowError):
            ev(s, 'page.changed', number=2, change='x')
        with self.assertRaises(WorkflowError):
            ev(s, 'page.generate_started', number=2, attempt_id=eid())

    def test_global_resets_require_unlock(self):
        s = ev(generated_deck(), 'pages.locked', numbers=[1], locked=True)
        with self.assertRaises(WorkflowError):
            ev(s, 'template.selected', template_id='other')
        with self.assertRaises(WorkflowError):
            ev(s, 'requirement.selected', key='style', value='new')
        s = ev(s, 'pages.locked', numbers=[1], locked=False)
        ev(s, 'template.selected', template_id='other')

    def test_unlocked_change_keeps_locked_image_and_trace_versions(self):
        s = ev(generated_deck(), 'pages.locked', numbers=[1, 3], locked=True)
        before = deepcopy(s['pages'])
        s = ev(s, 'page.changed', number=2, change='new', fresh=True)
        self.assertTrue(s['pages'][1]['fresh_render'])
        self.assertEqual(phase(s), 'page_generation')
        for index in (0, 2, 3):
            self.assertEqual(s['pages'][index]['image_version'], before[index]['image_version'])
            self.assertEqual(s['pages'][index]['artifact'], before[index]['artifact'])
        attempt = eid()
        s = ev(s, 'page.generate_started', number=2, attempt_id=attempt)
        s = ev(s, 'page.generated', number=2, attempt_id=attempt, copy='n', image_uri='artifact:new2')
        self.assertNotIn('fresh_render', s['pages'][1])
        self.assertEqual(phase(s), 'final_image_review')

    def test_resize_keeps_pages_and_rejects_removing_locked(self):
        s = ev(generated_deck(4), 'pages.locked', numbers=[4], locked=True)
        with self.assertRaises(WorkflowError):
            ev(s, 'pages.resized', count=3)
        s = ev(s, 'pages.resized', count=6)
        self.assertEqual(len(s['pages']), 6)
        self.assertEqual(s['requirements']['values']['page_count'], 6)
        self.assertEqual(s['pages'][3]['image_version'], 1)
        self.assertEqual(s['pages'][5]['generation_status'], 'idle')
        self.assertEqual(phase(s), 'page_generation')


class ActionTests(unittest.TestCase):
    def test_lock_action_sources(self):
        body = body_for(generated_deck())
        with self.assertRaises(APIError):
            lock.validate_lock_payload(body, {'numbers': [1], 'locked': True, 'source': 'manual_edit'}, deps())
        with self.assertRaises(APIError):
            lock.validate_lock_payload(body, {'numbers': [9], 'locked': True}, deps())
        action_state.apply_action_state(body, 'lock_pages', {'numbers': [1, 2], 'locked': True, 'source': 'user_chat'},
                                        project_id='p1', pending_resume=None, deps=deps())
        self.assertEqual(state_mod.locked_numbers(body['workflow']), [1, 2])
        self.assertEqual(body['workflow']['pages'][0]['lock_source'], 'user_chat')

    def test_regenerate_defaults_to_current_count_and_queues_replan(self):
        body = body_for(ev(generated_deck(), 'pages.locked', numbers=[1], locked=True))
        action_state.apply_action_state(body, 'regenerate_unlocked', {'feedback': '更简洁'}, project_id='p1',
                                        pending_resume=None, deps=deps())
        self.assertEqual(body['lock_regeneration']['page_count'], 4)
        self.assertEqual(body['lock_regeneration']['locked_numbers'], [1])
        plan = action_state.plan_action_job(body, 'regenerate_unlocked', {'feedback': '更简洁'}, retry_source=None, retry_scope=None,
            deps=types.SimpleNamespace(reading_queue_action=lambda b, a: None, phase=phase, async_actions=set(),
                                       read_actions=set(), page_retry_scope_key='__page_retry'))
        self.assertEqual(plan.action, '_replan_unlocked')

    def test_regenerate_rejections(self):
        s = generated_deck(4)
        with self.assertRaises(APIError):  # all locked
            lock.validate_regenerate_payload(body_for(ev(s, 'pages.locked', numbers=[1, 2, 3, 4], locked=True)), {}, deps(), phase)
        with self.assertRaises(APIError):  # locked page beyond new count
            lock.validate_regenerate_payload(body_for(ev(s, 'pages.locked', numbers=[4], locked=True)), {'page_count': 3}, deps(), phase)
        with self.assertRaises(APIError) as caught:  # stale view of the locks
            lock.validate_regenerate_payload(body_for(ev(s, 'pages.locked', numbers=[2], locked=True)), {'locked_numbers': [1]}, deps(), phase)
        self.assertEqual(caught.exception.code, 'PAGE_LOCK_CHANGED')
        lock.validate_regenerate_payload(body_for(s), {'page_count': 6}, deps(), phase)

    def test_invariant_blocks_any_action_that_alters_locked_page(self):
        before = ev(generated_deck(), 'pages.locked', numbers=[2], locked=True)
        lock.assert_locks_preserved(before, ev(before, 'page.changed', number=1, change='x'), 'revise_page', deps())
        reset = new_state('p1', 'batch', 'template_and_final')
        with self.assertRaises(APIError) as caught:
            lock.assert_locks_preserved(before, reset, 'update_brief', deps())
        self.assertEqual(caught.exception.code, 'PAGES_LOCKED')
        lock.assert_locks_preserved(before, ev(before, 'pages.locked', numbers=[2], locked=False), 'lock_pages', deps())

    def test_planning_brief_and_merge_preserve_locked_specs(self):
        body = body_for(ev(generated_deck(), 'pages.locked', numbers=[2], locked=True))
        lock.apply_regenerate_request(body, {'feedback': '加入案例', 'page_count': 5})
        brief = lock.planning_brief(body, body['lock_regeneration'])
        self.assertEqual(brief['page_count'], 5)
        self.assertIn('第2页（锁定）：T2', brief['requirements'])
        self.assertIn('加入案例', brief['requirements'])
        self.assertEqual(body['brief']['requirements'], '原需求')
        fresh = [{'number': n, 'title': 'N%d' % n} for n in range(1, 6)]
        merged = lock.merge_specs(body['slide_specs'], fresh, {2})
        self.assertEqual([s['title'] for s in merged], ['N1', 'T2', 'N3', 'N4', 'N5'])


if __name__ == '__main__':
    unittest.main()
