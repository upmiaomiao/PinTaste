"""Per-user first-run profile (op201): the Work questionnaire answers.

Imported lazily by services.wuyu_user_profile (never from app.py), so a database without the table still
passes the boot-time schema check; the service then reports itself unavailable.
"""
from datetime import datetime
from . import db


class WuyuUserProfile(db.Model):
    __tablename__ = 'wuyu_user_profiles'
    owner_id = db.Column(db.String(36), primary_key=True)
    value = db.Column(db.Text, nullable=False, default='{}')
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
