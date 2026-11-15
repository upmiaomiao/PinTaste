"""Durable, user-scoped deduplication for paid task submissions.

An interrupted request is never automatically replayed: paid side effects may
already exist. The caller must inspect task history before making a new request.
"""
from __future__ import annotations

import hashlib
import json
import re
import time

from fastapi import HTTPException


def protected_submission(path: str) -> bool:
    return path == '/portal/api/tasks' or bool(re.fullmatch(
        r'/portal/api/(uploads/[^/]+/workbook/batch|complaints/scans/[^/]+/start)', path))


class RequestReplay:
    def __init__(self, db):
        self.db = db
        with db.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS request_replays(
                user_id TEXT, request_key TEXT, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, response TEXT, http_status INTEGER,
                created REAL NOT NULL, PRIMARY KEY(user_id, request_key))''')

    def begin(self, user_id, key, path, body):
        if not re.fullmatch(r'[a-zA-Z0-9_.:-]{16,128}', key):
            raise HTTPException(422, '请求编号无效，请刷新后提交')
        fingerprint = hashlib.sha256(path.encode() + b'\0' + body).hexdigest()
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM request_replays WHERE user_id=? AND request_key=?',
                               (user_id, key)).fetchone()
            if row:
                if row['fingerprint'] != fingerprint:
                    raise HTTPException(409, '请求编号对应的内容已改变，请重新提交')
                if row['status'] == 'complete':
                    return row['http_status'], json.loads(row['response'])
                raise HTTPException(409, '该提交正在处理或已被中断，请先查看历史任务，避免重复执行',
                                    headers={'Retry-After': '3', 'X-Submission-State': 'pending'})
            conn.execute('INSERT INTO request_replays VALUES(?,?,?,?,?,?,?)',
                         (user_id, key, fingerprint, 'pending', None, None, time.time()))
        return None

    def finish(self, user_id, key, status_code, body):
        with self.db.connect() as conn:
            if status_code >= 500:
                # An exception does not prove the task wasn't created.
                return
            if status_code >= 400:
                conn.execute('DELETE FROM request_replays WHERE user_id=? AND request_key=?', (user_id, key))
            else:
                conn.execute('UPDATE request_replays SET status=?,response=?,http_status=? WHERE user_id=? AND request_key=?',
                             ('complete', json.dumps(body, ensure_ascii=False), status_code, user_id, key))
