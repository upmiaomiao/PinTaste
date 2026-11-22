"""op188: PostgreSQL compatibility for quota kinds and NUL-bearing parsed text."""
import os
import unittest

os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')

from models.reference_file import ReferenceFile
from models.wuyu_quota import QuotaLedger, QuotaReservation
from services import wuyu_points_policy


class QuotaKindWidthTest(unittest.TestCase):
    def test_every_policy_kind_fits_both_columns(self):
        kinds = {'text', 'image', 'trace', 'search', 'fetch', 'scholar', 'wechat_search', 'wechat_fetch', 'voice'}
        source = open(wuyu_points_policy.__file__, encoding='utf-8').read()
        for kind in kinds:
            self.assertIn(kind, source + ' text')
        for column in (QuotaReservation.__table__.c.kind, QuotaLedger.__table__.c.kind):
            self.assertGreaterEqual(column.type.length, 32)
            self.assertTrue(all(len(kind) <= column.type.length for kind in kinds))


class ReferenceFileNulTest(unittest.TestCase):
    def test_constructor_and_assignment_strip_nul(self):
        ref = ReferenceFile(filename='a.pdf', file_path='a.pdf', file_size=1, file_type='pdf',
                            markdown_content='a\x00b\x00c', error_message='e\x00rr')
        self.assertEqual(ref.markdown_content, 'abc')
        self.assertEqual(ref.error_message, 'err')
        ref.markdown_content = '中\x00文'
        self.assertEqual(ref.markdown_content, '中文')

    def test_clean_values_and_none_are_untouched(self):
        text = '正常文本\n\t'
        ref = ReferenceFile(filename='a.pdf', file_path='a.pdf', file_size=1, file_type='pdf', markdown_content=text)
        self.assertIs(ref.markdown_content, text)
        ref.markdown_content = None
        self.assertIsNone(ref.markdown_content)


if __name__ == '__main__':
    unittest.main()
