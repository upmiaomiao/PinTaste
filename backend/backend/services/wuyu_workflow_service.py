"""Workflow composition root and backwards-compatible API facade.

Business implementations belong to responsibility components. Existing reading,
research, template-review and relayout extensions keep their compatibility ports.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import mimetypes
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid
from sqlalchemy import update, inspect
from sqlalchemy.exc import IntegrityError
from werkzeug.utils import secure_filename
from models import db, Project, Page, Task, ReferenceFile
from models.wuyu_workflow import WuyuWorkflow, WuyuWorkflowJob
from services.wuyu_state import apply_event, new_state, phase, image_export_ready, WorkflowError
from services.wuyu_errors import PipelineError, USER_RETRYABLE_TRACE_CODES
from services.wuyu_workflow_quota import WorkflowCharge, current_workflow_actor, current_workflow_charge, initiating_user, recover_workflow_charges
from services.wuyu_quota import QuotaError, get_quota_service
from services.wuyu_reading_workflow import ReadingWorkflowMixin, READ_ACTIONS
from services.wuyu_template_review import TemplateReviewMixin, public_template_choice, current_template_choice
from services.wuyu_visual_settings import effective_visual_settings
from services.wuyu_research_workflow import ResearchWorkflowMixin
from services.wuyu_deep_research import RESEARCH_ACTIONS, research_public
from workflow_execution import execution_mode, owner_lock_held
from . import wuyu_workflow_lifecycle as lifecycle
from . import wuyu_page_retry as page_retry
from . import wuyu_action_state as action_state
from .wuyu_workflow_errors import failure_message as _failure_message, material_error_code, material_parse_error_code
from .wuyu_material_management import MaterialManagementMixin, capacity as material_capacity, usage as material_usage
from .wuyu_relayout import RelayoutMixin, current_source as current_relayout_source
from .wuyu_page_lock import PageLockMixin
from .wuyu_office import OfficeMixin
from functools import wraps, cached_property
from .wuyu_workflow_contracts import CancelledWork, DESIGN_KEYS, WorkflowAPIError, _UNCAPTURED_CATALOG, _design_requirements, _id, _json, _now
from .wuyu_workflow_reports import _failure_diagnostics, _failure_site, _generation_report, _material_coverage, _model_report, _public_runtime, _public_source, _quality_report, _report_text, _safe_failure_code
from .wuyu_workflow_project import ProjectState, ProjectStatePorts
from .wuyu_workflow_views import WorkflowViews, WorkflowViewsPorts
from .wuyu_workflow_actions import ActionCoordinator, ActionCoordinatorPorts
from .wuyu_workflow_templates import TemplateConfiguration, TemplateConfigurationPorts
from .wuyu_workflow_jobs import JobExecution, JobExecutionPorts
from .wuyu_workflow_models import ModelExecution, ModelExecutionPorts
from .wuyu_workflow_generation import PageGeneration, PageGenerationPorts
from .wuyu_workflow_artifacts import ArtifactDelivery, ArtifactDeliveryPorts
from .wuyu_workflow_materials import MaterialProcessing, MaterialProcessingPorts

_pid_alive = lifecycle.pid_alive


def _delegate(component, implementation):
    """Preserve method names/signatures while forwarding to one owner."""
    @wraps(implementation)
    def dispatch(self, *args, **kwargs):
        return getattr(self._components[component], implementation.__name__)(*args, **kwargs)
    return dispatch


def _late_method(service, name):
    def invoke(*args, **kwargs):
        return getattr(service, name)(*args, **kwargs)
    return invoke


def _late_resource(service, name):
    return lambda: getattr(service, name)


def _compose(service):
    # Late callbacks preserve instance overrides without giving components the service.
    return {
        'project': ProjectState(ProjectStatePorts(
            _close_inflight=_late_method(service, '_close_inflight'),
            _default=_late_method(service, '_default'),
            _ensure=_late_method(service, '_ensure'),
            _mutate=_late_method(service, '_mutate'),
            _new_state=_late_method(service, '_new_state'),
            _read=_late_method(service, '_read'),
            _reading_protocol_enabled=_late_method(service, '_reading_protocol_enabled'),
            _require_current_reading_replay=_late_method(service, '_require_current_reading_replay'),
            hook__id=lambda *args, **kwargs: _id(*args, **kwargs),
            hook__json=lambda *args, **kwargs: _json(*args, **kwargs),
            hook_apply_event=lambda *args, **kwargs: apply_event(*args, **kwargs),
            hook_new_state=lambda *args, **kwargs: new_state(*args, **kwargs),
        )),
        'views': WorkflowViews(WorkflowViewsPorts(
            _active_runtime_config=_late_method(service, '_active_runtime_config'),
            _current_hybrid_export=_late_method(service, '_current_hybrid_export'),
            _hybrid_signature=_late_method(service, '_hybrid_signature'),
            _image_signature=_late_method(service, '_image_signature'),
            _job_dict=_late_method(service, '_job_dict'),
            _pipeline=_late_method(service, '_pipeline'),
            _public_artifact=_late_method(service, '_public_artifact'),
            _public_material=_late_method(service, '_public_material'),
            _public_recipe=_late_method(service, '_public_recipe'),
            _read=_late_method(service, '_read'),
            _reading_public=_late_method(service, '_reading_public'),
            _template_references=_late_method(service, '_template_references'),
            app=_late_resource(service, 'app'),
            hook__failure_diagnostics=lambda *args, **kwargs: _failure_diagnostics(*args, **kwargs),
            hook__failure_message=lambda *args, **kwargs: _failure_message(*args, **kwargs),
            hook__public_runtime=lambda *args, **kwargs: _public_runtime(*args, **kwargs),
            hook__public_source=lambda *args, **kwargs: _public_source(*args, **kwargs),
            hook__safe_failure_code=lambda *args, **kwargs: _safe_failure_code(*args, **kwargs),
            hook_current_relayout_source=lambda *args, **kwargs: current_relayout_source(*args, **kwargs),
            hook_image_export_ready=lambda *args, **kwargs: image_export_ready(*args, **kwargs),
            hook_material_usage=lambda *args, **kwargs: material_usage(*args, **kwargs),
            hook_phase=lambda *args, **kwargs: phase(*args, **kwargs),
            hook_public_template_choice=lambda *args, **kwargs: public_template_choice(*args, **kwargs),
            hook_research_public=lambda *args, **kwargs: research_public(*args, **kwargs),
            material_limits=_late_method(service, 'material_limits'),
            native_complete=lambda predicate, body: predicate(body, service),
            provider_status=_late_method(service, 'provider_status'),
            queue_snapshot=_late_method(service, 'queue_snapshot'),
        )),
        'actions': ActionCoordinator(ActionCoordinatorPorts(
            ACTIONS=_late_resource(service, 'ACTIONS'),
            ASYNC_ACTIONS=_late_resource(service, 'ASYNC_ACTIONS'),
            EXPORT_FORMATS=_late_resource(service, 'EXPORT_FORMATS'),
            _action_plan_dependencies=_late_method(service, '_action_plan_dependencies'),
            _action_state_dependencies=_late_method(service, '_action_state_dependencies'),
            _artifact_file=_late_method(service, '_artifact_file'),
            _cancel_old_jobs=_late_method(service, '_cancel_old_jobs'),
            _close_inflight=_late_method(service, '_close_inflight'),
            _default_recipe=_late_method(service, '_default_recipe'),
            _job_dict=_late_method(service, '_job_dict'),
            _material_management_guard=_late_method(service, '_material_management_guard'),
            _mutate=_late_method(service, '_mutate'),
            _new_job_record=_late_method(service, '_new_job_record'),
            _new_state=_late_method(service, '_new_state'),
            _normalize_recipe=_late_method(service, '_normalize_recipe'),
            _read=_late_method(service, '_read'),
            _reading_apply_action=_late_method(service, '_reading_apply_action'),
            _reading_invalidate=_late_method(service, '_reading_invalidate'),
            _reading_queue_action=_late_method(service, '_reading_queue_action'),
            _reading_replan=_late_method(service, '_reading_replan'),
            _reading_validate_action=_late_method(service, '_reading_validate_action'),
            _relayout_validate_action=_late_method(service, '_relayout_validate_action'),
            _require_current_reading_replay=_late_method(service, '_require_current_reading_replay'),
            _require_native_rebuild=_late_method(service, '_require_native_rebuild'),
            _require_template_choice=_late_method(service, '_require_template_choice'),
            _require_template_confirmation_ui=_late_method(service, '_require_template_confirmation_ui'),
            _research_apply=_late_method(service, '_research_apply'),
            _research_ui=_late_method(service, '_research_ui'),
            _research_validate=_late_method(service, '_research_validate'),
            _restore_template_choice=_late_method(service, '_restore_template_choice'),
            _start_armed_research=_late_method(service, '_start_armed_research'),
            _sync_page=_late_method(service, '_sync_page'),
            _validate_page_revision_payload=_late_method(service, '_validate_page_revision_payload'),
            app=_late_resource(service, 'app'),
            hook__failure_message=lambda *args, **kwargs: _failure_message(*args, **kwargs),
            hook__id=lambda *args, **kwargs: _id(*args, **kwargs),
            hook__json=lambda *args, **kwargs: _json(*args, **kwargs),
            hook__now=lambda *args, **kwargs: _now(*args, **kwargs),
            hook_apply_event=lambda *args, **kwargs: apply_event(*args, **kwargs),
            hook_current_relayout_source=lambda *args, **kwargs: current_relayout_source(*args, **kwargs),
            hook_current_template_choice=lambda *args, **kwargs: current_template_choice(*args, **kwargs),
            hook_get_quota_service=lambda *args, **kwargs: get_quota_service(*args, **kwargs),
            hook_initiating_user=lambda *args, **kwargs: initiating_user(*args, **kwargs),
            hook_phase=lambda *args, **kwargs: phase(*args, **kwargs),
            run_pending=_late_method(service, 'run_pending'),
            snapshot=_late_method(service, 'snapshot'),
        )),
        'templates': TemplateConfiguration(TemplateConfigurationPorts(
            _artifact_file=_late_method(service, '_artifact_file'),
            _catalog_path=_late_method(service, '_catalog_path'),
            _diagram_preset_field=_late_method(service, '_diagram_preset_field'),
            app=_late_resource(service, 'app'),
            hook__json=lambda *args, **kwargs: _json(*args, **kwargs),
            hook_current_template_choice=lambda *args, **kwargs: current_template_choice(*args, **kwargs),
            hook_effective_visual_settings=lambda *args, **kwargs: effective_visual_settings(*args, **kwargs),
        )),
        'jobs': JobExecution(JobExecutionPorts(
            _check_cancel=_late_method(service, '_check_cancel'),
            _continue=_late_method(service, '_continue'),
            _export_additional=_late_method(service, '_export_additional'),
            _generate_batch=_late_method(service, '_generate_batch'),
            _generate_focused=_late_method(service, '_generate_focused'),
            _generate_next=_late_method(service, '_generate_next'),
            _generate_review_images=_late_method(service, '_generate_review_images'),
            _heartbeat=_late_method(service, '_heartbeat'),
            _job_catalog_binding=_late_method(service, '_job_catalog_binding'),
            _material_management_guard=_late_method(service, '_material_management_guard'),
            _mutate=_late_method(service, '_mutate'),
            _new_job_record=_late_method(service, '_new_job_record'),
            _outline=_late_method(service, '_outline'),
            _parse_materials=_late_method(service, '_parse_materials'),
            _pipeline=_late_method(service, '_pipeline'),
            _prepare_relayout=_late_method(service, '_prepare_relayout'),
            _replan_unlocked=_late_method(service, '_replan_unlocked'),
            _read=_late_method(service, '_read'),
            _reading_failed=_late_method(service, '_reading_failed'),
            _reading_run=_late_method(service, '_reading_run'),
            _reading_validate_job=_late_method(service, '_reading_validate_job'),
            _relayout_validate_action=_late_method(service, '_relayout_validate_action'),
            _research_validate=_late_method(service, '_research_validate'),
            _reset_inflight=_late_method(service, '_reset_inflight'),
            _run_research=_late_method(service, '_run_research'),
            _samples=_late_method(service, '_samples'),
            _trace_and_export=_late_method(service, '_trace_and_export'),
            _terminal_owner=_late_resource(service, 'terminal_owner'),
            _versions=_late_method(service, '_versions'),
            app=_late_resource(service, 'app'),
            get_job=_late_method(service, 'get_job'),
            hook__failure_diagnostics=lambda *args, **kwargs: _failure_diagnostics(*args, **kwargs),
            hook__failure_message=lambda *args, **kwargs: _failure_message(*args, **kwargs),
            hook__failure_site=lambda *args, **kwargs: _failure_site(*args, **kwargs),
            hook__id=lambda *args, **kwargs: _id(*args, **kwargs),
            hook__json=lambda *args, **kwargs: _json(*args, **kwargs),
            hook__safe_failure_code=lambda *args, **kwargs: _safe_failure_code(*args, **kwargs),
            hook_get_quota_service=lambda *args, **kwargs: get_quota_service(*args, **kwargs),
            hook_initiating_user=lambda *args, **kwargs: initiating_user(*args, **kwargs),
            hook_phase=lambda *args, **kwargs: phase(*args, **kwargs),
            hook_public_template_choice=lambda *args, **kwargs: public_template_choice(*args, **kwargs),
            hook_recover_workflow_charges=lambda *args, **kwargs: recover_workflow_charges(*args, **kwargs),
            run_pending=_late_method(service, 'run_pending'),
            snapshot=_late_method(service, 'snapshot'),
            stop_event=_late_resource(service, 'stop_event'),
        )),
        'models': ModelExecution(ModelExecutionPorts(
            _catalog_path=_late_method(service, '_catalog_path'),
            _check_cancel=_late_method(service, '_check_cancel'),
            _pipeline=_late_method(service, '_pipeline'),
            _record_batch_timing=_late_method(service, '_record_batch_timing'),
            app=_late_resource(service, 'app'),
            hook_ThreadPoolExecutor=lambda *args, **kwargs: ThreadPoolExecutor(*args, **kwargs),
            hook_WorkflowCharge=lambda *args, **kwargs: WorkflowCharge(*args, **kwargs),
            hook__failure_message=lambda *args, **kwargs: _failure_message(*args, **kwargs),
            hook__json=lambda *args, **kwargs: _json(*args, **kwargs),
            hook__quality_report=lambda *args, **kwargs: _quality_report(*args, **kwargs),
            hook__safe_failure_code=lambda *args, **kwargs: _safe_failure_code(*args, **kwargs),
            hook_wait=lambda *args, **kwargs: wait(*args, **kwargs),
        )),
        'generation': PageGeneration(PageGenerationPorts(
            _artifact_file=_late_method(service, '_artifact_file'),
            _bind_source_assets=_late_method(service, '_bind_source_assets'),
            _check_cancel=_late_method(service, '_check_cancel'),
            _continue=_late_method(service, '_continue'),
            _deliver_focused_images=_late_method(service, '_deliver_focused_images'),
            _event=_late_method(service, '_event'),
            _export_additional=_late_method(service, '_export_additional'),
            _generate_batch=_late_method(service, '_generate_batch'),
            _generate_next=_late_method(service, '_generate_next'),
            _image_signature=_late_method(service, '_image_signature'),
            _model_batch=_late_method(service, '_model_batch'),
            _model_workers=_late_method(service, '_model_workers'),
            _mutate=_late_method(service, '_mutate'),
            _normalize_specs=_late_method(service, '_normalize_specs'),
            _outline=_late_method(service, '_outline'),
            _outline_reserved=_late_method(service, '_outline_reserved'),
            _path=_late_method(service, '_path'),
            _pipeline_recipe=_late_method(service, '_pipeline_recipe'),
            _plan_outline=_late_method(service, '_plan_outline'),
            _progress=_late_method(service, '_progress'),
            _read=_late_method(service, '_read'),
            _record_artifact=_late_method(service, '_record_artifact'),
            _relayout_seed_unchanged=_late_method(service, '_relayout_seed_unchanged'),
            _require_template_choice=_late_method(service, '_require_template_choice'),
            _sync_page=_late_method(service, '_sync_page'),
            _trace_and_export=_late_method(service, '_trace_and_export'),
            _validate_specs=_late_method(service, '_validate_specs'),
            app=_late_resource(service, 'app'),
            hook_WorkflowCharge=lambda *args, **kwargs: WorkflowCharge(*args, **kwargs),
            hook__design_requirements=lambda *args, **kwargs: _design_requirements(*args, **kwargs),
            hook__generation_report=lambda *args, **kwargs: _generation_report(*args, **kwargs),
            hook__id=lambda *args, **kwargs: _id(*args, **kwargs),
            hook__json=lambda *args, **kwargs: _json(*args, **kwargs),
            hook__material_coverage=lambda *args, **kwargs: _material_coverage(*args, **kwargs),
            hook__model_report=lambda *args, **kwargs: _model_report(*args, **kwargs),
            hook__public_source=lambda *args, **kwargs: _public_source(*args, **kwargs),
            hook_apply_event=lambda *args, **kwargs: apply_event(*args, **kwargs),
            hook_phase=lambda *args, **kwargs: phase(*args, **kwargs),
        )),
        'artifacts': ArtifactDelivery(ArtifactDeliveryPorts(
            EXPORT_FORMATS=_late_resource(service, 'EXPORT_FORMATS'),
            _artifact_file=_late_method(service, '_artifact_file'),
            _check_cancel=_late_method(service, '_check_cancel'),
            _current_hybrid_export=_late_method(service, '_current_hybrid_export'),
            _ensure_image_export=_late_method(service, '_ensure_image_export'),
            _event=_late_method(service, '_event'),
            _export_additional=_late_method(service, '_export_additional'),
            _export_provider=_late_method(service, '_export_provider'),
            _hybrid_signature=_late_method(service, '_hybrid_signature'),
            _image_signature=_late_method(service, '_image_signature'),
            _model_batch=_late_method(service, '_model_batch'),
            _model_workers=_late_method(service, '_model_workers'),
            _mutate=_late_method(service, '_mutate'),
            _path=_late_method(service, '_path'),
            _progress=_late_method(service, '_progress'),
            _public_artifact=_late_method(service, '_public_artifact'),
            _public_recipe=_late_method(service, '_public_recipe'),
            _read=_late_method(service, '_read'),
            _record_artifact=_late_method(service, '_record_artifact'),
            _record_export=_late_method(service, '_record_export'),
            _serve_hybrid_requests=_late_method(service, '_serve_hybrid_requests'),
            _try_hybrid_delivery=_late_method(service, '_try_hybrid_delivery'),
            _versions=_late_method(service, '_versions'),
            app=_late_resource(service, 'app'),
            hook__id=lambda *args, **kwargs: _id(*args, **kwargs),
            hook__json=lambda *args, **kwargs: _json(*args, **kwargs),
            hook__model_report=lambda *args, **kwargs: _model_report(*args, **kwargs),
            hook__now=lambda *args, **kwargs: _now(*args, **kwargs),
            hook__quality_report=lambda *args, **kwargs: _quality_report(*args, **kwargs),
            hook__safe_failure_code=lambda *args, **kwargs: _safe_failure_code(*args, **kwargs),
            hook_image_export_ready=lambda *args, **kwargs: image_export_ready(*args, **kwargs),
            hook_phase=lambda *args, **kwargs: phase(*args, **kwargs),
            stop_event=_late_resource(service, 'stop_event'),
        )),
        'materials': MaterialProcessing(MaterialProcessingPorts(
            EXTENSIONS=_late_resource(service, 'EXTENSIONS'),
            _cancel_old_jobs=_late_method(service, '_cancel_old_jobs'),
            _check_cancel=_late_method(service, '_check_cancel'),
            _enqueue_response=_late_method(service, '_enqueue_response'),
            _ensure=_late_method(service, '_ensure'),
            _material_capacity_guard=_late_method(service, '_material_capacity_guard'),
            _material_management_guard=_late_method(service, '_material_management_guard'),
            _material_parser_capacity=_late_method(service, '_material_parser_capacity'),
            _mutate=_late_method(service, '_mutate'),
            _new_state=_late_method(service, '_new_state'),
            _path=_late_method(service, '_path'),
            _public_asset=_late_method(service, '_public_asset'),
            _read=_late_method(service, '_read'),
            _reading_invalidate=_late_method(service, '_reading_invalidate'),
            _record_artifact=_late_method(service, '_record_artifact'),
            app=_late_resource(service, 'app'),
            hook__failure_message=lambda *args, **kwargs: _failure_message(*args, **kwargs),
            hook__id=lambda *args, **kwargs: _id(*args, **kwargs),
            hook__now=lambda *args, **kwargs: _now(*args, **kwargs),
            hook__public_source=lambda *args, **kwargs: _public_source(*args, **kwargs),
            hook__report_text=lambda *args, **kwargs: _report_text(*args, **kwargs),
            hook_get_quota_service=lambda *args, **kwargs: get_quota_service(*args, **kwargs),
            hook_material_capacity=lambda *args, **kwargs: material_capacity(*args, **kwargs),
            hook_material_error_code=lambda *args, **kwargs: material_error_code(*args, **kwargs),
            hook_material_parse_error_code=lambda *args, **kwargs: material_parse_error_code(*args, **kwargs),
            hook_material_usage=lambda *args, **kwargs: material_usage(*args, **kwargs),
            material_limits=_late_method(service, 'material_limits'),
            snapshot=_late_method(service, 'snapshot'),
        )),
    }


class WuyuWorkflowService(OfficeMixin, PageLockMixin, RelayoutMixin, MaterialManagementMixin, ResearchWorkflowMixin, TemplateReviewMixin, ReadingWorkflowMixin):
    @cached_property
    def _components(self):
        # Read-only legacy projections may construct the facade via __new__.
        # Composition itself does not require initialized runtime resources.
        return _compose(self)

    ACTIONS = {'update_brief', 'select_references', 'generate_outline', 'update_outline', 'generate_samples', 'regenerate_samples', 'confirm_template', 'generate_next', 'confirm_page', 'revise_page', 'confirm_images', 'start_tracing', 'export', 'retry', 'pause', 'resume', 'cancel', 'generate_focused', 'confirm_template_choice', 'generate_images', 'lock_pages', 'regenerate_unlocked'} | READ_ACTIONS | RESEARCH_ACTIONS

    ASYNC_ACTIONS = {'generate_outline', 'generate_samples', 'regenerate_samples', 'generate_next', 'start_tracing', 'export', '_prepare_relayout', '_parse_materials', '_continue', 'generate_focused', 'generate_images', 'analyze_materials', 'revise_draft', '_replan_unlocked'}

    EXTENSIONS = {'.pdf', '.docx', '.pptx', '.xlsx', '.csv', '.tsv', '.txt', '.md', '.png', '.jpg', '.jpeg', '.webp'}

    EXPORT_FORMATS = {'editable_pptx': '.pptx', 'hybrid_pptx': '.pptx', 'pptx': '.pptx', 'pdf': '.pdf', 'images_zip': '.zip', 'sources': '.json'}

    def __init__(self, app):
        lifecycle.initialize_resources(self, app)
        self._components = _compose(self)

    initialize = lifecycle.initialize

    start_worker = lifecycle.start_worker

    shutdown = lifecycle.shutdown

    terminal_status = lifecycle.terminal_status

    _claim = lifecycle._claim

    run_pending = lifecycle.run_pending

    _fair_queue = lifecycle._fair_queue

    queue_snapshot = lifecycle.queue_snapshot

    _schedule = lifecycle._schedule

    _run_context = lifecycle._run_context

    _heartbeat = lifecycle._heartbeat

    def _lifecycle_dependencies(self):
        # Resolve the existing extension/patch hooks at call time, not at import.
        return lifecycle.LifecycleDependencies(
            execution_mode=execution_mode, owner_lock_held=owner_lock_held,
            recover_workflow_charges=recover_workflow_charges, pid_alive=_pid_alive,
            executor_factory=ThreadPoolExecutor, inspect_schema=inspect)

    _default = _delegate('project', ProjectState._default)

    _new_state = _delegate('project', ProjectState._new_state)

    _ensure = _delegate('project', ProjectState._ensure)

    _read = _delegate('project', ProjectState._read)

    _require_current_reading_replay = _delegate('project', ProjectState._require_current_reading_replay)

    _mutate = _delegate('project', ProjectState._mutate)

    _event = _delegate('project', ProjectState._event)

    provider_status = _delegate('views', WorkflowViews.provider_status)

    test_providers = _delegate('views', WorkflowViews.test_providers)

    _public_artifact = _delegate('views', WorkflowViews._public_artifact)

    snapshot = _delegate('views', WorkflowViews.snapshot)

    _job_dict = _delegate('views', WorkflowViews._job_dict)

    get_job = _delegate('views', WorkflowViews.get_job)

    _validate_page_revision_payload = staticmethod(ActionCoordinator._validate_page_revision_payload)

    def _action_state_dependencies(self):
        return action_state.ActionStateDependencies(
            new_state=self._new_state,
            validate_recipe=self._validate_recipe,
            confirm_template_choice=self._confirm_template_choice,
            patch_specs=self._patch_specs,
            validate_specs=self._validate_specs,
            normalize_specs=self._normalize_specs,
            bind_source_assets=self._bind_source_assets,
            validate_sample_promotion=self._validate_sample_promotion,
            public_source=_public_source,
            event_id=_id,
            error_type=WorkflowAPIError,
            phase=phase,
            current_relayout_source=current_relayout_source,
            apply_event=apply_event,
        )

    def _action_plan_dependencies(self):
        return action_state.JobPlanDependencies(
            reading_queue_action=self._reading_queue_action,
            phase=phase,
            async_actions=self.ASYNC_ACTIONS,
            read_actions=READ_ACTIONS,
            page_retry_scope_key=page_retry.PAGE_RETRY_SCOPE_KEY,
        )

    execute_action = _delegate('actions', ActionCoordinator.execute_action)

    _cancel_old_jobs = _delegate('project', ProjectState._cancel_old_jobs)

    _require_native_rebuild = _delegate('jobs', JobExecution._require_native_rebuild)

    _validate_sample_promotion = _delegate('templates', TemplateConfiguration._validate_sample_promotion)

    _validate_recipe = _delegate('templates', TemplateConfiguration._validate_recipe)

    _catalog_path = _delegate('templates', TemplateConfiguration._catalog_path)

    _default_recipe = _delegate('templates', TemplateConfiguration._default_recipe)

    _public_material = _delegate('materials', MaterialProcessing._public_material)

    _public_asset = _delegate('materials', MaterialProcessing._public_asset)

    material_limits = _delegate('materials', MaterialProcessing.material_limits)

    _bind_source_assets = _delegate('templates', TemplateConfiguration._bind_source_assets)

    _template_references = _delegate('templates', TemplateConfiguration._template_references)

    _pipeline_recipe = _delegate('templates', TemplateConfiguration._pipeline_recipe)

    _normalize_recipe = _delegate('templates', TemplateConfiguration._normalize_recipe)

    _public_recipe = _delegate('templates', TemplateConfiguration._public_recipe)

    _validate_specs = _delegate('templates', TemplateConfiguration._validate_specs)

    _patch_specs = _delegate('templates', TemplateConfiguration._patch_specs)

    _normalize_specs = _delegate('templates', TemplateConfiguration._normalize_specs)

    _diagram_preset_field = staticmethod(TemplateConfiguration._diagram_preset_field)

    _job_catalog_binding = _delegate('jobs', JobExecution._job_catalog_binding)

    _new_job_record = _delegate('jobs', JobExecution._new_job_record)

    _enqueue_response = _delegate('jobs', JobExecution._enqueue_response)

    _check_cancel = _delegate('jobs', JobExecution._check_cancel)

    _reset_inflight = _delegate('project', ProjectState._reset_inflight)

    _close_inflight = _delegate('project', ProjectState._close_inflight)

    _pipeline = _delegate('models', ModelExecution._pipeline)

    _active_runtime_config = _delegate('models', ModelExecution._active_runtime_config)

    _model_workers = _delegate('models', ModelExecution._model_workers)

    _record_batch_timing = _delegate('models', ModelExecution._record_batch_timing)

    _model_batch = _delegate('models', ModelExecution._model_batch)

    _run = _delegate('jobs', JobExecution._run)

    _outline = _delegate('generation', PageGeneration._outline)

    _plan_outline = _delegate('generation', PageGeneration._plan_outline)

    _outline_reserved = _delegate('generation', PageGeneration._outline_reserved)

    _path = _delegate('artifacts', ArtifactDelivery._path)

    _record_artifact = _delegate('artifacts', ArtifactDelivery._record_artifact)

    artifact_path = _delegate('artifacts', ArtifactDelivery.artifact_path)

    _artifact_file = _delegate('artifacts', ArtifactDelivery._artifact_file)

    _samples = _delegate('generation', PageGeneration._samples)

    _generate_batch = _delegate('generation', PageGeneration._generate_batch)

    _generate_next = _delegate('generation', PageGeneration._generate_next)

    _sync_page = _delegate('generation', PageGeneration._sync_page)

    _generate_focused = _delegate('generation', PageGeneration._generate_focused)

    _deliver_focused_images = _delegate('generation', PageGeneration._deliver_focused_images)

    _continue = _delegate('generation', PageGeneration._continue)

    _progress = _delegate('jobs', JobExecution._progress)

    _trace_and_export = _delegate('artifacts', ArtifactDelivery._trace_and_export)

    _versions = _delegate('artifacts', ArtifactDelivery._versions)

    _image_signature = _delegate('artifacts', ArtifactDelivery._image_signature)

    _hybrid_signature = _delegate('artifacts', ArtifactDelivery._hybrid_signature)

    _current_hybrid_export = _delegate('artifacts', ArtifactDelivery._current_hybrid_export)

    _try_hybrid_delivery = _delegate('artifacts', ArtifactDelivery._try_hybrid_delivery)

    _serve_hybrid_requests = _delegate('artifacts', ArtifactDelivery._serve_hybrid_requests)

    _ensure_image_export = _delegate('artifacts', ArtifactDelivery._ensure_image_export)

    _export_provider = _delegate('artifacts', ArtifactDelivery._export_provider)

    _export_additional = _delegate('artifacts', ArtifactDelivery._export_additional)

    _record_export = _delegate('artifacts', ArtifactDelivery._record_export)

    add_materials = _delegate('materials', MaterialProcessing.add_materials)

    _parse_materials = _delegate('materials', MaterialProcessing._parse_materials)

    evidence = _delegate('materials', MaterialProcessing.evidence)


OFFICE_SETTINGS = ('WUYU_OFFICE_ENABLED', 'WUYU_OFFICE_PUBLIC_URL', 'WUYU_OFFICE_INTERNAL_URL',
                   'WUYU_OFFICE_BACKEND_INTERNAL_URL', 'WUYU_OFFICE_JWT_SECRET', 'WUYU_OFFICE_BRIDGE_SECRET')


def init_wuyu_workflow(app):
    # Cloud editing is off unless these environment values are provided.
    for key in OFFICE_SETTINGS:
        app.config.setdefault(key, os.getenv(key, 'false' if key == 'WUYU_OFFICE_ENABLED' else ''))
    from controllers.wuyu_office_controller import wuyu_office_bp
    if 'wuyu_office' not in app.blueprints:
        app.register_blueprint(wuyu_office_bp)
    service = WuyuWorkflowService(app)
    app.extensions['wuyu_workflow'] = service
    with app.app_context():
        service.initialize()
    return service
