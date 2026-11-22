"""Real-font tests for the shared export cache, without importing the app."""
import ast
import hashlib
import io
import logging
import os
from pathlib import Path
import shutil
import tempfile
import types
from typing import Optional
import unittest
from PIL import ImageFont


def backend_root():
    if os.environ.get('WUYU_TEST_BACKEND_ROOT'):
        return Path(os.environ['WUYU_TEST_BACKEND_ROOT'])
    here = Path(__file__).resolve()
    if (here.parent / 'wuyu_font_cache.py').is_file():
        return here.parent
    return here.parents[2] / 'candidate/backend/backend'


ROOT = backend_root()
SOURCE = ROOT / 'wuyu_font_cache.py'
SOURCE_BYTES = SOURCE.read_bytes()
SOURCE_SHA256 = hashlib.sha256(SOURCE_BYTES).hexdigest()
cache = types.ModuleType('actual_font_cache_under_test')
cache.__file__ = str(SOURCE)
exec(compile(SOURCE_BYTES, str(SOURCE), 'exec'), cache.__dict__)


def usable_font():
    candidates = [os.environ.get('WUYU_TEST_FONT_PATH', ''), str(ROOT / 'fonts/NotoSansSC-Regular.ttf'),
                  'C:/Windows/Fonts/arial.ttf',
                  '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    for value in candidates:
        if value and Path(value).is_file():
            return Path(value).resolve()
    try:
        return Path(ImageFont.truetype('DejaVuSans.ttf', 12).path).resolve()
    except OSError as exc:
        raise RuntimeError('A real installed test font is required') from exc


def metrics(font, text):
    return font.getbbox(text), font.getlength(text), font.getmetrics()


class FontCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.font_path = usable_font()
        cls.sample = 'Mixed English 2026: AVWA, 123.5% / symbols!'

    def setUp(self):
        cache.load_font.cache_clear()

    def tearDown(self):
        cache.load_font.cache_clear()

    def test_file_backed_metrics_match_prior_bytes_loader(self):
        data = self.font_path.read_bytes()
        for size in (6, 12, 18, 32, 64, 96):
            with self.subTest(size=size):
                old = ImageFont.truetype(io.BytesIO(data), size=size)
                actual = cache.measurement_font(self.font_path, size)
                self.assertEqual(metrics(old, self.sample), metrics(actual, self.sample))

    def test_same_source_and_size_reuses_face(self):
        a = cache.measurement_font(self.font_path, 18)
        b = cache.measurement_font(str(self.font_path), 18)
        self.assertIs(a, b)

    def test_large_variety_of_sizes_has_bounded_retention(self):
        for size in range(6, 70):
            cache.measurement_font(self.font_path, size)
        self.assertEqual(cache.load_font.cache_info().currsize, 16)

    def test_eviction_never_invalidates_a_callers_active_reference(self):
        held = cache.measurement_font(self.font_path, 12)
        expected = metrics(held, self.sample)
        for size in range(20, 45):
            cache.measurement_font(self.font_path, size)
        replacement = cache.measurement_font(self.font_path, 12)
        self.assertIsNot(held, replacement)
        self.assertEqual(metrics(held, self.sample), expected)
        self.assertEqual(metrics(replacement, self.sample), expected)

    def test_changed_source_timestamp_invalidates_cached_face(self):
        with tempfile.TemporaryDirectory(prefix='wuyu-font-source-') as directory:
            local = Path(directory) / 'fixture.ttf'
            shutil.copyfile(self.font_path, local)
            first = cache.measurement_font(local, 18)
            before = local.stat()
            os.utime(local, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
            second = cache.measurement_font(local, 18)
            self.assertIsNot(first, second)
            self.assertEqual(metrics(first, self.sample), metrics(second, self.sample))
            del first, second
            cache.load_font.cache_clear()

    def test_cache_clear_releases_ownership_without_breaking_active_face(self):
        held = cache.measurement_font(self.font_path, 18)
        expected = metrics(held, self.sample)
        cache.load_font.cache_clear()
        self.assertEqual(cache.load_font.cache_info().currsize, 0)
        self.assertEqual(metrics(held, self.sample), expected)

    def test_missing_font_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory(prefix='wuyu-font-missing-') as directory:
            with self.assertRaises(FileNotFoundError):
                cache.measurement_font(Path(directory) / 'absent.ttf', 18)

    def test_text_fit_uses_unchanged_point_to_pixel_scale_and_metrics(self):
        source = ROOT / 'services/wuyu_text_fit.py'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_font')
        namespace = {'Path': Path, '_SCALE': 4, '_load_font': cache.load_font}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        actual = namespace['_font']((str(self.font_path), 0, 'fixture'), 18)
        old = ImageFont.truetype(io.BytesIO(self.font_path.read_bytes()), size=96, index=0)
        self.assertEqual(actual.size, 96)
        self.assertEqual(metrics(actual, self.sample), metrics(old, self.sample))

    def test_builder_uses_real_shared_loader_and_preserves_integer_pixels(self):
        builder = self.actual_builder()
        for size in (5.5, 6, 12.5, 18.5, 32.25, 64):
            with self.subTest(size=size):
                actual = builder._get_font(size)
                reference = ImageFont.truetype(str(self.font_path), int(size))
                self.assertEqual(metrics(actual, self.sample), metrics(reference, self.sample))
                self.assertIs(actual, cache.measurement_font(self.font_path, int(size)))

    def test_builder_missing_font_keeps_existing_none_contract(self):
        builder = self.actual_builder()
        with tempfile.TemporaryDirectory(prefix='wuyu-builder-font-') as directory:
            builder.FONT_PATH = str(Path(directory) / 'absent.ttf')
            with self.assertLogs('font-consumer-test', logging.WARNING):
                self.assertIsNone(builder._get_font(18))

    def actual_builder(self):
        source = ROOT / 'utils/pptx_builder.py'
        tree = ast.parse(source.read_text(encoding='utf-8-sig'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'PPTXBuilder')
        function = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_get_font')
        namespace = {'Optional': Optional, 'ImageFont': ImageFont,
                     'measurement_font': cache.measurement_font, 'logger': logging.getLogger('font-consumer-test')}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), 'exec'), namespace)
        return type('ActualBuilderMethod', (), {'FONT_PATH': str(self.font_path), '_get_font': namespace['_get_font']})


if __name__ == '__main__':
    unittest.main()
