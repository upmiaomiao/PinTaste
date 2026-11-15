"""Personal settings and exports, always scoped to the authenticated account."""
import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.background import BackgroundTask
from . import auth
from . import quota_limits
from . import data_export
from . import store as db

router = APIRouter(prefix='/api/me')


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    theme: Literal['light','dark','system'] = 'light'
    font_size: Literal['standard','large','larger'] = 'standard'
    send_key: Literal['enter','ctrl_enter'] = 'enter'
    ui_language: Literal['zh-CN','en'] = 'zh-CN'


def preferences(uid):
    row = db.query('SELECT value FROM user_preferences WHERE user_id=?',(uid,),one=True)
    return Preferences(**json.loads(row['value'])).model_dump() if row else Preferences().model_dump()


@router.get('/preferences')
def get_preferences(user=Depends(auth.current)):
    return preferences(user['id'])


class PreferencesPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    theme: Literal['light','dark','system'] | None = None
    font_size: Literal['standard','large','larger'] | None = None
    send_key: Literal['enter','ctrl_enter'] | None = None
    ui_language: Literal['zh-CN','en'] | None = None

    @model_validator(mode='after')
    def non_null_values(self):
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError('Preference values cannot be null')
        return self


def _save_preferences(uid, changes, expected_user_id):
    if expected_user_id is not None and expected_user_id != uid:
        raise HTTPException(409, 'ACCOUNT_CHANGED')
    # Merge inside the write transaction so a work-language update cannot
    # overwrite a concurrent theme/font/send-key update from the QA page.
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT value FROM user_preferences WHERE user_id=?', (uid,)).fetchone()
        current = Preferences(**json.loads(row['value'])) if row else Preferences()
        updated = Preferences(**(current.model_dump() | changes))
        conn.execute('INSERT INTO user_preferences(user_id,value,updated) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET value=excluded.value,updated=excluded.updated',
                     (uid, updated.model_dump_json(), time.time()))
    return updated.model_dump()


@router.put('/preferences')
def save_preferences(body: Preferences, user=Depends(auth.current),
                     expected_user_id: str | None = Header(default=None, alias='X-Wuyu-Preferences-User-ID')):
    # Legacy clients omit ui_language. Do not reset it when they save other fields.
    return _save_preferences(user['id'], body.model_dump(exclude_unset=True), expected_user_id)


@router.patch('/preferences')
def patch_preferences(body: PreferencesPatch, user=Depends(auth.current),
                      expected_user_id: str | None = Header(default=None, alias='X-Wuyu-Preferences-User-ID')):
    return _save_preferences(user['id'], body.model_dump(exclude_unset=True), expected_user_id)


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=60)
    organization: str = Field(min_length=1,max_length=160)
    identity: str = Field(max_length=40)


@router.patch('/profile')
def save_profile(body: ProfileInput,user=Depends(auth.current)):
    auth.validate_profile(body.name,body.organization,body.identity)
    db.execute('UPDATE users SET name=?,organization=?,identity=? WHERE id=?',
               (body.name.strip(),body.organization.strip(),body.identity,user['id']))
    db.audit(user['id'],'update_own_profile')
    from . import integrated_identity
    return integrated_identity.public_user(user['id'])


@router.get('/data')
def personal_data(user=Depends(auth.current)):
    uid=user['id'];cfg=db.settings()
    day=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    usage=db.query("SELECT COUNT(*) AS questions,COALESCE(SUM(charged),0) AS charged,COALESCE(SUM(CASE WHEN status IN ('queued','running') THEN reserved ELSE 0 END),0) AS reserved FROM jobs WHERE user_id=? AND day=?",(uid,day),one=True)
    return {'conversations':db.query('SELECT COUNT(*) AS n FROM conversations WHERE user_id=? AND deleted=0',(uid,),one=True)['n'],
        'files':db.query('SELECT COUNT(*) AS count,COALESCE(SUM(size),0) AS bytes FROM files WHERE user_id=?',(uid,),one=True),
        'today':usage,'limits':{k:cfg[k] for k in ('daily_tokens','daily_questions','storage_mb')} | {'quota_unlimited': quota_limits.unlimited(uid)}}


class PersonalExportInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    include_attachments: bool = False


@router.post('/exports')
def prepare_export(body: PersonalExportInput,user=Depends(auth.current)):
    path=data_export.build_export('selected',[user['id']],body.include_attachments,user['id'])
    export_id=uuid.uuid4().hex
    target=path.with_name('wuyu-personal-'+user['id']+'-'+export_id+'.json')
    path.rename(target)
    return {'download_url':'/api/me/exports/'+export_id,'size_bytes':target.stat().st_size,'expires_in':3600}


@router.get('/exports/{export_id}')
def download_export(export_id: str,user=Depends(auth.current)):
    if len(export_id)!=32 or any(c not in '0123456789abcdef' for c in export_id): raise HTTPException(404,'导出文件不存在。')
    path=db.DATA/'exports'/('wuyu-personal-'+user['id']+'-'+export_id+'.json')
    if not path.is_file() or path.stat().st_mtime<time.time()-3600: raise HTTPException(404,'导出文件已过期，请重新导出。')
    return FileResponse(path,media_type='application/json',filename='wuyu-my-data-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json',background=BackgroundTask(data_export.remove_export,path))
