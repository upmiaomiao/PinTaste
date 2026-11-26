"""op204: the owner's own uploaded PPT becomes their template; representative pages can be removed/restored."""
import io
import json
from types import SimpleNamespace
import unittest

from werkzeug.datastructures import FileStorage
import test_wuyu_relayout as fixture
from services.wuyu_workflow_contracts import WorkflowAPIError
from services.wuyu_template_from_material import use_material_as_template, set_template_pages, template_candidates


class TemplateFromMaterialTests(unittest.TestCase):
    tearDown = fixture.RelayoutTests.tearDown

    def setUp(self):
        fixture.RelayoutTests.setUp(self)
        catalog = self.root / 'catalog.json'
        catalog.write_text('{"schema_version":"1.0","sources":[]}', encoding='utf-8')
        self.app.config.update(WUYU_TEMPLATE_LIBRARY_PATH=str(catalog),
                               WUYU_TEMPLATE_STORE_ROOT=str(self.root / 'template-store'),
                               WUYU_PIPELINE_FACTORY=lambda config: SimpleNamespace(config=config))

    def body(self):
        return self.service._read(self.project)[1]

    def upload(self, name='deck.pptx', data=b'PK fixture deck', role='material'):
        result = self.service.add_materials(self.project, [FileStorage(stream=io.BytesIO(data), filename=name)],
                                            None, None, role=role)
        return result['uploaded_material_ids'][-1]

    def completed_template(self, pages=(1, 3, 5, 8)):
        """A parsed template material with real artifact files for each page."""
        ident = self.upload('tpl.pptx', role='template')

        def complete(body):
            material = next(m for m in body['materials'] if m['id'] == ident)
            material['status'] = 'completed'
            material['template_images'] = [{'number': n, 'artifact_id': 'a%d' % n, 'relative_path': 'x/page-%03d.png' % n,
                                            'sha256': '0' * 64} for n in pages]
        self.service._mutate(self.project, complete)
        return ident

    def item(self, ident):
        return next(m for m in self.body()['materials'] if m['id'] == ident)

    # ---- use as template -------------------------------------------------------------------------
    def test_content_ppt_becomes_selected_template_once(self):
        source = self.upload()
        candidates = {c['material_id']: c for c in template_candidates(self.body())}
        self.assertEqual(candidates[source], {'material_id': source, 'filename': 'deck.pptx', 'template_material_id': None})
        result = use_material_as_template(self.service, self.project, source, None)
        new = result['uploaded_material_ids'][0]
        self.assertFalse(result['template_reused'])
        made = self.item(new)
        self.assertEqual((made['role'], made['source_material_id'], made['filename']), ('template', source, 'deck.pptx'))
        self.assertEqual(made['sha256'], self.item(source)['sha256'])
        self.assertEqual(self.body()['reference_recipe']['style_anchor'], 'material:' + new)
        self.assertEqual({c['material_id']: c for c in template_candidates(self.body())}[source]['template_material_id'], new)
        again = use_material_as_template(self.service, self.project, source, None)
        self.assertTrue(again['template_reused'])
        self.assertEqual(again['uploaded_material_ids'], [new])
        self.assertEqual(sum(m['role'] == 'template' for m in self.body()['materials']), 1)

    def test_only_uploaded_ppt_or_pdf_material(self):
        note = self.upload('notes.txt', b'plain text')
        with self.assertRaises(WorkflowAPIError) as caught:
            use_material_as_template(self.service, self.project, note, None)
        self.assertEqual(caught.exception.code, 'TEMPLATE_SOURCE_INVALID')
        template = self.upload('tpl.pptx', role='template')
        with self.assertRaises(WorkflowAPIError):
            use_material_as_template(self.service, self.project, template, None)
        with self.assertRaises(WorkflowAPIError) as caught:
            use_material_as_template(self.service, self.project, 'missing', None)
        self.assertEqual(caught.exception.status, 404)
        ids = {c['material_id'] for c in template_candidates(self.body())}
        self.assertNotIn(note, ids)
        self.assertNotIn(template, ids)

    def test_missing_source_file_is_reported(self):
        source = self.upload()
        path = self.root / self.item(source)['relative_path']
        if not path.exists():
            from pathlib import Path
            path = Path(self.app.config['UPLOAD_FOLDER']) / self.item(source)['relative_path']
        path.unlink()
        with self.assertRaises(WorkflowAPIError) as caught:
            use_material_as_template(self.service, self.project, source, None)
        self.assertEqual(caught.exception.code, 'TEMPLATE_SOURCE_UNAVAILABLE')

    # ---- representative pages ------------------------------------------------------------------
    def test_remove_and_restore_pages(self):
        ident = self.completed_template()
        set_template_pages(self.service, self.project, ident, remove=[3, 8])
        self.assertEqual([i['number'] for i in self.item(ident)['template_images']], [1, 5])
        self.assertEqual([i['number'] for i in self.item(ident)['template_images_removed']], [3, 8])
        public = next(m for m in self.service.snapshot(self.project)['materials'] if m['id'] == ident)
        self.assertEqual([i['number'] for i in public['template_images']], [1, 5])
        self.assertEqual([i['number'] for i in public['template_images_removed']], [3, 8])
        self.assertTrue(all('image_url' in i and 'relative_path' not in i for i in public['template_images_removed']))
        self.assertNotIn('relative_path', json.dumps(public))
        set_template_pages(self.service, self.project, ident, restore=[8])
        self.assertEqual([i['number'] for i in self.item(ident)['template_images']], [1, 5, 8])
        set_template_pages(self.service, self.project, ident, restore=[3])
        self.assertNotIn('template_images_removed', self.item(ident))

    def test_at_least_two_pages_stay(self):
        ident = self.completed_template()
        with self.assertRaises(WorkflowAPIError) as caught:
            set_template_pages(self.service, self.project, ident, remove=[1, 3, 5])
        self.assertEqual(caught.exception.code, 'TEMPLATE_PAGES_MINIMUM')
        self.assertEqual(len(self.item(ident)['template_images']), 4)       # nothing changed
        single = self.completed_template(pages=(1,))
        with self.assertRaises(WorkflowAPIError):
            set_template_pages(self.service, self.project, single, remove=[1])

    def test_unknown_numbers_are_ignored_and_bad_input_refused(self):
        ident = self.completed_template()
        set_template_pages(self.service, self.project, ident, remove=[99])
        self.assertEqual(len(self.item(ident)['template_images']), 4)
        for bad in (['3'], [True], [1.5]):
            with self.assertRaises(WorkflowAPIError):
                set_template_pages(self.service, self.project, ident, remove=bad)

    def test_pages_need_a_parsed_template(self):
        pending = self.upload('tpl.pptx', role='template')
        with self.assertRaises(WorkflowAPIError) as caught:
            set_template_pages(self.service, self.project, pending, remove=[1])
        self.assertEqual(caught.exception.code, 'TEMPLATE_NOT_READY')
        content = self.upload()
        with self.assertRaises(WorkflowAPIError):
            set_template_pages(self.service, self.project, content, remove=[1])

    # ---- HTTP -----------------------------------------------------------------------------------
    def test_routes_require_the_panel_interaction(self):
        from controllers.wuyu_workflow_controller import wuyu_workflow_bp
        rules = {r.rule for r in self.app.url_map.iter_rules()} if 'wuyu_workflow' in self.app.blueprints else set()
        if not rules:
            self.app.register_blueprint(wuyu_workflow_bp)
            rules = {r.rule for r in self.app.url_map.iter_rules()}
        self.assertIn('/api/projects/<project_id>/workflow/materials/<material_id>/use-as-template', rules)
        self.assertIn('/api/projects/<project_id>/workflow/materials/<material_id>/template-pages', rules)
        with self.app.test_request_context('/', method='POST', json={}):
            from controllers import wuyu_workflow_controller as controller
            response = controller.use_material_as_template(self.project, 'x')
            self.assertEqual(response[1] if isinstance(response, tuple) else response.status_code, 403)
            response = controller.set_template_pages(self.project, 'x')
            self.assertEqual(response[1] if isinstance(response, tuple) else response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
