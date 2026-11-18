"""TemplateConfiguration: explicit responsibility and finite call-time ports.

Constructors store ports only; transaction, IO and charging order stay in the
original methods. Components never import or receive the facade.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import re
from .wuyu_workflow_contracts import WorkflowAPIError
from dataclasses import dataclass
from typing import Any, Callable

@dataclass(frozen=True)
class TemplateConfigurationPorts:
    _artifact_file: Callable[..., Any]
    _catalog_path: Callable[..., Any]
    _diagram_preset_field: Callable[..., Any]
    app: Callable[..., Any]
    hook__json: Callable[..., Any]
    hook_current_template_choice: Callable[..., Any]
    hook_effective_visual_settings: Callable[..., Any]



class TemplateConfiguration:
    def __init__(self, ports: TemplateConfigurationPorts):
        self.ports = ports

    def _validate_sample_promotion(self, body):
        """Reuse the two actual page images, bound to this exact accepted draft.

        Legacy samples have no spec digest. Their original event/version bindings,
        recorded content and artifact digest remain required; no image model is
        called during confirmation or migration.
        """
        state = body['workflow']
        for number, role in [(1, 'cover'), (2, 'first_body')]:
            sample = state['template']['samples'].get(role)
            if not sample:
                raise WorkflowAPIError('Both current first-page images are required')
            item = next((a for a in body['artifacts'] if 'artifact:' + a['id'] == sample['image_uri']), None)
            if not item:
                raise WorkflowAPIError('Current first-page image is missing')
            path = Path(self.ports._artifact_file(body, sample['image_uri']))
            metadata = item.get('metadata') or {}
            if (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item.get('sha256')
                    or metadata.get('number', number) != number or metadata.get('role', role) != role
                    or sample.get('template_revision') != state['template']['revision']
                    or sample.get('requirements_revision') != state['requirements']['revision']):
                raise WorkflowAPIError('First-page image or its reviewed version has changed')
            spec = body['slide_specs'][number - 1]
            expected = hashlib.sha256(self.ports.hook__json(spec).encode()).hexdigest()
            if metadata.get('sample_spec_sha256'):
                if metadata['sample_spec_sha256'] != expected:
                    raise WorkflowAPIError('First-page image belongs to a different draft')
            elif sample.get('copy') != (spec.get('copy') or spec.get('title')):
                raise WorkflowAPIError('Legacy first-page image does not match the current draft')
            from PIL import Image
            try:
                with Image.open(path) as image:
                    image.verify()
            except Exception:
                raise WorkflowAPIError('Current first-page image cannot be decoded') from None

    def _validate_recipe(self, recipe, body):
        if set(recipe) - {'style_anchor', 'references'} or not isinstance(recipe.get('references', []), list):
            raise WorkflowAPIError('Invalid reference recipe')
        catalog_path = self.ports._catalog_path()
        catalog = json.loads(catalog_path.read_text(encoding='utf-8-sig')) if catalog_path.is_file() else {}
        allowed = {s['id'] for s in catalog.get('sources', [])} | {p['id'] for s in catalog.get('sources', []) for p in s.get('pages', [])}
        allowed |= {'material:' + m['id'] for m in body['materials'] if m.get('role') == 'template'
                    and m.get('status') == 'completed' and m.get('template_images')}
        allowed |= {'material:' + m['id'] + ':p' + str(image['number'])
                    for m in body['materials'] if m.get('role') == 'template' and m.get('status') == 'completed'
                    for image in m.get('template_images', [])}
        for item in [recipe.get('style_anchor')] + recipe.get('references', []):
            if item is None:
                continue
            ident = item if isinstance(item, str) else item.get('page_id') or item.get('source_id') if isinstance(item, dict) else None
            if ident not in allowed:
                raise WorkflowAPIError('A reference must identify a catalog source or page')

    def _catalog_path(self):
        library = next((self.ports.app().config.get(key) or os.getenv(key) for key in
            ['WUYU_TEMPLATE_LIBRARY_PATH', 'WUYU_TEMPLATE_LIBRARY_ROOT', 'WUYU_TEMPLATE_CATALOG', 'WUYU_LIBRARY_ROOT']
            if self.ports.app().config.get(key) or os.getenv(key)), None)
        path = Path(library or Path(__file__).resolve().parents[4] / 'template_library')
        from services.wuyu_template_library import effective_catalog_path
        return effective_catalog_path(self.ports.app().config,
            path if path.suffix.lower() == '.json' else path / 'catalog.json')

    def _default_recipe(self, body):
        for material in reversed(body['materials']):
            if material.get('role') == 'template' and material.get('status') == 'completed' and material.get('template_images'):
                return {'style_anchor': 'material:' + material['id'], 'references': [],
                        'selection': {'kind': 'uploaded_default', 'title': material['filename']}}
        path = self.ports._catalog_path()
        if not path.is_file():
            raise WorkflowAPIError('No usable built-in template is available; upload a template image first', 'TEMPLATE_REFERENCE_UNAVAILABLE', 503)
        catalog = json.loads(path.read_text(encoding='utf-8-sig'))
        root, candidates, rendered_by_source = path.parent.resolve(), [], {}
        query = ' '.join(str(body['brief'].get(k, '')) for k in ['style', 'scenario', 'subscenario', 'topic', 'goal', 'audience'])
        terms = set(re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]{2,}', query.lower()))
        for source in catalog.get('sources', []):
            text = self.ports.hook__json({'title': source.get('title'), 'tags': source.get('tags'), 'summary': source.get('summary')}).lower()
            score = sum(len(term) for term in terms if term in text)
            if source.get('source_type') == 'local':
                score += 1
            rendered = []
            for page in source.get('pages', []):
                if not page.get('image'):
                    continue
                image = (root / page['image']).resolve()
                if image.is_relative_to(root) and image.is_file():
                    rendered.append(page)
            if rendered:
                candidates.append((score, source, rendered[0]))
                rendered_by_source[source['id']] = rendered
        if not candidates:
            raise WorkflowAPIError('No built-in template has a rendered page image; upload a template image first', 'TEMPLATE_REFERENCE_UNAVAILABLE', 503)
        academic = ['user_library-478d24a90895d946', 'user_library-8a1aa7dd468c5d68',
                    'user_library-78549ac0c9996386', 'user_library-05366c65ec9cd6ba']
        subscenario = str(body['brief'].get('subscenario', '')).lower()
        academic_scenarios = {'thesis_defense', 'master_defense', 'doctoral_defense', 'proposal_defense',
                              'academic_report', 'qualifying_exam', 'degree', 'paper', 'paper_report', 'academic'}
        goal = str(body['brief'].get('goal', '')).lower()
        # A coarse classifier may label a real literature/group report as a
        # technical proposal. Use the stated research task only for this default;
        # precise academic choices, uploaded templates and explicit recipes win.
        # Document filenames and material contents do not determine the task.
        if (subscenario not in academic_scenarios
                and str(body['brief'].get('scenario', '')).lower() == 'research'
                and subscenario not in {'grant', 'award', 'talent', 'business_plan', 'product', 'market', 'bid'}
                and not any(term in goal for term in ['项目申请', '项目申报', '基金申请', '基金申报', '报奖', '奖励申报',
                                                      '企业介绍', '企业汇报', '商业计划', '商业汇报', '客户方案', '投标'])):
            task_markers = {
                'qualifying_exam': ('资格考试', '资格考核', 'qualifying exam'),
                'proposal_defense': ('开题答辩', '开题汇报', '开题报告'),
                'thesis_defense': ('毕业答辩', '论文答辩', '学位答辩', '毕业汇报'),
                'academic_report': ('文献汇报', '论文汇报', '文献分享', '论文分享', '组会', 'journal club'),
            }
            for task in [goal, str(body['brief'].get('topic', '')).lower()]:
                matches = [(task.index(marker), category) for category, markers in task_markers.items()
                           for marker in markers if marker in task]
                if matches:
                    subscenario = min(matches)[1]
                    break
        if subscenario in academic_scenarios:
            first = academic[3] if subscenario == 'proposal_defense' else academic[1] if subscenario == 'qualifying_exam' else (
                academic[2] if subscenario in {'academic_report', 'paper', 'paper_report', 'academic'} else academic[0])
            preferred = [first] + [ident for ident in academic if ident != first]
        else:
            preferred = ['user_library-45bf42376498712c']
        # Prefer real, complete body pools; a lone cover is not a full template.
        usable = {source['id']: (score, source, page) for score, source, page in candidates
                  if len(rendered_by_source[source['id']]) >= 6}
        preferred_ids = [ident for ident in preferred if ident in usable]
        if preferred_ids:
            _, source, page = usable[preferred_ids[0]]
            references = [ident for ident in preferred_ids[1:] if ident in academic]
            reason = 'academic_four_local_families' if subscenario in academic_scenarios else 'blue_white_professional_default'
        else:
            _, source, page = max(candidates, key=lambda item: item[0])
            references, reason = [], 'scenario_and_style_with_available_rendered_page'
        return {'style_anchor': {'source_id': source['id'], 'page_id': page['id']}, 'references': references,
                'selection': {'kind': 'builtin_default', 'source_id': source['id'], 'page_id': page['id'],
                              'title': source.get('title', source['id']), 'reason': reason}}

    def _bind_source_assets(self, specs, body):
        records = {asset['id']: asset for material in body['materials'] if material.get('role') == 'material'
                   and material['id']not in body.get('reading_exclusions',{})
                   for asset in material.get('source_assets', [])
                   if asset['id'] not in body.get('reading_asset_exclusions', {})}
        for spec in specs:
            if any(isinstance(e,dict)and isinstance(e.get('source'),dict)
                   and e['source'].get('material_id')in body.get('reading_exclusions',{}) for e in spec.get('evidence',[])):
                raise WorkflowAPIError('Excluded material evidence cannot be used for this presentation')
            ids = spec.get('source_asset_ids', [])
            if not isinstance(ids, list) or len(ids) > 2 or any(not isinstance(i, str) or i not in records for i in ids) or len(set(ids)) != len(ids):
                raise WorkflowAPIError('Slide figure IDs must refer to this project material records (maximum two per slide)')
            spec['source_assets'] = [deepcopy(records[ident]) for ident in ids]
            views = spec.get('source_asset_views', [])
            if not isinstance(views, list) or len(views) > 2:
                raise WorkflowAPIError('At most two source figure views are allowed per slide')
            clean = []
            for view in views:
                if not isinstance(view, dict) or set(view) - {'asset_id', 'crop'} or view.get('asset_id') not in ids:
                    raise WorkflowAPIError('Figure views must refer to the selected figure IDs')
                crop = view.get('crop')
                if not isinstance(crop, list) or len(crop) != 4 or any(not isinstance(n, (float, int)) or isinstance(n, bool) or not math.isfinite(n) for n in crop):
                    raise WorkflowAPIError('Figure crop must contain four finite pixel coordinates')
                x, y, width, height = crop
                asset = records[view['asset_id']]
                if x < 0 or y < 0 or width < 2 or height < 2 or x + width > asset['width'] or y + height > asset['height']:
                    raise WorkflowAPIError('Figure crop is outside the original image')
                clean.append({'asset_id': view['asset_id'], 'crop': crop})
            spec['source_asset_views'] = clean
        return specs

    def _template_references(self, project_id, body):
        return [{'id': 'material:' + m['id'], 'material_id': m['id'], 'title': m['filename'],
                 'status': m['status'], 'selectable': m.get('status') == 'completed' and bool(m.get('template_images')),
                 'image_url': f"/api/projects/{project_id}/workflow/artifacts/{m['template_images'][0]['artifact_id']}"
                              if m.get('template_images') else None,
                 'warnings': m.get('warnings', [])}
                for m in body['materials'] if m.get('role') == 'template']

    def _pipeline_recipe(self, body, approved_samples=False):
        recipe = deepcopy(body['reference_recipe'])
        recipe['visual_settings'] = self.ports.hook_effective_visual_settings(body, self.ports.hook_current_template_choice(body))
        recipe['presentation_brief']={k:body['brief'][k]for k in ('scenario','subscenario','purpose','creation_kind')if k in body['brief']}
        from services.wuyu_design_memory import current_design_context
        if current_design_context.get() and recipe['visual_settings']['style_mode'] != 'template':
            recipe['design_memory'] = deepcopy(current_design_context.get())
        elif current_design_context.get():
            # A confirmed template owns the look; only content and expression habits pass.
            from services.wuyu_visual_settings import template_safe_design_memory
            kept = template_safe_design_memory(current_design_context.get())
            if kept:
                recipe['design_memory'] = deepcopy(kept)
        recipe['project_id'] = body['workflow']['project_id']
        requested = [recipe.get('style_anchor')] + recipe.get('references', [])
        ids = {r if isinstance(r, str) else r.get('page_id') or r.get('source_id') for r in requested if r}
        refs = []
        root = Path(self.ports.app().config['UPLOAD_FOLDER']).resolve()
        for material in body['materials']:
            ident = 'material:' + material['id']
            image_ids = {ident + ':p' + str(i['number']) for i in material.get('template_images', [])}
            if ident not in ids and not image_ids.intersection(ids):
                continue
            if material.get('role') != 'template' or material.get('status') != 'completed' or not material.get('template_images'):
                raise WorkflowAPIError('The uploaded template has no usable rendered page; inspect its parsing status')
            for index, image in enumerate(material['template_images'][:12]):
                image_id = ident if index == 0 and ident in ids else ident + ':p' + str(image['number'])
                if ident not in ids and image_id not in ids:
                    continue
                path = (root / image['relative_path']).resolve()
                if not path.is_relative_to(root / body['workflow']['project_id']) or not path.is_file():
                    raise WorkflowAPIError('Uploaded template reference is unavailable')
                refs.append({'id': image_id, 'material_id': material['id'], 'role': 'template',
                             'project_id': body['workflow']['project_id'], 'path': str(path), 'sha256': image['sha256'],
                             'source_sha256': material['sha256'],
                             **{key: deepcopy(image[key]) for key in ['title', 'number', 'page_role', 'layout_family'] if key in image}})
                # A selected material root binds this private candidate pool;
                # per-page selection happens in the pipeline. Do not inflate the
                # public external reference list with all uploaded pages.
        recipe['user_template_refs'] = refs
        if approved_samples:
            template = body['workflow']['template']
            if template['confirmed_revision'] != template['revision'] or set(template['samples']) != {'cover', 'first_body'}:
                raise WorkflowAPIError('Confirm the current two samples before producing final pages')
            recipe['approved_sample_refs'] = []
            for kind in ['first_body', 'cover']:
                source = Path(self.ports._artifact_file(body, template['samples'][kind]['image_uri'])).resolve()
                if not source.is_relative_to(root / recipe['project_id']) or not source.is_file():
                    raise WorkflowAPIError('An approved sample source image is unavailable')
                recipe['approved_sample_refs'].append({'id': 'approved_sample:' + kind,
                    'role': 'approved_sample', 'sample_kind': kind, 'project_id': recipe['project_id'],
                    'path': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                    'template_revision': template['revision'], 'samples_revision': template['samples_revision']})
        return recipe

    def _normalize_recipe(self, recipe):
        if not isinstance(recipe, dict) or set(recipe) - {'style_anchor', 'references', 'style_anchor_id', 'source_ids', 'page_refs', 'selection', 'style_anchor_title'}:
            raise WorkflowAPIError('Invalid reference recipe')
        anchor = recipe.get('style_anchor_id', recipe.get('style_anchor'))
        references = [] if 'source_ids' in recipe or 'page_refs' in recipe else recipe.get('references', [])
        sources, pages = recipe.get('source_ids', []), recipe.get('page_refs', [])
        if not all(isinstance(items, list) for items in [references, sources, pages]):
            raise WorkflowAPIError('Reference source IDs and page refs must be lists')
        from services.wuyu_template_context import normalize_template_reference
        path = self.ports._catalog_path()
        catalog = json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
        try:
            anchor = normalize_template_reference(anchor, catalog)
            references = [normalize_template_reference(item, catalog) for item in references]
            sources = [normalize_template_reference(item, catalog) for item in sources]
            pages = [normalize_template_reference({'page_id': item} if isinstance(item, str) else item, catalog) for item in pages]
        except ValueError as error:
            raise WorkflowAPIError(str(error)) from None
        normalized, seen = [], {self.ports.hook__json(anchor)}
        for item in references + sources + [{'page_id': p} if isinstance(p, str) else p for p in pages]:
            key = self.ports.hook__json(item)
            if key not in seen:
                normalized.append(deepcopy(item))
                seen.add(key)
        if len(normalized) + bool(anchor) > 5:
            raise WorkflowAPIError('Choose no more than five style reference images')
        return {'style_anchor': deepcopy(anchor), 'references': normalized}

    def _public_recipe(self, recipe):
        result = deepcopy(recipe)
        anchor = recipe.get('style_anchor')
        result['style_anchor_id'] = (anchor.get('page_id') or anchor.get('source_id')) if isinstance(anchor, dict) else anchor
        if recipe.get('selection'):
            result['style_anchor_title'] = recipe['selection'].get('title')
        result['source_ids'], result['page_refs'] = [], []
        if anchor and not (isinstance(anchor, dict) and anchor.get('page_id')):
            result['source_ids'].append(anchor.get('source_id') if isinstance(anchor, dict) else anchor)
        for item in recipe.get('references', []):
            if (isinstance(item, dict) and item.get('page_id')) or (isinstance(item, str) and ':p' in item):
                result['page_refs'].append(deepcopy(item))
            else:
                result['source_ids'].append(item.get('source_id') if isinstance(item, dict) else item)
        return result

    def _validate_specs(self, specs, count, brief=None):
        if not isinstance(specs, list) or len(specs) != count or not all(isinstance(s, dict) for s in specs):
            raise WorkflowAPIError('Outline must contain exactly the requested number of slides')
        if (brief or {}).get('creation_kind', 'deck') == 'deck' and len(specs) > 1 and specs[1].get('kind') in {'contents', 'toc', 'directory'}:
            raise WorkflowAPIError('The first body sample must be a content page')

    def _patch_specs(self, current, patches):
        if not current:
            raise WorkflowAPIError('Generate or save the draft before applying page patches')
        if not isinstance(patches, list) or not 1 <= len(patches) <= 4:
            raise WorkflowAPIError('page_patches must contain from one to four page changes')
        merged, seen = deepcopy(current), set()
        allowed = {'number', 'title', 'copy', 'points', 'body', 'speaker_notes'}
        for patch in patches:
            if not isinstance(patch, dict) or set(patch) - allowed or 'number' not in patch or len(patch) < 2:
                raise WorkflowAPIError('Page patches may change only title, copy, points, body or speaker_notes')
            number = patch['number']
            if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= len(merged) or number in seen:
                raise WorkflowAPIError('Page patch numbers must be unique existing slide numbers')
            seen.add(number)
            for key, value in patch.items():
                if key == 'number':
                    continue
                if key == 'title':
                    valid = isinstance(value, str) and bool(value.strip()) and len(value) <= 500
                elif key == 'speaker_notes':
                    valid = isinstance(value, str) and len(value) <= 16000
                elif key == 'copy' or key == 'body' and isinstance(value, str):
                    valid = isinstance(value, str) and len(value) <= 30000
                else:
                    valid = isinstance(value, list) and len(value) <= 100 and all(
                        isinstance(text, str) and len(text) <= 5000 for text in value) and sum(map(len, value)) <= 30000
                if not valid:
                    raise WorkflowAPIError('Page patch content must be bounded text or lists of text')
                merged[number - 1][key] = deepcopy(value)
        return merged

    def _normalize_specs(self, specs, trusted=False, brief=None):
        focused = (brief or {}).get('creation_kind', 'deck') != 'deck'
        return [dict({k: v for k, v in spec.items() if k not in {'previous_image_path', 'creation_kind', 'asset_kind', 'role'} and
                      (trusted or k not in {'structured_data', 'source_structured_data', 'source_path', 'source_assets'})},
                     number=i, kind='cover' if i == 1 and not focused else 'content',
                     role='cover' if i == 1 and not focused else 'body',
                     **({'creation_kind': brief['creation_kind'], 'asset_kind': brief.get('asset_kind', 'diagram'),
                         **self.ports._diagram_preset_field(brief)}
                        if focused else {})) for i, spec in enumerate(specs, 1)]

    @staticmethod
    def _diagram_preset_field(brief):
        from services.wuyu_diagram_presets import resolve
        chosen = resolve(brief) if brief.get('creation_kind') == 'asset' else None
        return {'diagram_preset': chosen} if chosen else {}
