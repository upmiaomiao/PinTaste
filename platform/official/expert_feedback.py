"""Versioned expert reviews; permission grants never change account roles."""
import json
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from . import store as db
from . import auth

router = APIRouter()
CRITERIA = [
    {'id': 'question', 'label': '问题理解', 'description': '是否理解问题、条件与用户目标。'},
    {'id': 'reasoning', 'label': '思考过程', 'description': '检查模型实际返回的思考内容是否遗漏条件或产生错误推断。'},
    {'id': 'logic', 'label': '推理逻辑', 'description': '推理是否连贯，因果、计算、单位与结论是否成立。'},
    {'id': 'tools', 'label': '工具调用', 'description': '工具选择、参数、返回内容及模型对工具结果的使用是否正确。'},
    {'id': 'evidence', 'label': '证据与引用', 'description': '原文、页码、来源和数值能否支持结论，是否存在编造。'},
    {'id': 'answer', 'label': '最终回答', 'description': '回答是否准确、完整、清晰，是否说明证据边界与不足。'},
]
DEFAULT_RULES = '先核对问题与原始资料，再检查思考过程、逻辑、工具调用和最终回答。意见应指出具体位置、问题原因及修改建议；涉及数值、来源或专业判断时补充依据。模型没有返回的思考内容不得推测或补写。专家意见单独保存，不覆盖原始问答。'
DEFAULTS = {'enabled': False, 'voice_enabled': True, 'title': '专家评审规则', 'rules': DEFAULT_RULES, 'rule_version': 0}
TERMINAL = {'complete', 'error', 'incomplete', 'stopped', 'interrupted'}


def init():
    with db.connect() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS expert_reviewers(user_id TEXT PRIMARY KEY REFERENCES users(id),
          granted_by TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS expert_feedback_rules(id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,rules TEXT NOT NULL,criteria TEXT NOT NULL,created REAL NOT NULL,actor TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS expert_feedback(id TEXT PRIMARY KEY,
          job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,reviewer_id TEXT NOT NULL REFERENCES users(id),
          revision INTEGER NOT NULL,supersedes TEXT REFERENCES expert_feedback(id),created REAL NOT NULL,
          turn_revision INTEGER NOT NULL,turn_digest TEXT NOT NULL,content TEXT NOT NULL,rule_snapshot TEXT NOT NULL,
          UNIQUE(job_id,reviewer_id,revision));
        CREATE INDEX IF NOT EXISTS expert_feedback_turn ON expert_feedback(job_id,created,id);
        ''')


def config(conn=None):
    row = (dict(conn.execute("SELECT value FROM settings WHERE key='expert_feedback_config'").fetchone() or {}) if conn
           else db.query("SELECT value FROM settings WHERE key='expert_feedback_config'", one=True))
    return DEFAULTS | (json.loads(row['value']) if row else {})


def rules_snapshot(cfg=None):
    cfg = cfg or config()
    return {'version': cfg['rule_version'], 'title': cfg['title'], 'rules': cfg['rules'], 'criteria': CRITERIA,
            'requirements': ['指出具体问题和位置', '给出修改建议或说明无需修改', '保留原始问答和每次反馈版本']}


def is_reviewer(user):
    return user['role'] == 'admin' or bool(db.query('SELECT user_id FROM expert_reviewers WHERE user_id=?', (user['id'],), one=True))


def public_config(user):
    cfg = config()
    allowed = cfg['enabled'] and is_reviewer(user)
    return {'enabled': bool(allowed), 'voice_enabled': bool(allowed and cfg['voice_enabled']),
            'rules': rules_snapshot(cfg) if allowed else None}


def review_job(jid, user, submit=False, voice=False):
    cfg = config()
    if submit and (not cfg['enabled'] or not is_reviewer(user)):
        raise HTTPException(403, '专家反馈尚未开启，或当前账号没有专家权限。')
    if voice and not cfg['voice_enabled']:
        raise HTTPException(403, '管理员已关闭专家语音反馈。')
    if not submit and user['role'] != 'admin' and not public_config(user)['enabled']:
        raise HTTPException(403, '当前账号不能查看专家反馈。')
    row = db.query('SELECT * FROM jobs WHERE id=?' + ('' if user['role'] == 'admin' else ' AND user_id=?'),
                   (jid,) if user['role'] == 'admin' else (jid, user['id']), one=True)
    if not row: raise HTTPException(404, '问答不存在。')
    if submit and row['status'] not in TERMINAL:
        raise HTTPException(409, '请等本轮生成结束后再提交专家反馈。')
    return row


def feedback_records(conn, jid):
    rows = conn.execute('''SELECT f.*,u.name,u.email,u.organization,u.identity FROM expert_feedback f
        JOIN users u ON u.id=f.reviewer_id WHERE f.job_id=? ORDER BY f.created,f.id''', (jid,)).fetchall()
    result = []
    for raw in rows:
        row = dict(raw)
        row['reviewer'] = {key: row.pop(key) for key in ('name', 'email', 'organization', 'identity')}
        row['reviewer']['id'] = row['reviewer_id']
        row['content'] = json.loads(row['content']); row['rule_snapshot'] = json.loads(row['rule_snapshot'])
        result.append(row)
    return result


class ConfigInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    voice_enabled: bool = True
    title: str = Field(min_length=1, max_length=100)
    rules: str = Field(min_length=1, max_length=10000)

    @model_validator(mode='after')
    def nonblank(self):
        self.title = self.title.strip(); self.rules = self.rules.strip()
        if not self.title or not self.rules: raise ValueError('请填写评审规则名称与内容。')
        return self


@router.get('/api/admin/expert-feedback')
def admin_config(user=Depends(auth.admin)):
    from . import voice
    reviewers = db.query('''SELECT u.id,u.name,u.email,u.organization,u.role,u.active,
        CASE WHEN u.role='admin' OR r.user_id IS NOT NULL THEN 1 ELSE 0 END AS expert
        FROM users u LEFT JOIN expert_reviewers r ON r.user_id=u.id ORDER BY u.created,u.id''')
    return config() | {'criteria': CRITERIA, 'reviewers': reviewers, 'voice': voice.public_config(),
                       'counts': db.query('SELECT COUNT(*) AS versions,COUNT(DISTINCT job_id) AS reviewed_turns FROM expert_feedback', one=True)}


@router.put('/api/admin/expert-feedback')
def save_config(body: ConfigInput, user=Depends(auth.admin)):
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        previous = config(conn)
        data = body.model_dump()
        version = previous['rule_version']
        if version == 0 or previous['title'] != data['title'] or previous['rules'] != data['rules']:
            cursor = conn.execute('INSERT INTO expert_feedback_rules(title,rules,criteria,created,actor) VALUES(?,?,?,?,?)',
                (data['title'], data['rules'], json.dumps(CRITERIA, ensure_ascii=False), time.time(), user['id']))
            version = cursor.lastrowid
        conn.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     ('expert_feedback_config', json.dumps(data | {'rule_version': version}, ensure_ascii=False)))
    db.audit(user['id'], 'update_expert_feedback_config')
    return config()


class GrantInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool


@router.put('/api/admin/expert-feedback/reviewers/{uid}')
def grant(uid: str, body: GrantInput, user=Depends(auth.admin)):
    target = db.query('SELECT id,role FROM users WHERE id=?', (uid,), one=True)
    if not target: raise HTTPException(404, '账号不存在。')
    if target['role'] == 'admin': raise HTTPException(409, '管理员已具有专家评审权限。')
    if body.enabled:
        db.execute('INSERT INTO expert_reviewers(user_id,granted_by,created) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET granted_by=excluded.granted_by,created=excluded.created', (uid, user['id'], time.time()))
    else: db.execute('DELETE FROM expert_reviewers WHERE user_id=?', (uid,))
    db.audit(user['id'], 'expert_reviewer:' + uid + ':' + str(body.enabled))
    return {'ok': True}


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    job_revision: int = Field(ge=1)
    rule_version: int = Field(ge=0)
    expected_feedback_revision: int = Field(default=0, ge=0)
    verdict: Literal['acceptable', 'revision_required', 'unusable'] = 'revision_required'
    severity: Literal['none', 'minor', 'major', 'critical'] = 'major'
    issues: list[Literal['question', 'reasoning', 'logic', 'tools', 'evidence', 'answer']] = Field(default_factory=list, max_length=6)
    comment: str = Field(min_length=1, max_length=20000)
    suggestion: str = Field(default='', max_length=20000)
    corrected_answer: str = Field(default='', max_length=20000)
    voice_call_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode='after')
    def validate_content(self):
        self.comment = self.comment.strip(); self.suggestion = self.suggestion.strip()
        self.corrected_answer = self.corrected_answer.strip(); self.issues = list(dict.fromkeys(self.issues))
        self.voice_call_ids = list(dict.fromkeys(self.voice_call_ids))
        if not self.comment: raise ValueError('请填写评审意见。')
        if self.verdict == 'acceptable': self.severity = 'none'
        elif self.severity == 'none': raise ValueError('存在不足时，请选择问题程度。')
        if self.verdict != 'acceptable' and (not self.issues or not self.suggestion):
            raise ValueError('存在不足时，请选择问题位置并填写修改建议。')
        return self


@router.get('/api/jobs/{jid}/expert-feedback')
def get_feedback(jid: str, user=Depends(auth.current)):
    row = review_job(jid, user)
    with db.connect() as conn: records = feedback_records(conn, jid)
    return {'job_id': jid, 'job_revision': row['revision'], 'rules': rules_snapshot(),
            'can_submit': public_config(user)['enabled'] and row['status'] in TERMINAL,
            'items': records}


@router.post('/api/jobs/{jid}/expert-feedback')
def save_feedback(jid: str, body: FeedbackInput, user=Depends(auth.current)):
    review_job(jid, user, submit=True)
    now = time.time(); fid = uuid.uuid4().hex
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        cfg = config(conn)
        eligible = user['role'] == 'admin' or conn.execute('SELECT user_id FROM expert_reviewers WHERE user_id=?', (user['id'],)).fetchone()
        if not cfg['enabled'] or not eligible: raise HTTPException(403, '专家反馈已关闭或专家权限已撤销。')
        if cfg['rule_version'] != body.rule_version: raise HTTPException(409, '评审规则已更新，请刷新规则与记录后再提交。')
        row = dict(conn.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
        if row['revision'] != body.job_revision:
            raise HTTPException(409, '问答记录已更新，请重新打开反馈后提交。')
        latest = conn.execute('SELECT id,revision FROM expert_feedback WHERE job_id=? AND reviewer_id=? ORDER BY revision DESC LIMIT 1',
                              (jid, user['id'])).fetchone()
        if (latest['revision'] if latest else 0) != body.expected_feedback_revision:
            raise HTTPException(409, '已有新的反馈版本，请重新打开后再修改。')
        content = body.model_dump(exclude={'job_revision', 'rule_version', 'expected_feedback_revision'})
        voices = []
        for call_id in body.voice_call_ids:
            voice = conn.execute('''SELECT id,purpose,target_job_id,field,created,seconds,status,transcript,model_label
                FROM voice_calls WHERE id=? AND user_id=? AND purpose='expert_feedback' AND target_job_id=? AND status='complete' ''',
                (call_id, user['id'], jid)).fetchone()
            if not voice: raise HTTPException(422, '语音记录不属于本轮评审，或转写尚未完成。')
            voices.append(dict(voice))
        content['voice_transcripts'] = voices
        content['input_modes'] = ['text', 'voice'] if voices else ['text']
        revision = (latest['revision'] if latest else 0) + 1
        conn.execute('INSERT INTO expert_feedback VALUES(?,?,?,?,?,?,?,?,?,?)',
            (fid, jid, user['id'], revision, latest['id'] if latest else None, now, row['revision'], db.digest(row['turn']),
             json.dumps(content, ensure_ascii=False), json.dumps(rules_snapshot(cfg), ensure_ascii=False)))
        saved = next(r for r in feedback_records(conn, jid) if r['id'] == fid)
    db.audit(user['id'], 'expert_feedback_saved:' + fid)
    return saved


@router.get('/api/jobs/{jid}/expert-feedback/trace')
def get_trace(jid: str, user=Depends(auth.current)):
    review_job(jid, user)
    events = db.query('SELECT sequence,created,event FROM job_events WHERE job_id=? ORDER BY sequence', (jid,))
    for item in events: item['event'] = json.loads(item['event'])
    return {'availability': 'recorded' if events else 'legacy_partial',
            'capture_complete': bool(events and events[-1]['event']['type']=='execution_end'), 'events': events}


@router.get('/api/admin/expert-feedback/turns')
def review_turns(offset: int = 0, user_id: str = '', reviewed: bool = False, user=Depends(auth.admin)):
    if not 0 <= offset <= 100000 or len(user_id) > 64: raise HTTPException(422, '筛选参数无效。')
    where = "j.status NOT IN ('queued','running')"
    values = []
    if user_id: where += ' AND j.user_id=?'; values.append(user_id)
    if reviewed: where += ' AND EXISTS(SELECT 1 FROM expert_feedback f WHERE f.job_id=j.id)'
    rows = db.query('''SELECT j.id,j.user_id,j.conversation_id,j.status,j.created,j.turn,u.name,u.email,
        (SELECT COUNT(*) FROM expert_feedback f WHERE f.job_id=j.id) AS feedback_versions
        FROM jobs j JOIN users u ON u.id=j.user_id WHERE ''' + where + ' ORDER BY j.created DESC,j.id DESC LIMIT 50 OFFSET ?',
        (*values, offset))
    for row in rows: row['question'] = json.loads(row.pop('turn')).get('user', '')[:300]
    return {'items': rows, 'offset': offset, 'has_more': len(rows) == 50}


@router.get('/api/admin/expert-feedback/turns/{jid}')
def admin_turn(jid: str, user=Depends(auth.admin)):
    row = review_job(jid, user)
    from .jobs import public_turn
    return {'turn': public_turn(row), 'user': db.query('SELECT id,name,email,organization FROM users WHERE id=?', (row['user_id'],), one=True),
            'task': {k: row[k] for k in ('id', 'status', 'created', 'started', 'finished', 'revision')}}
