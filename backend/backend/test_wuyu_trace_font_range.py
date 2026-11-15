"""Synthetic typography boundaries and real PPTX centipoint round trips."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import ast
import hashlib
import json
import math
import re
import tempfile
import unicodedata
import unittest
import zipfile
from copy import deepcopy

from PIL import Image
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches, Pt
from services import wuyu_trace_styles as styles
from services import wuyu_typography as typography
from services import wuyu_text_fit as fit
from services.wuyu_errors import PipelineError

SERVICES = Path(styles.__file__).parent
EVIDENCE = {}


def actual_inspect(source=None):
    path = Path(source) if source is not None else SERVICES/'wuyu_trace_export.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'inspect_pptx')
    from services.wuyu_asset_quality import inspect_presentation_media
    namespace = {'re': re, 'unicodedata': unicodedata, 'hashlib': hashlib, 'Image': Image,
                 'inspect_presentation_media': inspect_presentation_media}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<actual-pptx-inspection>', 'exec'), namespace)
    return namespace['inspect_pptx']


class FontBoundaryTests(unittest.TestCase):
    def test_positive_pt_boundaries_preserved(self):
        for value in [1, 1.01, 5.5, 5.99, 6, 18, 160]:
            with self.subTest(size=value):
                font = {'size': value, 'color': '#123456'}
                before = deepcopy(font)
                styles.validate_font(font)
                self.assertEqual(font, before)
                self.assertEqual(typography._font_points(font), value)

    def test_outside_boundaries_and_non_numbers_still_rejected(self):
        for value in [0, .99, -1, 160.01, 161, True, False, '5.5', None, math.inf, math.nan]:
            with self.subTest(size=value), self.assertRaises(PipelineError):
                styles.validate_font({'size': value})

    def test_px_conversion_uses_same_pt_boundary(self):
        styles.validate_font({'size': 22/3, 'size_unit': 'px'})
        self.assertEqual(styles.points(22/3, 'px'), 5.5)
        self.assertEqual(typography._font_points({'size': 22/3, 'size_unit': 'px'}), 5.5)
        with self.assertRaises(PipelineError):
            styles.validate_font({'size': 1, 'size_unit': 'px'})
        with self.assertRaises(PipelineError):
            styles.validate_font({'size': 5.5, 'size_unit': 'em'})

    def test_other_font_validation_is_unchanged(self):
        for font in [{'size': 5.5, 'family': ''}, {'size': 5.5, 'color': 'red'}, {'size': 5.5, 'opacity': 2},
                     {'size': 5.5, 'bold': 1}, {'size': 5.5, 'italic': 'yes'}, {'size': 5.5, 'underline': None}]:
            with self.subTest(font=font), self.assertRaises(PipelineError):
                styles.validate_font(font)


class FitTests(unittest.TestCase):
    def element(self, size=5.5):
        return {'text': 'Self-created fitting text', 'bounds': [0, 0, 120, 60], 'font': {'size': size, 'color': '#123456'}}

    def test_fitting_small_original_is_not_clamped_or_mutated(self):
        element = self.element()
        before = deepcopy(element)
        with patch.object(fit, 'resolve_font', return_value=('fake', 0, 'FakeFont')), patch.object(fit, 'measure_text', return_value=(80, 20)):
            override, diagnostic = fit.fit_native_text(element)
        self.assertEqual(element, before)
        self.assertEqual(override, before['font'])
        self.assertIsNone(diagnostic)

    def test_small_overflow_rejected_without_increasing_size_or_losing_text(self):
        element, measured = self.element(), []
        def measure(_element, size, _resolved):
            measured.append(size)
            return 1000, 1000
        with patch.object(fit, 'resolve_font', return_value=('fake', 0, 'FakeFont')), patch.object(fit, 'measure_text', side_effect=measure):
            with self.assertRaises(PipelineError) as error:
                fit.fit_native_text(element)
        self.assertEqual(error.exception.code, 'native_text_overflow')
        self.assertEqual(element['font']['size'], 5.5)
        self.assertEqual(measured, [5.5, 5.5])

    def test_larger_fonts_retain_existing_6pt_overflow_floor(self):
        element, measured = self.element(20), []
        def measure(_element, size, _resolved):
            measured.append(size)
            return 10*size, 2*size
        with patch.object(fit, 'resolve_font', return_value=('fake', 0, 'FakeFont')), patch.object(fit, 'measure_text', side_effect=measure):
            override, diagnostic = fit.fit_native_text(element)
        self.assertGreaterEqual(override['size'], 6)
        self.assertLess(override['size'], 20)
        self.assertGreaterEqual(min(measured), 6)
        self.assertEqual(element['font']['size'], 20)
        self.assertEqual(diagnostic['content_unchanged'], True)

    def test_mixed_semantic_role_does_not_enlarge_explicit_5_5pt_to_18(self):
        elements = [{'type': 'text', 'text': 'Self-created same-role specimen',
                     'bounds': [0, 0, 900, 100], 'typography_role': 'body', 'font': {'size': size}}
                    for size in [5.5, 18, 18, 18]]
        original = deepcopy(elements)
        with patch.object(fit, 'fit_native_text', side_effect=lambda element: (element['font'], None)):
            normalized, report = typography.normalize_trace_typography([{'elements': elements}])
        self.assertEqual([element['font']['size'] for element in normalized[0]['elements']], [5.5, 18, 18, 18])
        self.assertEqual(elements, original)
        self.assertEqual(report['groups'][0]['members'], 3)
        EVIDENCE['mixed_role'] = {'source_sizes': [5.5, 18, 18, 18], 'output_sizes': [5.5, 18, 18, 18], 'normal_role_group_members': 3}


class ActualPptxTests(unittest.TestCase):
    def test_real_native_run_font_550_survives_save_readback_and_final_QA(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'self-created-small-font.pptx'
            deck = Presentation()
            slide = deck.slides.add_slide(deck.slide_layouts[6])
            box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
            run = box.text_frame.paragraphs[0].add_run()
            run.text = 'Self-created small-font specimen'
            font = {'size': 5.5, 'family': 'Arial', 'color': '#123456'}
            styles.validate_font(font)
            styles.apply_font(run.font, font)
            typography.normalize_presentation_typography(deck)
            deck.save(path)
            back = Presentation(path)
            self.assertEqual(back.slides[0].shapes[0].text_frame.paragraphs[0].runs[0].font.size.pt, 5.5)
            with zipfile.ZipFile(path) as archive:
                xml = archive.read('ppt/slides/slide1.xml')
            self.assertIn(b'sz="550"', xml)
            result = actual_inspect()(path, 1, editable=True)
            self.assertTrue(result['passed'])
            self.assertTrue(result['checks']['fonts'])
            EVIDENCE['native_font'] = {'requested_pt': 5.5, 'readback_pt': 5.5, 'DrawingML_sz': 550,
                                      'fonts_QA_passed': result['checks']['fonts'], 'structure_QA_passed': result['passed']}

    def test_chart_font_5_5_is_not_replaced_by_default_18(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'self-created-small-chart.pptx'
            deck = Presentation()
            slide = deck.slides.add_slide(deck.slide_layouts[6])
            data = CategoryChartData()
            data.categories = ['Self-created A', 'Self-created B']
            data.add_series('Self-created', [1, 2])
            chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(5), Inches(3), data).chart
            typography.apply_chart_typography(chart, {'size': 5.5})
            typography.normalize_presentation_typography(deck)
            deck.save(path)
            loaded = Presentation(path).slides[0].shapes[0].chart
            nodes = [n for n in loaded._chartSpace.iter() if n.tag.rsplit('}', 1)[-1] in {'defRPr', 'rPr', 'endParaRPr'} and n.get('sz')]
            self.assertTrue(nodes)
            self.assertTrue(all(n.get('sz') == '550' for n in nodes))
            EVIDENCE['chart_font'] = {'DrawingML_sz_values': sorted({n.get('sz') for n in nodes}), 'default_18pt_not_applied': True}

    def test_real_PPTX_below_1pt_or_above_160pt_still_fails_final_QA(self):
        with tempfile.TemporaryDirectory() as folder:
            for size in [.99, 160.01]:
                deck = Presentation()
                slide = deck.slides.add_slide(deck.slide_layouts[6])
                box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
                run = box.text_frame.paragraphs[0].add_run()
                run.text = 'Self-created invalid-font specimen'
                # 0.99pt is outside the OOXML native API range. Inject it only
                # in this synthetic file to verify the product's final reader.
                run.font.size = Pt(5.5)
                run._r.get_or_add_rPr().set('sz', str(round(size*100)))
                path = Path(folder)/f'invalid-{size}.pptx'
                deck.save(path)
                self.assertFalse(actual_inspect()(path, 1, editable=True)['checks']['fonts'])


if __name__ == '__main__':
    unittest.main()
