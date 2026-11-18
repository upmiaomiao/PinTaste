"""Per-user design habits: typed memory items, their audit trail and usage logs.

This module is imported lazily by services.wuyu_design_habits (never from
app.py) so an un-migrated PostgreSQL database still passes the boot-time
schema check; the service then reports the feature as unavailable.
"""
from datetime import datetime
import uuid
from . import db


def ident():
    return str(uuid.uuid4())


class DesignEvent(db.Model):
    """Immutable log of user design actions; the raw material of later inference."""
    __tablename__ = 'wuyu_design_events'
    id = db.Column(db.String(36), primary_key=True, default=ident)
    owner_id = db.Column(db.String(36), nullable=False, index=True)
    project_id = db.Column(db.String(36), nullable=False, index=True)
    page_number = db.Column(db.Integer, nullable=True)
    kind = db.Column(db.String(40), nullable=False)
    payload = db.Column(db.Text, nullable=False)
    context = db.Column(db.Text, nullable=False, default='{}')
    request_key = db.Column(db.String(160), nullable=True, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class DesignHabit(db.Model):
    __tablename__ = 'wuyu_design_habits'
    id = db.Column(db.String(36), primary_key=True, default=ident)
    layer = db.Column(db.String(10), nullable=False, default='personal')
    owner_id = db.Column(db.String(36), nullable=True, index=True)
    category = db.Column(db.String(12), nullable=False)
    slot = db.Column(db.String(60), nullable=False)
    scope = db.Column(db.Text, nullable=False, default='{}')
    value = db.Column(db.Text, nullable=False, default='{}')
    rule = db.Column(db.Text, nullable=False)
    origin = db.Column(db.String(12), nullable=False, default='explicit')
    evidence = db.Column(db.Text, nullable=False, default='[]')
    project_count = db.Column(db.Integer, nullable=False, default=0)
    helpful = db.Column(db.Integer, nullable=False, default=0)
    harmful = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(10), nullable=False, default='candidate')
    pinned = db.Column(db.Boolean, nullable=False, default=False)
    paused = db.Column(db.Boolean, nullable=False, default=False)
    valid_from = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    valid_to = db.Column(db.DateTime, nullable=True)
    superseded_by = db.Column(db.String(36), nullable=True)
    last_used_at = db.Column(db.DateTime, nullable=True)
    last_confirmed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    version = db.Column(db.Integer, nullable=False, default=1)


class DesignHabitRevision(db.Model):
    __tablename__ = 'wuyu_design_habit_revisions'
    id = db.Column(db.String(36), primary_key=True, default=ident)
    habit_id = db.Column(db.String(36), nullable=False, index=True)
    owner_id = db.Column(db.String(36), nullable=False, index=True)
    actor = db.Column(db.String(10), nullable=False)
    action = db.Column(db.String(20), nullable=False)
    before = db.Column(db.Text, nullable=True)
    after = db.Column(db.Text, nullable=True)
    reason = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class DesignInjectionLog(db.Model):
    __tablename__ = 'wuyu_design_injections'
    id = db.Column(db.String(36), primary_key=True, default=ident)
    owner_id = db.Column(db.String(36), nullable=False, index=True)
    project_id = db.Column(db.String(36), nullable=False, index=True)
    request_key = db.Column(db.String(160), nullable=True)
    habit_ids = db.Column(db.Text, nullable=False, default='[]')
    rule_ids = db.Column(db.Text, nullable=False, default='[]')
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class DesignHabitPreference(db.Model):
    __tablename__ = 'wuyu_design_habit_prefs'
    owner_id = db.Column(db.String(36), primary_key=True)
    paused = db.Column(db.Boolean, nullable=False, default=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
