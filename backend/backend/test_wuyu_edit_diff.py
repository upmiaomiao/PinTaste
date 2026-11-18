"""Typed diff of manual PPTX edits and its best-effort capture on save.

Uses real python-pptx files built in memory; no Flask app, database or Document Server.
"""
import importlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

import test_wuyu_office as office_tests  # reuses its lightweight 'services' package setup and helpers

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Emu, Inches, Pt
from PIL import Image

edit_diff = importlib.import_module('services.wuyu_edit_diff')
diff_decks = edit_diff.diff_decks
office = office_tests.office


def image_bytes(color):
    out = io.BytesIO()
    Image.new('RGB', (64, 48), color).save(out, 'PNG')
    out.seek(0)
    return out


def base_deck(pages=2, extra_boxes=0):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    for n in range(1, pages + 1):
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        title = slide.shapes.add_textbox(Inches(1), Inches(0.5), Inches(8), Inches(1))
        title.name = 'Title box'
        title.text_frame.text = 'Title %d' % n
        run = title.text_frame.paragraphs[0].runs[0]
        run.font.size, run.font.name, run.font.color.rgb = Pt(32), 'Arial', RGBColor(0x11, 0x22, 0x33)
        body = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(5), Inches(1))
        body.name = 'Body box'
        body.text_frame.text = 'Body text %d' % n
        body.text_frame.paragraphs[0].runs[0].font.size = Pt(16)
        slide.shapes.add_picture(image_bytes((200, 30, 30)), Inches(8), Inches(2), Inches(3), Inches(2)).name = 'Photo'
        data = CategoryChartData()
        data.categories = ['A', 'B']
        data.add_series('s', (1, 2))
        slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(4), Inches(4), Inches(3), data).name = 'Chart'
        frame = slide.shapes.add_table(2, 2, Inches(7), Inches(5), Inches(4), Inches(1))
        frame.name = 'Table'
        frame.table.cell(0, 0).text = 'head'
        for index in range(extra_boxes):
            extra = slide.shapes.add_textbox(Inches(0.2), Inches(0.1 * index), Inches(1), Inches(0.3))
            extra.name = 'Extra %d' % index
            extra.text_frame.text = 'extra %d' % index
        slide.notes_slide.notes_text_frame.text = 'notes %d' % n
    return save(prs)


def save(prs):
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def edit(data, change):
    prs = Presentation(io.BytesIO(data))
    change(prs)
    return save(prs)


def shape(slide, name):
    return next(item for item in slide.shapes if item.name == name)


def types_of(result, page=1):
    return [c['type'] for entry in result if entry['page'] == page for c in entry['changes']]


def one(result, kind, page=1):
    found = [c for entry in result if entry['page'] == page for c in entry['changes'] if c['type'] == kind]
    assert len(found) == 1, (kind, result)
    return found[0]


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.base = base_deck()

    def diff(self, change, pages=None):
        return diff_decks(self.base, edit(self.base, change), pages)

    def test_unchanged_deck_has_no_changes(self):
        self.assertEqual(diff_decks(self.base, self.base), [])
        self.assertEqual(self.diff(lambda prs: None), [])

    def test_reserialisation_noise_is_not_a_change(self):
        fractional = edit(self.base, lambda prs: setattr(
            shape(prs.slides[0], 'Title box').text_frame.paragraphs[0].runs[0].font, 'size', Pt(32.71)))

        def noise(prs):
            for slide in prs.slides:
                for item in slide.shapes:
                    item.left, item.top = Emu(item.left + 1500), Emu(item.top - 2500)
                    item.width, item.height = Emu(item.width + 3000), Emu(item.height - 900)
            title = shape(prs.slides[0], 'Title box').text_frame.paragraphs[0]
            title.runs[0].font.size = Pt(32.5)          # the editor's 0.5pt step
            title.runs[0].font.bold = False             # inherited -> explicit false
            # the editor splits one run into two with the same formatting
            title.runs[0].text = 'Tit'
            extra = title.add_run()
            extra.text = 'le 1'
            extra.font.size, extra.font.name, extra.font.bold = Pt(32.5), 'Arial', False
            extra.font.color.rgb = RGBColor(0x11, 0x22, 0x33)
        self.assertEqual(diff_decks(fractional, edit(fractional, noise)), [])

    def test_font_changes_with_role_guess(self):
        def change(prs):
            font = shape(prs.slides[0], 'Title box').text_frame.paragraphs[0].runs[0].font
            font.size, font.name, font.bold, font.italic = Pt(40), 'Microsoft YaHei', True, True
            font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
            shape(prs.slides[0], 'Body box').text_frame.paragraphs[0].runs[0].font.size = Pt(18)
        result = self.diff(change)
        self.assertEqual([entry['page'] for entry in result], [1])
        sizes = sorted((c['role'], c['from'], c['to']) for c in result[0]['changes'] if c['type'] == 'font_size')
        self.assertEqual(sizes, [('body', 16.0, 18.0), ('title', 32.0, 40.0)])
        color = one(result, 'font_color')
        self.assertEqual((color['from'], color['to']), ('112233', 'C00000'))
        name = one(result, 'font_name')
        self.assertEqual((name['from'], name['to'], name['script']), ('Arial', 'Microsoft YaHei', 'latin'))
        self.assertEqual((one(result, 'bold')['from'], one(result, 'bold')['to']), (False, True))
        self.assertTrue(one(result, 'italic')['to'])
        self.assertNotIn('text_changed', types_of(result))

    def test_partial_restyle_reports_affected_characters(self):
        def change(prs):
            paragraph = shape(prs.slides[0], 'Body box').text_frame.paragraphs[0]
            paragraph.runs[0].text = 'Body '
            run = paragraph.add_run()
            run.text = 'text 1'
            run.font.size, run.font.bold = Pt(16), True
        bold = one(self.diff(change), 'bold')
        self.assertEqual((bold['to'], bold['chars']), (True, 6))

    def test_text_change_is_truncated_and_counted(self):
        result = self.diff(lambda prs: setattr(shape(prs.slides[1], 'Body box').text_frame, 'text', 'x' * 500))
        change = one(result, 'text_changed', page=2)
        self.assertEqual((change['before'], len(change['after']), change['before_chars'], change['after_chars']),
                         ('Body text 2', 200, 11, 500))

    def test_moved_and_resized(self):
        def change(prs):
            item = shape(prs.slides[0], 'Photo')
            item.left, item.width = Emu(item.left + Inches(1)), Emu(item.width + Inches(0.5))
        result = self.diff(change)
        self.assertEqual(sorted(types_of(result)), ['moved', 'resized'])
        self.assertEqual(one(result, 'moved')['from'], [576.0, 144.0])
        self.assertEqual(one(result, 'moved')['to'], [648.0, 144.0])
        self.assertEqual(one(result, 'resized')['kind'], 'picture')

    def test_shape_added_and_removed(self):
        def change(prs):
            slide = prs.slides[0]
            gone = shape(slide, 'Chart')
            gone._element.getparent().remove(gone._element)
            box = slide.shapes.add_textbox(Inches(10), Inches(6), Inches(2), Inches(1))
            box.text_frame.text = 'new note'
        result = self.diff(change)
        self.assertEqual(one(result, 'shape_removed')['kind'], 'chart')
        added = one(result, 'shape_added')
        self.assertEqual((added['kind'], added['text']), ('text', 'new note'))

    def test_picture_replaced(self):
        def change(prs):
            slide = prs.slides[0]
            old = shape(slide, 'Photo')
            new = slide.shapes.add_picture(image_bytes((10, 200, 10)), old.left, old.top, old.width, old.height)
            new.name = 'Photo'
            old._element.getparent().remove(old._element)   # new id, same name and place
        result = self.diff(change)
        self.assertEqual(types_of(result), ['picture_replaced'])
        self.assertNotEqual(result[0]['changes'][0]['from'], result[0]['changes'][0]['to'])

    def test_chart_type_and_data(self):
        def retype(prs):
            slide = prs.slides[0]
            old = shape(slide, 'Chart')
            data = CategoryChartData()
            data.categories = ['A', 'B']
            data.add_series('s', (1, 2))
            slide.shapes.add_chart(XL_CHART_TYPE.PIE, old.left, old.top, old.width, old.height, data).name = 'Chart'
            old._element.getparent().remove(old._element)
        result = self.diff(retype)
        self.assertEqual(types_of(result), ['chart_type_changed'])
        self.assertEqual((result[0]['changes'][0]['from'], result[0]['changes'][0]['to']), ('COLUMN_CLUSTERED', 'PIE'))

        def redata(prs):
            data = CategoryChartData()
            data.categories = ['A', 'B', 'C']
            data.add_series('s', (1, 5, 9))
            shape(prs.slides[0], 'Chart').chart.replace_data(data)
        change = one(self.diff(redata), 'chart_data_changed')
        self.assertEqual((change['before_points'], change['after_points'], change['categories_changed']), (2, 3, True))

    def test_table_and_notes(self):
        def change(prs):
            shape(prs.slides[0], 'Table').table.cell(1, 1).text = 'value'
            prs.slides[0].notes_slide.notes_text_frame.text = 'spoken words'
        result = self.diff(change)
        table = one(result, 'table_changed')
        self.assertEqual((table['cells_changed'], table['samples']),
                         (1, [{'row': 2, 'col': 2, 'before': '', 'after': 'value'}]))
        notes = one(result, 'notes_changed')
        self.assertEqual((notes['before'], notes['after']), ('notes 1', 'spoken words'))

    def test_matching_survives_renumbered_ids_and_renames(self):
        def change(prs):
            slide = prs.slides[0]
            for offset, item in enumerate(slide.shapes):
                node = item._element.xpath('.//p:cNvPr')[0]
                node.set('id', str(900 + offset))
                node.set('name', 'Renamed %d' % offset)
            slide.shapes[1].text_frame.paragraphs[0].runs[0].font.size = Pt(20)
        result = self.diff(change)
        self.assertEqual(types_of(result), ['font_size'])
        self.assertEqual((result[0]['changes'][0]['from'], result[0]['changes'][0]['to']), (16.0, 20.0))

    def test_group_children_are_compared(self):
        def grouped(prs):
            slide = prs.slides[0]
            group = slide.shapes.add_group_shape()
            group.name = 'Group'
            inner = group.shapes.add_textbox(Inches(2), Inches(6), Inches(2), Inches(0.5))
            inner.name = 'Inner'
            inner.text_frame.text = 'inside'
            inner.text_frame.paragraphs[0].runs[0].font.size = Pt(12)
        base = edit(self.base, grouped)

        def change(prs):
            group = shape(prs.slides[0], 'Group')
            shape(group, 'Inner').text_frame.paragraphs[0].runs[0].font.size = Pt(14)
        result = diff_decks(base, edit(base, change))
        size = one(result, 'font_size')
        self.assertEqual((size['group'], size['shape'], size['to']), ('Group', 'Inner', 14.0))

    def test_pages_filter_and_paths(self):
        def change(prs):
            for slide in prs.slides:
                shape(slide, 'Body box').text_frame.text = 'rewritten'
        after = edit(self.base, change)
        self.assertEqual([e['page'] for e in diff_decks(self.base, after)], [1, 2])
        self.assertEqual([e['page'] for e in diff_decks(self.base, after, pages=[2, 9, 'x'])], [2])
        with tempfile.TemporaryDirectory() as tmp:
            old, new = Path(tmp) / 'old.pptx', Path(tmp) / 'new.pptx'
            old.write_bytes(self.base)
            new.write_bytes(after)
            self.assertEqual([e['page'] for e in diff_decks(old, str(new), pages=[1])], [1])

    def test_structure_change(self):
        def change(prs):
            prs.slides.add_slide(prs.slide_layouts[6])
        self.assertEqual(self.diff(change), [{'page': None, 'changes': [
            {'type': 'structure_changed', 'before_slides': 2, 'after_slides': 3}]}])

    def test_caps(self):
        base = base_deck(pages=edit_diff.MAX_PAGES + 2, extra_boxes=0)

        def every_page(prs):
            for slide in prs.slides:
                slide.notes_slide.notes_text_frame.text = 'changed'
        result = diff_decks(base, edit(base, every_page))
        self.assertEqual(len(result), edit_diff.MAX_PAGES)

        crowded = base_deck(pages=1, extra_boxes=edit_diff.MAX_CHANGES_PER_PAGE + 5)

        def every_box(prs):
            for item in prs.slides[0].shapes:
                if item.name.startswith('Extra'):
                    item.text_frame.text = 'edited ' + item.name
        result = diff_decks(crowded, edit(crowded, every_box))
        self.assertEqual(len(result[0]['changes']), edit_diff.MAX_CHANGES_PER_PAGE)
        self.assertTrue(result[0]['truncated'])

    def test_never_raises(self):
        self.assertEqual(diff_decks(b'not a pptx', self.base), [])
        self.assertEqual(diff_decks(self.base, '/nonexistent/deck.pptx'), [])
        original = edit_diff._info

        def flaky(item):
            if item.name == 'Chart':
                raise RuntimeError('odd shape')
            return original(item)
        edit_diff._info = flaky
        try:
            result = self.diff(lambda prs: setattr(shape(prs.slides[0], 'Body box').text_frame, 'text', 'still seen'))
        finally:
            edit_diff._info = original
        self.assertEqual(types_of(result), ['text_changed'])


class Habits:
    def __init__(self, fail=False, available=True, paused=()):
        self.events, self.fail, self.available, self.paused_owners = [], fail, available, set(paused)

    def paused(self, owner_id):
        return owner_id in self.paused_owners

    def record_event(self, owner_id, project_id, kind, payload, *, context=None, page_number=None,
                     request_key=None, commit=False):
        if self.fail:
            raise RuntimeError('database is down')
        self.events.append({'owner_id': owner_id, 'project_id': project_id, 'kind': kind, 'payload': payload,
                            'page_number': page_number, 'request_key': request_key, 'commit': commit})


class Memory:
    def __init__(self, enabled=True):
        self.enabled = enabled

    def policy(self):
        return {'settings': {'capture_enabled': self.enabled}}


class CaptureTests(unittest.TestCase):
    """save_office_edit records manual edits and never fails because of that step."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = office_tests.deck(self.root / 'export.pptx', 'manual', self.root)
        self.rollbacks = 0
        session = types.SimpleNamespace(
            get=lambda model, ident: types.SimpleNamespace(user_id='owner-1') if ident == 'p1' else None,
            rollback=self.count_rollback)
        models = types.ModuleType('models')
        models.db, models.Project = types.SimpleNamespace(session=session), object()
        self.saved_models = sys.modules.get('models')
        sys.modules['models'] = models

    def tearDown(self):
        if self.saved_models is None:
            sys.modules.pop('models', None)
        else:
            sys.modules['models'] = self.saved_models
        self.tmp.cleanup()

    def count_rollback(self):
        self.rollbacks += 1

    def service(self, **extensions):
        service = office_tests.FakeService(office_tests.exported_body(self.root, self.base), self.root)
        service.app.extensions = extensions
        return service

    def edited(self, number, text):
        prs = Presentation(self.base)
        prs.slides[number - 1].shapes[0].text_frame.text = text
        return save(prs)

    def test_one_event_per_changed_page(self):
        habits = Habits()
        service = self.service(wuyu_design_habits=habits, wuyu_design_memory=Memory())
        result = service.save_office_edit('p1', {'aid': 'e1'}, self.edited(2, 'my words'), user_id='owner-1')
        self.assertEqual(result, {'saved': True, 'current': True, 'artifact_id': result['artifact_id'], 'edited_pages': [2]})
        self.assertEqual(len(habits.events), 1)
        event = habits.events[0]
        self.assertEqual((event['owner_id'], event['project_id'], event['kind'], event['page_number'], event['commit']),
                         ('owner-1', 'p1', 'manual_edit', 2, True))
        self.assertEqual(event['request_key'], 'manual:%s:2' % result['artifact_id'])
        self.assertEqual((event['payload']['base_artifact_id'], event['payload']['artifact_id']), ('e1', result['artifact_id']))
        self.assertEqual([c['type'] for c in event['payload']['changes']], ['text_changed'])
        self.assertEqual(event['payload']['changes'][0]['after'], 'my words')

    def test_only_the_owners_own_edits_are_recorded(self):
        # Finding: an administrator's edits in a user's project were stored as that user's design events.
        habits = Habits()
        service = self.service(wuyu_design_habits=habits, wuyu_design_memory=Memory())
        result = service.save_office_edit('p1', {'aid': 'e1'}, self.edited(2, 'admin words'), user_id='admin-7')
        self.assertEqual((result['saved'], result['edited_pages']), (True, [2]))
        self.assertEqual(service.body['manual_edit']['user_id'], 'admin-7')
        self.assertEqual(habits.events, [])

    def test_nothing_is_recorded_while_the_owner_paused(self):
        habits = Habits(paused={'owner-1'})
        service = self.service(wuyu_design_habits=habits, wuyu_design_memory=Memory())
        original, calls = edit_diff.diff_decks, []
        edit_diff.diff_decks = lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs)
        try:
            self.assertTrue(service.save_office_edit('p1', {'aid': 'e1'}, self.edited(2, 'paused'), user_id='owner-1')['saved'])
        finally:
            edit_diff.diff_decks = original
        self.assertEqual((habits.events, calls), ([], []))  # not even compared

    def test_payload_trim_counts_stored_characters_and_is_marked(self):
        # Finding: the trim measured escaped JSON (6 characters per Chinese character), so Chinese decks lost
        # most changes far below the store's limit, with no marker.
        change = lambda index, size: {'type': 'text_changed', 'shape': 'Box %d' % index, 'before': '改' * size, 'after': '写' * size}
        original = edit_diff.diff_decks
        for size, kept, truncated in [(250, 12, False), (900, 7, True)]:
            habits = Habits()
            service = self.service(wuyu_design_habits=habits)
            edit_diff.diff_decks = lambda *args, **kwargs: [{'page': 2, 'changes': [change(index, size) for index in range(12)]}]
            try:
                service.save_office_edit('p1', {'aid': 'e1'}, self.edited(2, 'x'), user_id='owner-1')
            finally:
                edit_diff.diff_decks = original
            payload = habits.events[0]['payload']
            self.assertEqual((len(payload['changes']), payload.get('truncated', False)), (kept, truncated))
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False)), 15000)
            self.assertEqual([item['shape'] for item in payload['changes']], ['Box %d' % index for index in range(kept)])

    def test_structure_change_is_one_event_without_page(self):
        habits = Habits()
        service = self.service(wuyu_design_habits=habits)
        prs = Presentation(self.base)
        prs.slides.add_slide(prs.slide_layouts[6])
        result = service.save_office_edit('p1', {'aid': 'e1'}, save(prs))
        self.assertIsNone(result['edited_pages'])
        self.assertEqual([(e['page_number'], e['request_key']) for e in habits.events],
                         [(None, 'manual:%s:structure' % result['artifact_id'])])

    def test_capture_respects_policy_and_availability(self):
        for extensions in ({'wuyu_design_habits': Habits(), 'wuyu_design_memory': Memory(enabled=False)},
                           {'wuyu_design_habits': Habits(available=False)}):
            service = self.service(**extensions)
            self.assertTrue(service.save_office_edit('p1', {'aid': 'e1'}, self.edited(1, 'quiet'))['saved'])
            self.assertEqual(extensions['wuyu_design_habits'].events, [])

    def test_unknown_project_owner_records_nothing(self):
        habits = Habits()
        service = self.service(wuyu_design_habits=habits)
        self.assertTrue(service.save_office_edit('p2', {'aid': 'e1'}, self.edited(1, 'orphan project'))['saved'])
        self.assertEqual(habits.events, [])

    def test_capture_failures_never_break_the_save(self):
        class BrokenMemory:
            def policy(self):
                raise RuntimeError('policy table missing')
        original = edit_diff.diff_decks
        cases = [({'wuyu_design_habits': Habits(fail=True)}, None),
                 ({'wuyu_design_habits': Habits(), 'wuyu_design_memory': BrokenMemory()}, None),
                 ({'wuyu_design_habits': Habits()}, lambda *a, **k: (_ for _ in ()).throw(ValueError('diff broke'))),
                 ({}, None)]
        for extensions, broken_diff in cases:
            service = self.service(**extensions)
            edit_diff.diff_decks = broken_diff or original
            try:
                with self.assertLogs(level='WARNING') if extensions else self.assertNoLogs(level='WARNING'):
                    result = service.save_office_edit('p1', {'aid': 'e1'}, self.edited(3, 'kept'), user_id='owner-1')
            finally:
                edit_diff.diff_decks = original
            self.assertEqual((result['saved'], result['current'], result['edited_pages']), (True, True, [3]))
            self.assertEqual(service.body['manual_edit']['edited_pages'], [3])
        self.assertEqual(self.rollbacks, 1)   # only the failed event write rolls the session back


if __name__ == '__main__':
    unittest.main()
