import unittest
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
import test_wuyu_relayout as fixture
from services.wuyu_template_library import TemplateLibrary, TemplateLibraryError, VOCABULARY
from services.wuyu_template_import import import_public_templates
from services.wuyu_template_recommendations import rank_templates
from services.wuyu_workflow_service import WorkflowAPIError

class PublicImportTests(unittest.TestCase):
    setUp=fixture.RelayoutTests.setUp
    tearDown=fixture.RelayoutTests.tearDown

    def manifest(self, single=True, ident='figure75-test'):
        assets=self.root/'import';assets.mkdir(exist_ok=True);pages=[];files=[]
        for n in range(1,2 if single else 6):
            p=assets/f'{n}.png';Image.new('RGB',(320,180),(n*20,70,120)).save(p)
            sha=hashlib.sha256(p.read_bytes()).hexdigest();target='original-previews/'+sha+'.png'
            pages.append({'id':ident+':p'+str(n),'page_number':n,'image':target,'title':'self-created reference',
                          'selected':True,'is_body_reference':True,'layout_family':'flow'})
            files.append({'source':p.name,'target':target,'sha256':sha})
        record={'id':ident,'title':'模板1','source_type':'local','neutralized':False,'preview_kind':'original_template_render',
                'preview_disclaimer':'Self-created visual reference, not project evidence','pages':pages,'cover':pages[0]['image'],'warnings':[],
                'tags':{'purpose':['技术方案'],'page_role':['技术路线'],'visual_style':['蓝白'],'information_density':['中'],
                        'layout_structure':['流程图'],'industry_topic':['通用'],'template_category':['技术路线']}}
        if single:record['reference_scope']='single_image'
        original=self.root/'catalog-original';original.mkdir(exist_ok=True);(original/'catalog.json').write_text(json.dumps({'sources':[]}))
        self.app.config.update(WUYU_TEMPLATE_LIBRARY_PATH=str(original),WUYU_TEMPLATE_STORE_ROOT=str(self.root/'public-store'))
        return TemplateLibrary(self.app),{'entries':[{'record':record,'files':files}]},assets

    def test_import_publishes_real_single_image_and_is_idempotent(self):
        lib,m,assets=self.manifest();v=import_public_templates(lib,m,assets);self.assertEqual(v['added'],1)
        self.assertEqual(import_public_templates(lib,m,assets)['reused'],1)
        cards=lib.list(SimpleNamespace(id='owner',role='admin'),{'scope':'public'})
        self.assertEqual(cards['total'],1);self.assertEqual(cards['items'][0]['reference_scope'],'single_image')
        self.assertEqual(cards['items'][0]['tags']['template_category'],['技术路线'])
        self.assertTrue(lib.preview_path('figure75-test',1,SimpleNamespace(id='owner',role='admin')).is_file())
        catalog=json.loads(lib.frozen_catalog().read_text());self.assertEqual(catalog['sources'][0]['reference_scope'],'single_image')

    def test_bad_hash_and_path_never_publish(self):
        lib,m,assets=self.manifest();m['entries'][0]['files'][0]['sha256']='0'*64
        with self.assertRaises(TemplateLibraryError):import_public_templates(lib,m,assets)
        m['entries'][0]['files'][0]['source']='../outside.png'
        with self.assertRaises(TemplateLibraryError):import_public_templates(lib,m,assets)

    def test_collision_does_not_overwrite_approved_template(self):
        lib,m,assets=self.manifest();import_public_templates(lib,m,assets);m['entries'][0]['record']['title']='changed'
        with self.assertRaises(TemplateLibraryError):import_public_templates(lib,m,assets)

    def test_category_ranking_prefers_actual_requested_family(self):
        a={'id':'a','tags':{'purpose':['技术方案'],'template_category':['摘要图']},'body_reference_count':1}
        b={'id':'b','tags':{'purpose':['技术方案'],'template_category':['技术路线']},'body_reference_count':1}
        self.assertEqual(rank_templates([a,b],{'topic':'技术路线图'})[0][0]['id'],'b')

    def test_single_reference_confirms_for_one_page_but_not_a_full_deck(self):
        lib,m,assets=self.manifest();import_public_templates(lib,m,assets)
        self.service.execute_action(self.project,'update_brief',{'creation_kind':'deck','page_count':2})
        with patch.object(self.service,'_require_template_confirmation_ui'):
            with self.assertRaises(WorkflowAPIError)as caught:self.service.execute_action(self.project,'confirm_template_choice',{'source_id':'figure75-test'})
            self.assertEqual(caught.exception.code,'TEMPLATE_SCOPE_MISMATCH')
            self.service.execute_action(self.project,'update_brief',{'creation_kind':'pages','page_count':1})
            snap=self.service.execute_action(self.project,'confirm_template_choice',{'source_id':'figure75-test'})
        self.assertEqual(self.service._read(self.project)[1]['template_choice']['title'],'模板1')

    def test_full_deck_still_needs_five_real_body_references(self):
        lib,m,assets=self.manifest(False);v=import_public_templates(lib,m,assets);self.assertEqual(v['added'],1)
        source=json.loads(lib.frozen_catalog().read_text())['sources'][0]
        self.assertTrue(source['reference_sufficient']);self.assertEqual(source['body_reference_count'],5)

    def test_complete_78_template_import_uses_real_public_assets(self):
        assets=Path('/acceptance-assets')
        if not assets.exists():self.skipTest('Full approved assets are not mounted')
        manifest=json.loads(Path('/acceptance-manifest.json').read_text(encoding='utf-8'))
        lib,_,_=self.manifest()
        result=import_public_templates(lib,manifest,assets)
        self.assertEqual(result['added'],78)
        actor=SimpleNamespace(id='owner',role='admin')
        for category,total in [('工艺流程图',7),('技术路线',22),('摘要图',14),('论文方法图',23),('整套PPT',12)]:
            listing=lib.list(actor,{'scope':'public','template_category':category,'limit':50})
            self.assertEqual(listing['total'],total)
