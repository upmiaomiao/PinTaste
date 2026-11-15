"""Additive workflow tables; existing Project/Page/Task data is retained."""
import uuid
from datetime import datetime
from . import db


class WuyuWorkflow(db.Model):
    __tablename__ = 'wuyu_workflows'
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id', ondelete='CASCADE'), primary_key=True)
    revision = db.Column(db.Integer, nullable=False, default=0)
    body = db.Column(db.Text, nullable=False)
    active_job_id = db.Column(db.String(36), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    project = db.relationship('Project', backref=db.backref('wuyu_workflow', uselist=False,
                                                           cascade='all, delete-orphan'))


class WuyuWorkflowJob(db.Model):
    __tablename__ = 'wuyu_workflow_jobs'
    __table_args__ = (db.UniqueConstraint('project_id', 'idempotency_key', name='uq_wuyu_project_request'),)
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = db.Column(db.String(36), db.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False, index=True)
    task_id = db.Column(db.String(36), db.ForeignKey('tasks.id'), nullable=True)
    actor_user_id = db.Column(db.String(36), db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)
    action = db.Column(db.String(50), nullable=False)
    payload = db.Column(db.Text, nullable=False, default='{}')
    status = db.Column(db.String(20), nullable=False, default='queued', index=True)
    idempotency_key = db.Column(db.String(120), nullable=True)
    input_epoch = db.Column(db.Integer, nullable=False, default=0)
    cancel_requested = db.Column(db.Boolean, nullable=False, default=False)
    progress = db.Column(db.Text, nullable=False, default='{}')
    error_code = db.Column(db.String(80), nullable=True)
    retry_of = db.Column(db.String(36), nullable=True)
    worker_pid = db.Column(db.Integer, nullable=True)
    heartbeat_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    project = db.relationship('Project', backref=db.backref('wuyu_jobs', cascade='all, delete-orphan'))
    task = db.relationship('Task', backref=db.backref('wuyu_jobs', cascade='all, delete-orphan'))
