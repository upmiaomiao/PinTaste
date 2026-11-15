"""Cloud editing: tokens, callback checks, slide diff, slide merge and save handling.

Uses real python-pptx files; no Flask app, database or Document Server.
"""
import importlib
import io
import sys
import tempfile
import time
import types
import unittest
from copy import deepcopy
from pathlib import Path

import jwt

if 'services' not in sys.modules:
    package = types.ModuleType('services')
    package.__path__ = [str(Path(__file__).resolve().parent / 'services')]
    sys.modules['services'] = package
if 'services.wuyu_workflow_contracts' not in sys.modules:  # avoid the ORM-heavy real module
    contracts = types.ModuleType('services.wuyu_workflow_contracts')
    counter = iter(range(10 ** 6))
    contracts._id = lambda: 'id%d' % next(counter)
    contracts._now = lambda: '2026-11-13T00:00:00Z'
    sys.modules['services.wuyu_workflow_contracts'] = contracts

office = importlib.import_module('services.wuyu_office')
state_mod = importlib.import_module('services.wuyu_state')
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches, Pt
from PIL import Image

CONFIG = {'WUYU_OFFICE_ENABLED': 'true', 'WUYU_OFFICE_PUBLIC_URL': 'https://test-ai.example.com/office',
          'WUYU_OFFICE_INTERNAL_URL': 'http://wuyu-staging-onlyoffice', 'WUYU_OFFICE_BACKEND_INTERNAL_URL': 'http://wuyu-staging-backend:5000',
          'WUYU_OFFICE_JWT_SECRET': 'j' * 40, 'WUYU_OFFICE_BRIDGE_SECRET': 'b' * 40}


def png(path, color):
    Image.new('RGB', (64, 48), color).save(path)
    return str(path)


def deck(path, label, tmp, pages=3):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for n in range(1, pages + 1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
        box.text_frame.text = f'{label} page {n}'
        box.text_frame.paragraphs[0].runs[0].font.size = Pt(28)
        slide.shapes.add_picture(png(Path(tmp) / f'{label}-{n}.png', (40 * n, 80, 200 if label == 'fresh' else 20)),
                                 Inches(8), Inches(1), Inches(3), Inches(2))
        if n == 2:
            data = CategoryChartData()
            data.categories = ['A', 'B']
            data.add_series(label, (1, 2 if label == 'fresh' else 3))
            slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(3), Inches(5), Inches(3), data)
            table = slide.shapes.add_table(2, 2, Inches(7), Inches(4), Inches(4), Inches(1)).table
            table.cell(0, 0).text = label
        slide.notes_slide.notes_text_frame.text = f'{label} notes {n}'
    prs.save(path)
    return str(path)


class TokenTests(unittest.TestCase):
    def test_bridge_token_audience_and_expiry(self):
        token = office.bridge_token(CONFIG, office.FILE_AUDIENCE, 'p1', 'a1', 'k1')
        self.assertEqual(office.read_bridge_token(CONFIG, token, office.FILE_AUDIENCE)['aid'], 'a1')
        with self.assertRaises(office.OfficeError):
            office.read_bridge_token(CONFIG, token, office.CALLBACK_AUDIENCE)
        expired = jwt.encode({'aud': office.FILE_AUDIENCE, 'exp': int(time.time()) - 5}, 'b' * 40, algorithm='HS256')
        with self.assertRaises(office.OfficeError):
            office.read_bridge_token(CONFIG, expired, office.FILE_AUDIENCE)

    def test_settings_require_distinct_long_secrets(self):
        with self.assertRaises(office.OfficeError):
            office.settings(dict(CONFIG, WUYU_OFFICE_BRIDGE_SECRET='j' * 40))
        self.assertFalse(office.settings({})['enabled'])

    def test_editor_config_is_signed_and_uses_internal_urls(self):
        user = types.SimpleNamespace(id='u1', username='ren')
        result = office.editor_config(CONFIG, 'p1', {'id': 'a1', 'sha256': 'x'}, '报告', user)
        config = result['config']
        self.assertEqual(result['document_server_url'], 'https://test-ai.example.com/office')
        self.assertTrue(config['document']['url'].startswith('http://wuyu-staging-backend:5000/office-bridge/file/'))
        self.assertEqual(config['document']['title'], '报告.pptx')
        signed = jwt.decode(config['token'], 'j' * 40, algorithms=['HS256'])
        self.assertEqual(signed['document']['key'], config['document']['key'])

    def test_callback_signature_and_download_host(self):
        body = {'key': 'k', 'status': 2, 'url': 'https://test-ai.example.com/office/cache/files/x/output.pptx?md5=1'}
        signed = dict(body, token=jwt.encode(body, 'j' * 40, algorithm='HS256'))
        self.assertEqual(office.verify_callback(CONFIG, signed)['status'], 2)
        header = 'Bearer ' + jwt.encode({'payload': body}, 'j' * 40, algorithm='HS256')
        self.assertEqual(office.verify_callback(CONFIG, body, header)['key'], 'k')
        with self.assertRaises(office.OfficeError):
            office.verify_callback(CONFIG, dict(body, token=jwt.encode(body, 'x' * 40, algorithm='HS256')))
        with self.assertRaises(office.OfficeError):
            office.verify_callback(CONFIG, body)
        self.assertEqual(office.internal_download_url(CONFIG, body['url']),
                         'http://wuyu-staging-onlyoffice/cache/files/x/output.pptx?md5=1')
        for bad in ('http://169.254.169.254/latest', 'file:///etc/passwd', 'https://evil.example.com/office/x'):
            with self.assertRaises(office.OfficeError):
                office.internal_download_url(CONFIG, bad)


class SlideTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fingerprints_detect_only_changed_slides(self):
        base = deck(self.dir / 'base.pptx', 'manual', self.dir)
        prs = Presentation(base)
        prs.slides[1].shapes[0].text_frame.text = 'edited title'
        prs.save(self.dir / 'edited.pptx')
        self.assertEqual(office.edited_pages(office.slide_fingerprints(base),
                                             office.slide_fingerprints(str(self.dir / 'edited.pptx'))), [2])
        # Re-saving without changes is not an edit.
        Presentation(base).save(self.dir / 'resaved.pptx')
        self.assertEqual(office.edited_pages(office.slide_fingerprints(base),
                                             office.slide_fingerprints((self.dir / 'resaved.pptx').read_bytes())), [])
        self.assertIsNone(office.edited_pages([1, 2], [1]))

    def test_editor_font_size_rounding_is_not_an_edit(self):
        """ONLYOFFICE rewrites font sizes in 0.5pt steps (32.71 -> 32.5). That alone must not lock a page."""
        from pptx.util import Pt
        base = deck(self.dir / 'base.pptx', 'manual', self.dir)
        prs = Presentation(base)
        for shape in prs.slides[0].shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(32.71)
        prs.save(self.dir / 'precise.pptx')
        rounded = Presentation(self.dir / 'precise.pptx')
        for slide in rounded.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        for run in paragraph.runs:
                            if run.font.size is not None:
                                run.font.size = Pt(round(run.font.size.pt * 2) / 2)
        rounded.save(self.dir / 'rounded.pptx')
        before = office.slide_fingerprints(str(self.dir / 'precise.pptx'))
        after = office.slide_fingerprints(str(self.dir / 'rounded.pptx'))
        self.assertEqual(office.edited_pages(before, after), [])

    def test_real_size_change_and_text_change_are_still_edits(self):
        from pptx.util import Pt
        base = deck(self.dir / 'base.pptx', 'manual', self.dir)
        prs = Presentation(base)
        for shape in prs.slides[0].shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(20)
        prs.save(self.dir / 'size20.pptx')
        bigger = Presentation(self.dir / 'size20.pptx')
        changed_any = False
        for shape in bigger.slides[0].shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(24)
                        changed_any = True
        self.assertTrue(changed_any)
        bigger.save(self.dir / 'size24.pptx')
        self.assertEqual(office.edited_pages(office.slide_fingerprints(str(self.dir / 'size20.pptx')),
                                             office.slide_fingerprints(str(self.dir / 'size24.pptx'))), [1])
        # A 1pt change is a real edit even though it is above the editor's 0.5pt step.
        one = Presentation(self.dir / 'size20.pptx')
        for shape in one.slides[0].shapes:
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(21)
        one.save(self.dir / 'size21.pptx')
        self.assertEqual(office.edited_pages(office.slide_fingerprints(str(self.dir / 'size20.pptx')),
                                             office.slide_fingerprints(str(self.dir / 'size21.pptx'))), [1])

    def test_replace_slides_copies_media_chart_table_and_notes(self):
        manual = deck(self.dir / 'manual.pptx', 'manual', self.dir)
        fresh = deck(self.dir / 'fresh.pptx', 'fresh', self.dir)
        out = office.replace_slides(manual, fresh, [2], self.dir / 'merged.pptx')
        merged, fresh_prs, manual_prs = Presentation(out), Presentation(fresh), Presentation(manual)
        self.assertEqual(len(merged.slides), 3)
        texts = [s.shapes[0].text_frame.text for s in merged.slides]
        self.assertEqual(texts, ['manual page 1', 'fresh page 2', 'manual page 3'])
        pic = lambda prs, i: next(sh for sh in prs.slides[i].shapes if sh.shape_type == 13).image.blob
        self.assertEqual(pic(merged, 1), pic(fresh_prs, 1))
        self.assertEqual(pic(merged, 0), pic(manual_prs, 0))
        chart = next(sh for sh in merged.slides[1].shapes if sh.has_chart).chart
        self.assertEqual(list(chart.series[0].values), [1.0, 2.0])
        self.assertEqual(chart.series[0].name, 'fresh')
        self.assertIsNotNone(chart.part.chart_workbook.xlsx_part)
        table = next(sh for sh in merged.slides[1].shapes if sh.has_table).table
        self.assertEqual(table.cell(0, 0).text, 'fresh')
        self.assertEqual(merged.slides[1].notes_slide.notes_text_frame.text, 'fresh notes 2')
        self.assertEqual(merged.slides[0].notes_slide.notes_text_frame.text, 'manual notes 1')
        # Every relationship ID in the replaced slide resolves.
        rels = merged.slides[1].part.rels
        rid_attrs = [v for el in merged.slides[1]._element.iter() for k, v in el.attrib.items() if k.startswith('{%s}' % office.R_NS)]
        self.assertTrue(rid_attrs and all(r in rels for r in rid_attrs))

    def test_replace_rejects_page_count_mismatch(self):
        manual = deck(self.dir / 'm.pptx', 'manual', self.dir, pages=3)
        fresh = deck(self.dir / 'f.pptx', 'fresh', self.dir, pages=4)
        with self.assertRaises(office.OfficeError):
            office.replace_slides(manual, fresh, [1], self.dir / 'o.pptx')

    def test_merge_manual_edits(self):
        manual = deck(self.dir / 'manual.pptx', 'manual', self.dir)
        fresh = deck(self.dir / 'fresh.pptx', 'fresh', self.dir)
        body = {'artifacts': [{'id': 'm1'}], 'manual_edit': {'artifact_id': 'm1', 'version': 2,
                'image_versions': {'1': 1, '2': 1, '3': 1}}}
        resolve = lambda artifact: manual
        path, report = office.merge_manual_edits(body, resolve, fresh, {'1': 1, '2': 2, '3': 1}, self.dir / 'out.pptx')
        self.assertEqual(report['replaced_pages'], [2])
        self.assertEqual([s.shapes[0].text_frame.text for s in Presentation(path).slides],
                         ['manual page 1', 'fresh page 2', 'manual page 3'])
        path, report = office.merge_manual_edits(body, resolve, fresh, {'1': 2, '2': 2, '3': 2}, self.dir / 'o2.pptx')
        self.assertTrue(report['superseded'])
        self.assertEqual(path, fresh)
        self.assertEqual(office.merge_manual_edits({'artifacts': []}, resolve, fresh, {}, self.dir / 'o3.pptx'), (fresh, None))


class FakeService(office.OfficeMixin):
    def __init__(self, body, root):
        self.body, self.root, self.app = body, Path(root), types.SimpleNamespace(config=CONFIG)

    def _read(self, project_id):
        return 1, deepcopy(self.body)

    def _mutate(self, project_id, operation, **_):
        body = deepcopy(self.body)
        result = operation(body)
        self.body = body
        return result, body

    def _path(self, project_id, section, name):
        path = self.root / section / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _artifact_file(self, body, uri):
        return str(self.root / next(a for a in body['artifacts'] if a['id'] == uri.removeprefix('artifact:'))['relative_path'])

    def _record_artifact(self, project_id, path, kind, job, metadata=None):
        ident = 'a%d' % len(self.body['artifacts'])
        self.body['artifacts'].append({'id': ident, 'kind': kind, 'relative_path': str(Path(path).relative_to(self.root)),
                                       'metadata': metadata or {}})
        return 'artifact:' + ident


def exported_body(root, pptx):
    from test_wuyu_page_lock import generated_deck, ev
    s = generated_deck(3)
    s = ev(s, 'images.confirmed', image_versions={'1': 1, '2': 1, '3': 1})
    s = state_mod.apply_event(s, {'event_id': 'tb', 'type': 'trace.started', 'payload': {'batch_id': 'b1'}})
    for n in (1, 2, 3):
        s = ev(s, 'trace.page_completed', batch_id='b1', page={'number': n, 'artifact_uri': 'artifact:t%d' % n, 'editable_objects': [{'type': 'text'}]})
    s = ev(s, 'trace.completed', batch_id='b1', pages=[])
    versions = {'image_versions': {'1': 1, '2': 1, '3': 1}, 'trace_versions': {'1': 1, '2': 1, '3': 1}}
    s = ev(s, 'qa.started', run_id='q1')
    s = ev(s, 'qa.completed', run_id='q1', passed=True, checks={'page_count': True}, **versions)
    s = ev(s, 'export.started', run_id='x1')
    s = ev(s, 'export.completed', run_id='x1', pptx_uri='artifact:e1.pptx', **versions)
    assert state_mod.phase(s) == 'exported'
    rel = Path(pptx).relative_to(root)
    return {'workflow': s, 'artifacts': [{'id': 'e1', 'kind': 'pptx', 'relative_path': str(rel), 'sha256': 'old'}], 'history': []}


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = deck(self.root / 'export.pptx', 'manual', self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def edited(self, number, text):
        prs = Presentation(self.base)
        prs.slides[number - 1].shapes[0].text_frame.text = text
        out = io.BytesIO()
        prs.save(out)
        return out.getvalue()

    def test_status_requires_editable_export(self):
        service = FakeService(exported_body(self.root, self.base), self.root)
        self.assertTrue(service.office_status('p1')['available'])
        service.body['workflow']['output_preference'] = 'image'
        self.assertEqual(service.office_status('p1')['code'], 'OFFICE_EDITABLE_REQUIRED')

    def test_save_creates_version_and_locks_edited_pages(self):
        service = FakeService(exported_body(self.root, self.base), self.root)
        result = service.save_office_edit('p1', {'aid': 'e1'}, self.edited(2, 'my words'), user_id='u1')
        self.assertTrue(result['current'])
        self.assertEqual(result['edited_pages'], [2])
        manual = service.body['manual_edit']
        self.assertEqual((manual['version'], manual['edited_pages'], manual['export_run_id']), (1, [2], 'x1'))
        page = service.body['workflow']['pages'][1]
        self.assertEqual((page['locked'], page['lock_source']), (True, 'manual_edit'))
        self.assertFalse(service.body['workflow']['pages'][0]['locked'])
        self.assertEqual(state_mod.phase(service.body['workflow']), 'exported')
        # The editor now opens the manual version; a second save builds on it.
        self.assertEqual(service.office_status('p1')['source_kind'], 'manual_edit')
        second_base = manual['artifact_id']
        prs = Presentation(io.BytesIO(self.edited(2, 'my words')))
        prs.slides[2].shapes[0].text_frame.text = 'third'
        data = io.BytesIO(); prs.save(data)
        result = service.save_office_edit('p1', {'aid': second_base}, data.getvalue())
        self.assertEqual(result['edited_pages'], [3])
        self.assertEqual(service.body['manual_edit']['edited_pages'], [2, 3])
        self.assertEqual(service.body['manual_edit']['version'], 2)

    def test_stale_base_is_kept_as_orphan(self):
        service = FakeService(exported_body(self.root, self.base), self.root)
        service.body['workflow']['export']['run_id'] = 'x2'  # a newer export replaced the file being edited
        service.body['workflow']['export']['pptx_uri'] = 'artifact:other.pptx'
        result = service.save_office_edit('p1', {'aid': 'e1'}, self.edited(1, 'late'))
        self.assertFalse(result['current'])
        self.assertNotIn('manual_edit', service.body)
        self.assertEqual(len(service.body['manual_edit_orphans']), 1)

    def test_structure_change_locks_every_page_and_blocks_regeneration(self):
        service = FakeService(exported_body(self.root, self.base), self.root)
        prs = Presentation(self.base)
        prs.slides.add_slide(prs.slide_layouts[6])
        data = io.BytesIO(); prs.save(data)
        result = service.save_office_edit('p1', {'aid': 'e1'}, data.getvalue())
        self.assertIsNone(result['edited_pages'])
        self.assertEqual(state_mod.locked_numbers(service.body['workflow']), [1, 2, 3])
        self.assertTrue(service.body['manual_edit']['structure_changed'])

    def test_unchanged_and_invalid_saves(self):
        service = FakeService(exported_body(self.root, self.base), self.root)
        import hashlib
        data = Path(self.base).read_bytes()
        service.body['artifacts'][0]['sha256'] = hashlib.sha256(data).hexdigest()
        self.assertFalse(service.save_office_edit('p1', {'aid': 'e1'}, data)['saved'])
        with self.assertRaises(office.OfficeError):
            service.save_office_edit('p1', {'aid': 'e1'}, b'not a pptx')



def hybrid_body(root, pptx):
    """Reviewed deck whose full conversion did not finish; a mixed delivery exists."""
    from test_wuyu_page_lock import generated_deck, ev
    s = generated_deck(3)
    s = ev(s, 'images.confirmed', image_versions={'1': 1, '2': 1, '3': 1})
    assert state_mod.phase(s) != 'exported'
    rel = Path(pptx).relative_to(root)
    return {'workflow': s, 'history': [],
            'artifacts': [{'id': 'h1', 'kind': 'hybrid_pptx', 'relative_path': str(rel), 'sha256': 'old'}]}


class HybridService(FakeService):
    hybrid = 'h1'

    def _current_hybrid_export(self, project_id, body):
        return {'id': self.hybrid} if self.hybrid else None


class HybridEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = deck(self.root / 'hybrid.pptx', 'manual', self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def edited(self, source, number, text):
        prs = Presentation(source)
        prs.slides[number - 1].shapes[0].text_frame.text = text
        out = io.BytesIO()
        prs.save(out)
        return out.getvalue()

    def test_status_uses_current_hybrid_when_no_full_export(self):
        service = HybridService(hybrid_body(self.root, self.base), self.root)
        status = service.office_status('p1')
        self.assertTrue(status['available'])
        self.assertEqual((status['source_kind'], status['artifact_id'], status['hybrid']), ('hybrid_export', 'h1', True))
        service.hybrid = None
        self.assertEqual(service.office_status('p1')['code'], 'OFFICE_EDITABLE_REQUIRED')
        service.hybrid = 'h1'
        service.body['workflow']['output_preference'] = 'image'
        self.assertEqual(service.office_status('p1')['code'], 'OFFICE_EDITABLE_REQUIRED')

    def test_full_export_still_wins_over_hybrid(self):
        service = HybridService(exported_body(self.root, self.base), self.root)
        self.assertEqual(service.office_status('p1')['source_kind'], 'editable_export')

    def test_hybrid_save_versions_locks_and_reopens(self):
        service = HybridService(hybrid_body(self.root, self.base), self.root)
        result = service.save_office_edit('p1', {'aid': 'h1'}, self.edited(self.base, 2, 'my words'), user_id='u1')
        self.assertTrue(result['current'])
        manual = service.body['manual_edit']
        self.assertEqual((manual['base'], manual['hybrid_artifact_id'], manual['edited_pages'], manual['export_run_id']),
                         ('hybrid', 'h1', [2], None))
        self.assertEqual(state_mod.locked_numbers(service.body['workflow']), [2])
        status = service.office_status('p1')
        self.assertEqual((status['source_kind'], status['artifact_id'], status['hybrid']), ('manual_edit', manual['artifact_id'], True))
        # Second save on the manual version accumulates edited pages.
        saved = self.root / service.body['artifacts'][-1]['relative_path']
        result = service.save_office_edit('p1', {'aid': manual['artifact_id']}, self.edited(saved, 3, 'third'))
        self.assertTrue(result['current'])
        self.assertEqual(service.body['manual_edit']['edited_pages'], [2, 3])
        self.assertEqual(service.body['manual_edit']['version'], 2)

    def test_hybrid_save_after_newer_delivery_is_orphan(self):
        service = HybridService(hybrid_body(self.root, self.base), self.root)
        service.hybrid = 'h2'  # project changed: a newer mixed delivery replaced the file being edited
        result = service.save_office_edit('p1', {'aid': 'h1'}, self.edited(self.base, 1, 'late'))
        self.assertFalse(result['current'])
        self.assertNotIn('manual_edit', service.body)
        self.assertEqual(len(service.body['manual_edit_orphans']), 1)

    def test_full_export_after_hybrid_edit_keeps_only_edited_pages(self):
        manual_path = deck(self.root / 'm.pptx', 'manual', self.root)
        fresh = deck(self.root / 'f.pptx', 'fresh', self.root)
        body = {'artifacts': [{'id': 'm1', 'relative_path': 'm.pptx'}],
                'manual_edit': {'artifact_id': 'm1', 'base': 'hybrid', 'hybrid_artifact_id': 'h1',
                                'image_versions': {'1': 1, '2': 1, '3': 1}, 'edited_pages': [2], 'version': 1}}
        out, report = office.merge_manual_edits(body, lambda a: str(self.root / a['relative_path']), fresh,
                                                {'1': 1, '2': 1, '3': 1}, self.root / 'out.pptx')
        self.assertEqual((report['replaced_pages'], report['kept_manual_pages']), ([1, 3], [2]))
        texts = [s.shapes[0].text_frame.text for s in Presentation(out).slides]
        self.assertEqual(texts, ['fresh page 1', 'manual page 2', 'fresh page 3'])


if __name__ == '__main__':
    unittest.main()
