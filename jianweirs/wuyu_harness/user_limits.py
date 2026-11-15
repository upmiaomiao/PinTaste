"""Independent product preferences and transactional monthly usage accounting."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from contextlib import nullcontext
import json
from fastapi import HTTPException


def month_key():
    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m')


class UserLimits:
    def __init__(self, db):
        self.db = db
        with db.connect() as conn:
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS product_settings(user_id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS product_usage(user_id TEXT, period TEXT, kind TEXT, amount INTEGER NOT NULL,
                PRIMARY KEY(user_id,period,kind));
            CREATE TABLE IF NOT EXISTS usage_events(id TEXT PRIMARY KEY,user_id TEXT,period TEXT,kind TEXT,amount INTEGER);
            ''')

    def settings(self, user_id, default_font_size=14):
        with self.db.connect() as conn:
            row = conn.execute('SELECT body FROM product_settings WHERE user_id=?', (user_id,)).fetchone()
            usage = {r['kind']: r['amount'] for r in conn.execute(
                'SELECT kind,amount FROM product_usage WHERE user_id=? AND period=?', (user_id, month_key()))}
        custom = json.loads(row[0]) if row else {}
        return {'font_size': default_font_size, 'theme': 'system', 'send_key': 'enter',
                'imagery_quota': 100, 'qa_quota': 100,
                **custom, 'font_size_override':custom.get('font_size'),
                'usage': {'imagery': usage.get('imagery', 0), 'qa': usage.get('qa', 0)},
                'period': month_key(), 'reset_timezone': 'Asia/Shanghai'}

    def update_preferences(self, user_id, patch, default_font_size=16):
        """Self-service preferences cannot alter billing, quotas or another user."""
        if not isinstance(patch, dict) or not patch or set(patch) - {'font_size', 'theme', 'send_key'}:
            raise HTTPException(422, '仅可修改字号、主题和发送快捷键')
        return self.update(user_id, patch, default_font_size)

    def update(self, user_id, patch, default_font_size=14):
        if not isinstance(patch, dict) or not patch or set(patch) - {'font_size', 'imagery_quota', 'qa_quota', 'theme', 'send_key'}:
            raise HTTPException(422, '用户设置字段无效')
        for name, value in patch.items():
            if name == 'font_size':
                if value is not None and (type(value) is not int or not 12 <= value <= 24):
                    raise HTTPException(422, '字号需为12至24')
            elif name in ('theme', 'send_key'):
                choices = ('light', 'dark', 'system') if name == 'theme' else ('enter', 'ctrl_enter')
                if not isinstance(value, str) or value not in choices:
                    raise HTTPException(422, '外观或快捷键选项无效')
            elif value is not None and (type(value) is not int or not 0 <= value <= 1000000):
                raise HTTPException(422, '额度需为0至1000000，或null表示不限额')
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM users WHERE id=?', (user_id,)).fetchone():
                raise HTTPException(404, '用户不存在')
            row = conn.execute('SELECT body FROM product_settings WHERE user_id=?', (user_id,)).fetchone()
            body = {**(json.loads(row[0]) if row else {}), **patch}
            if body.get('font_size') is None:
                body.pop('font_size', None)
            conn.execute('INSERT OR REPLACE INTO product_settings VALUES(?,?)', (user_id, json.dumps(body)))
        return self.settings(user_id, default_font_size)

    def charge(self, user_id, kind, event_id, amount=1, *, _connection=None):
        if kind not in ('imagery', 'qa') or type(amount) is not int or amount < 1:
            raise ValueError('invalid usage')
        with (self.db.connect() if _connection is None else nullcontext(_connection)) as conn:
            if _connection is None:
                conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT user_id,kind,amount FROM usage_events WHERE id=?', (event_id,)).fetchone()
            if old:
                if tuple(old) != (user_id, kind, amount):
                    raise ValueError('usage event conflict')
                return False
            settings = conn.execute('SELECT body FROM product_settings WHERE user_id=?', (user_id,)).fetchone()
            limit = (json.loads(settings[0]) if settings else {}).get(kind + '_quota', 100)
            period = month_key()
            row = conn.execute('SELECT amount FROM product_usage WHERE user_id=? AND period=? AND kind=?',
                               (user_id, period, kind)).fetchone()
            used = row[0] if row else 0
            if limit is not None and used + amount > limit:
                raise HTTPException(429, '本月' + ('影像获取' if kind == 'imagery' else '问答') + '额度已用完，请联系见微系统管理员')
            conn.execute('INSERT INTO product_usage VALUES(?,?,?,?) ON CONFLICT(user_id,period,kind) DO UPDATE SET amount=product_usage.amount+excluded.amount',
                         (user_id, period, kind, amount))
            conn.execute('INSERT INTO usage_events VALUES(?,?,?,?,?)', (event_id, user_id, period, kind, amount))
            return True

    def refund(self, event_id):
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM usage_events WHERE id=?', (event_id,)).fetchone()
            if row:
                conn.execute('UPDATE product_usage SET amount=MAX(0,amount-?) WHERE user_id=? AND period=? AND kind=?',
                             (row['amount'], row['user_id'], row['period'], row['kind']))
                conn.execute('DELETE FROM usage_events WHERE id=?', (event_id,))

    def accept_tasks(self, user_id, event_id, records, *, scan=None):
        """Persist every task and its quota debit in one transaction or neither."""
        if not records or any(record.get('user_id') != user_id for record in records):
            raise ValueError('invalid task owner')
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not self.charge(user_id, 'qa', event_id, len(records), _connection=conn):
                raise HTTPException(409, '这组任务已经提交，请在历史任务中查看')
            for record in records:
                conn.execute('INSERT INTO tasks VALUES(?,?,?)',
                    (record['id'], user_id, json.dumps(record, ensure_ascii=False)))
            if scan is not None:
                if scan.get('user_id') != user_id:
                    raise ValueError('invalid scan owner')
                conn.execute('INSERT OR REPLACE INTO scans VALUES(?,?,?)',
                    (scan['id'], user_id, json.dumps(scan, ensure_ascii=False)))
