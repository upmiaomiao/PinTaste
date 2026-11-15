"""User model for authentication"""
import uuid
from datetime import datetime
from . import db


class User(db.Model):
    """用户模型 - 支持 admin 和 employee 两种角色"""
    __tablename__ = 'users'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='employee')  # 'admin' | 'employee'
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    projects = db.relationship('Project', back_populates='owner', lazy='select')

    def to_dict(self):
        created_at_str = None
        if self.created_at:
            created_at_str = self.created_at.isoformat() + 'Z' if not self.created_at.tzinfo else self.created_at.isoformat()
        return {
            'id': self.id,
            'username': self.username,
            'role': self.role,
            'created_at': created_at_str,
        }

    def __repr__(self):
        return f'<User {self.username} ({self.role})>'
