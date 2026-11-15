"""Shared workflow identities and value helpers; no service or database access."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime
import json
import uuid

_UNCAPTURED_CATALOG = object()

class WorkflowAPIError(ValueError):
    def __init__(self, message, code='INVALID_WORKFLOW_ACTION', status=400):
        super().__init__(message)
        self.code, self.status = code, status

class CancelledWork(Exception):
    pass

def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

def _now():
    return datetime.utcnow().isoformat() + 'Z'

def _id():
    return str(uuid.uuid4())

DESIGN_KEYS = ('style', 'requirements', 'design_notes')

def _design_requirements(brief):
    """What steers a page's look. style and requirements are always sent (as before);
    design_notes only when the user actually wrote some, so a project that never used it
    produces exactly the same request as before this field existed."""
    result = {key: brief.get(key, '') for key in ('style', 'requirements')}
    notes = brief.get('design_notes')
    if isinstance(notes, str) and notes.strip():
        result['design_notes'] = notes
    return deepcopy(result)
