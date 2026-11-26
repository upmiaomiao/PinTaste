"""op201: Work questionnaire answers (first-run profile) and their use as a weak deck-type default."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from models import db, User
from services import wuyu_user_profile as profiles
from services.wuyu_template_library import TemplateLibrary

ANSWERS = {'status': 'done', 'industries': ['solid_waste', 'research'], 'role': 'student',
           'deck_types': ['proposal_defense', 'academic_report']}


class ProfileFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///' + str(Path(self.temp.name) / 'p.sqlite'),
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        self.service = profiles.init_user_profiles(self.app)

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.context.pop(); self.temp.cleanup()


class NormalizeTests(unittest.TestCase):
    def test_valid_partial_and_empty(self):
        self.assertEqual(profiles.normalize(ANSWERS)['deck_types'], ['proposal_defense', 'academic_report'])
        self.assertEqual(profiles.normalize({'status': 'done', 'role': 'research'})['industries'], [])
        self.assertEqual(profiles.normalize({'status': 'done'})['status'], 'skipped')   # nothing answered
        self.assertEqual(profiles.normalize({'status': 'skipped'})['role'], None)

    def test_refusals(self):
        bad = ({}, [], {'status': 'maybe'}, {'status': 'done', 'industries': ['x']}, {'status': 'done', 'role': 'boss'},
               {'status': 'done', 'deck_types': ['grant', 'grant']}, {'status': 'done', 'deck_types': ['nope']},
               {'status': 'done', 'deck_types': ['grant', 'award', 'training', 'work_report']},
               {'status': 'skipped', 'role': 'research'}, {'status': 'done', 'note': 'free text'},
               {'status': 'done', 'industries': 'research'}, {'status': 'done', 'role': 3})
        for body in bad:
            with self.assertRaises(profiles.UserProfileError, msg=body):
                profiles.normalize(body)

    def test_vocabulary_matches_deck_types(self):
        from services.wuyu_deck_types import CODES
        self.assertEqual(len(CODES), 13)
        profiles.normalize({'status': 'done', 'deck_types': list(CODES[:3])})


class StoreTests(ProfileFixture):
    def test_new_owner_has_no_profile(self):
        self.assertEqual(self.service.get('u1'), {'available': True, 'profile': None})
        self.assertIsNone(self.service.default_deck_type('u1'))

    def test_save_get_overwrite_and_isolation(self):
        saved = self.service.save('u1', ANSWERS)['profile']
        self.assertEqual({k: saved[k] for k in ANSWERS}, ANSWERS)
        self.assertEqual(saved['deck_type_labels'], ['开题答辩', '学术汇报'])
        self.assertTrue(saved['updated_at'].endswith('Z'))
        self.assertEqual(self.service.default_deck_type('u1'), 'proposal_defense')
        self.assertIsNone(self.service.get('u2')['profile'])
        self.service.save('u1', {'status': 'done', 'role': 'research'})
        self.assertEqual(self.service.get('u1')['profile']['deck_types'], [])
        self.assertIsNone(self.service.default_deck_type('u1'))

    def test_skip_is_remembered(self):
        self.assertEqual(self.service.save('u1', {'status': 'skipped'})['profile']['status'], 'skipped')
        self.assertEqual(self.service.get('u1')['profile']['status'], 'skipped')

    def test_invalid_save_writes_nothing(self):
        with self.assertRaises(profiles.UserProfileError):
            self.service.save('u1', {'status': 'done', 'role': 'boss'})
        self.assertIsNone(self.service.get('u1')['profile'])

    def test_corrupt_row_reads_as_unanswered(self):
        from models.wuyu_user_profile import WuyuUserProfile
        db.session.add(WuyuUserProfile(owner_id='u1', value='not json')); db.session.commit()
        self.assertIsNone(self.service.get('u1')['profile'])
        self.assertIsNone(self.service.default_deck_type('u1'))

    def test_account_deletion_removes_profile(self):
        user = User(id='u1', username='u1', password_hash='x')
        db.session.add(user); db.session.commit()
        self.service.save('u1', ANSWERS)
        self.service.save('u2', ANSWERS)
        db.session.delete(user); db.session.commit()
        self.assertIsNone(self.service.get('u1')['profile'])
        self.assertIsNotNone(self.service.get('u2')['profile'])

    def test_unavailable_service(self):
        self.service.available = False
        self.assertEqual(self.service.get('u1'), {'available': False, 'profile': None})
        self.assertIsNone(self.service.default_deck_type('u1'))
        with self.assertRaises(profiles.UserProfileError) as caught:
            self.service.save('u1', ANSWERS)
        self.assertEqual(caught.exception.status, 503)


class RecommendFallbackTests(unittest.TestCase):
    """The questionnaire type is used only when the project's brief names none."""

    def recommend(self, brief, default):
        from contextlib import contextmanager
        records = {i: {'id': i, 'tags': {'purpose': []}, 'pages': [{'selected': True}] * 5, 'review_status': 'approved',
                       'reference_sufficient': True} for i in ('L1', 'L2')}

        @contextmanager
        def state():
            yield {'sources': records}
        memory = types.SimpleNamespace(policy=lambda: {'settings': {'template_by_deck_type': {'grant': ['L2']}}})
        profile = types.SimpleNamespace(default_deck_type=lambda owner: default)
        stub = types.SimpleNamespace(
            app=types.SimpleNamespace(extensions={'wuyu_design_memory': memory, 'wuyu_user_profiles': profile},
                                      config={'UPLOAD_FOLDER': '/tmp'}),
            prepare=lambda: None, _state=state, _card=lambda record, detail=False: {'id': record['id']},
            _choice_history=lambda owner, deck_type: [], _has_template_history=lambda owner: True)
        with patch('services.wuyu_uploaded_templates.UploadedTemplates.keyed', return_value=[]):
            return TemplateLibrary.recommend(stub, types.SimpleNamespace(id='u1'), brief)

    def test_profile_type_fills_an_untyped_brief(self):
        result = self.recommend({'topic': '固废资源化'}, 'grant')
        self.assertEqual(result['deck_type'], 'grant')
        self.assertEqual(result['items'][0], {'id': 'L2', 'recommendation_reason': '推荐用于项目申请的模板'})

    def test_brief_type_wins(self):
        self.assertEqual(self.recommend({'topic': '年终总结'}, 'grant')['deck_type'], 'work_report')

    def test_no_profile_no_type(self):
        self.assertIsNone(self.recommend({'topic': '固废资源化'}, None)['deck_type'])


if __name__ == '__main__':
    unittest.main()
