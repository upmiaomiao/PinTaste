"""Additive design preference records, separate from project evidence."""
from datetime import datetime
import uuid
from . import db


def ident():
    return str(uuid.uuid4())


class DesignFeedback(db.Model):
    __tablename__ = 'wuyu_design_feedback'
    id = db.Column(db.String(36), primary_key=True, default=ident)
    owner_id = db.Column(db.String(36), nullable=False, index=True)
    project_id = db.Column(db.String(36), nullable=False, index=True)
    request_key = db.Column(db.String(160), nullable=False, unique=True)
    action = db.Column(db.String(40), nullable=False)
    text = db.Column(db.Text, nullable=False)
    signals = db.Column(db.Text, nullable=False, default='[]')
    supported = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class DesignPolicy(db.Model):
    __tablename__ = 'wuyu_design_policy'
    id = db.Column(db.Integer, primary_key=True)
    revision = db.Column(db.Integer, nullable=False, default=0)
    body = db.Column(db.Text, nullable=False, default='{}')


class DesignRuleSet(db.Model):
    __tablename__ = 'wuyu_design_rule_sets'
    id = db.Column(db.String(36), primary_key=True, default=ident)
    scope = db.Column(db.String(10), nullable=False)
    owner_id = db.Column(db.String(36), nullable=True, index=True)
    status = db.Column(db.String(20), nullable=False, default='candidate')
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewer_id = db.Column(db.String(36), nullable=True)


class DesignCompileRun(db.Model):
    __tablename__ = 'wuyu_design_compile_runs'
    id = db.Column(db.String(36), primary_key=True, default=ident)
    request_key = db.Column(db.String(160), nullable=False, unique=True)
    actor_id = db.Column(db.String(36), nullable=False)
    scope = db.Column(db.String(10), nullable=False)
    owner_id = db.Column(db.String(36), nullable=True)
    status = db.Column(db.String(24), nullable=False, default='running')
    input = db.Column(db.Text, nullable=False)
    rule_set_id = db.Column(db.String(36), nullable=True)
    error_code = db.Column(db.String(60), nullable=True)
    points_upper = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)


class SavedDesignTemplate(db.Model):
    __tablename__ = 'wuyu_saved_design_templates'
    __table_args__ = (db.UniqueConstraint('owner_id', 'source_signature', name='uq_wuyu_saved_design_signature'),)
    id = db.Column(db.String(36), primary_key=True, default=ident)
    owner_id = db.Column(db.String(36), nullable=False, index=True)
    source_signature = db.Column(db.String(64), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
