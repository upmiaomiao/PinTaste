"""Consent that only a person clicking the interface can give.

Research scope, template choice and (later) any other approval share ONE rule: the request
must be authenticated, belong to this application, name the expected interaction in the
X-Wuyu-Interaction header, and come from the project's owner (or an admin). A model acting
through the tools cannot satisfy it. The header is an interaction boundary, not a credential.

Before this module the rule was written twice (research, template) with different messages
and slightly different shapes, so a fix to one never reached the other.
"""

RESEARCH = 'research-confirmation'
TEMPLATE = 'template-confirmation'

_MESSAGES = {
    RESEARCH: ('请在对话中的资料调研选项确认范围与预算，模型不能代为同意。', 'RESEARCH_CONFIRMATION_REQUIRED'),
    TEMPLATE: ('请在模板预览中点击确认使用，不能由模型代为确认。', 'TEMPLATE_CONFIRMATION_UI_REQUIRED'),
}


def require_user_interaction(app, project_id, interaction):
    """Raise WorkflowAPIError(403) unless the current request is that interaction.

    Checked inside the mutation and again before an idempotent replay, so an internal
    server action without a real request context can never mark consent.
    """
    from flask import current_app, has_request_context, request
    from models import db, Project
    from .wuyu_workflow_service import WorkflowAPIError

    message, code = _MESSAGES[interaction]
    actor = getattr(request, 'current_user', None) if has_request_context() else None
    project = db.session.get(Project, project_id) if actor is not None else None
    valid = (actor is not None and current_app._get_current_object() is app
             and request.headers.get('X-Wuyu-Interaction') == interaction
             and project is not None
             and (project.user_id == getattr(actor, 'id', None) or getattr(actor, 'role', None) == 'admin'))
    if not valid:
        raise WorkflowAPIError(message, code, 403)
