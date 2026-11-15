"""Explicitly saved successful designs; original previews stay owner-private."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
from sqlalchemy.exc import IntegrityError
from models import db, Project
from models.wuyu_design_memory import SavedDesignTemplate
from .wuyu_design_memory import DesignMemoryError, native_complete, dumps


class PersonalTemplates:
    def __init__(self, app):
        self.app = app
        self.root = Path(app.config.get('WUYU_PERSONAL_TEMPLATE_ROOT') or Path(app.instance_path) / 'personal-templates').resolve()

    def _record(self, owner_id, ident):
        row = SavedDesignTemplate.query.filter_by(id=ident.removeprefix('personal:'), owner_id=owner_id).first()
        if not row:
            raise DesignMemoryError('个人模板不存在。', 'NOT_FOUND', 404)
        return row

    def card(self, row):
        body = json.loads(row.body)
        pages = [dict(page, id='personal:'+row.id+':p'+str(page['page_number']),
            image_url=f'/api/template-library/personal:{row.id}/previews/{page["page_number"]}.png') for page in body['pages']]
        return {'id': 'personal:' + row.id, 'title': row.title, 'source_type':'user', 'review_status':'approved',
            'neutralized':False, 'preview_kind':'private_generated_design',
            'preview_disclaimer':'仅自己可见的已完成设计；原页内容仅供自己回看，后续生成只参考其视觉设计。',
            'cover_url': pages[0]['image_url'], 'tags': body['tags'], 'page_count':len(pages),
            'body_reference_count':max(0,len(pages)-1), 'reference_sufficient':True,
            'warnings':[], 'tagging_status':'completed', 'tag_source':'confirmed_design', 'pages':pages,
            'recommendation_reason':'你已确认并收藏的设计，优先推荐。'}

    def list(self, owner_id):
        return [self.card(row) for row in SavedDesignTemplate.query.filter_by(owner_id=owner_id).order_by(SavedDesignTemplate.created_at.desc()).limit(100).all()]

    def save(self, owner_id, project_id, revision, title):
        workflow = self.app.extensions['wuyu_workflow']
        project = db.session.get(Project, project_id)
        if not project or project.user_id != owner_id:
            raise DesignMemoryError('项目不存在。', 'NOT_FOUND', 404)
        current, body = workflow._read(project_id)
        if type(revision) is not int or current != revision:
            raise DesignMemoryError('此设计已更新，请刷新后再保存。', 'REVISION_CONFLICT', 409)
        if not native_complete(body, workflow):
            raise DesignMemoryError('请先完成全部页面的可编辑描摹与导出。', 'NATIVE_DECK_REQUIRED', 409)
        if not isinstance(title,str) or not title.strip() or len(title)>200:
            raise DesignMemoryError('请填写200字以内的模板名称。')
        signature = workflow._hybrid_signature(body)
        old = SavedDesignTemplate.query.filter_by(owner_id=owner_id, source_signature=signature).first()
        if old:
            return self.card(old)
        ident = str(uuid.uuid4())
        destination = self.root / ident
        destination.mkdir(parents=True, mode=0o700)
        try:
            # Include cover and a spread of body designs, up to twelve actual pages.
            all_pages = body['workflow']['pages']
            indexes = sorted({round(i*(len(all_pages)-1)/min(11,len(all_pages)-1)) for i in range(min(12,len(all_pages)))}) if len(all_pages)>1 else [0]
            pages=[]
            for number,index in enumerate(indexes,1):
                page = all_pages[index]
                source = Path(workflow._artifact_file(body,page['artifact']['image_uri']))
                target = destination / f'{number}.png'
                shutil.copyfile(source,target)
                os.chmod(target,0o600)
                spec=body['slide_specs'][index]
                pages.append({'page_number':number,'title':f'设计预览 {number}', 'layout_family':str((spec.get('layout') or {}).get('structure') or 'overview')[:80],
                              'sha256':hashlib.sha256(target.read_bytes()).hexdigest()})
            pptx=Path(workflow._artifact_file(body,body['workflow']['export']['pptx_uri'].removesuffix('.pptx')))
            shutil.copyfile(pptx,destination/'design.pptx')
            os.chmod(destination/'design.pptx',0o600)
            from .wuyu_template_library import _rule_tags
            tags=_rule_tags({'title':body['brief'].get('scenario',''),'tags':{'visual_style':[body['brief'].get('style','')]}})
            row=SavedDesignTemplate(id=ident,owner_id=owner_id,source_signature=signature,title=title.strip(),
                body=dumps({'pages':pages,'tags':tags,'pptx_sha256':hashlib.sha256(pptx.read_bytes()).hexdigest()}))
            db.session.add(row)
            db.session.commit()
            return self.card(row)
        except IntegrityError:
            db.session.rollback()
            shutil.rmtree(destination)
            return self.card(SavedDesignTemplate.query.filter_by(owner_id=owner_id,source_signature=signature).one())
        except Exception:
            db.session.rollback()
            shutil.rmtree(destination)
            raise

    def preview(self, owner_id, ident, number):
        row=self._record(owner_id,ident)
        if number not in {page['page_number'] for page in json.loads(row.body)['pages']}:
            raise DesignMemoryError('预览不存在。','NOT_FOUND',404)
        path=self.root/row.id/f'{number}.png'
        if not path.is_file():
            raise DesignMemoryError('预览暂不可用。','PREVIEW_UNAVAILABLE',503)
        return path

    def attach(self, owner_id, ident, body):
        row=self._record(owner_id,ident)
        existing=next((m for m in body['materials'] if m.get('personal_template_id')==row.id),None)
        if existing:
            return existing
        metadata=json.loads(row.body)
        project_id=body['workflow']['project_id']
        relative=Path(project_id)/'materials'/('personal-'+row.id)
        target=Path(self.app.config['UPLOAD_FOLDER'])/relative
        target.mkdir(parents=True,exist_ok=True)
        images=[]
        for page in metadata['pages']:
            number=page['page_number']
            original=self.preview(owner_id,row.id,number)
            if hashlib.sha256(original.read_bytes()).hexdigest()!=page['sha256']:
                raise DesignMemoryError('个人模板内容已变化。','TEMPLATE_CHANGED',409)
            shutil.copyfile(original,target/f'{number}.png')
            from .wuyu_workflow_service import _now
            artifact_id=str(uuid.uuid4())
            body['artifacts'].append({'id':artifact_id,'kind':'template_image',
                'relative_path':(relative/f'{number}.png').as_posix(), 'filename':f'{number}.png',
                'bytes':original.stat().st_size,'sha256':page['sha256'],'mime_type':'image/png',
                'metadata':{'number':number,'role':'template'},'created_at':_now()})
            images.append({'number':number,'title':page['title'],'layout_family':page['layout_family'],
                           'relative_path':(relative/f'{number}.png').as_posix(),'sha256':page['sha256'],'artifact_id':artifact_id})
        material={'id':str(uuid.uuid4()),'filename':row.title,'personal_template_id':row.id,'role':'template','status':'completed',
                  'sha256':metadata['pptx_sha256'],'template_images':images,'chunks':[], 'warnings':[]}
        body['materials'].append(material)
        return material
