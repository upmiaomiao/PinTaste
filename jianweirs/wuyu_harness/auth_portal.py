"""Product-local identity projections, hashed local sessions and resource ownership."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from fastapi import HTTPException

from .db_backend import IntegrityError

COOKIE = "wuyu_portal_session"
SESSION_SECONDS = 12 * 3600


def password_hash(password: str) -> str:
    if not isinstance(password, str) or not 10 <= len(password) <= 128 or not password.strip():
        raise HTTPException(422, "密码需为10到128个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1,
                            dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt$32768$8$1$" + salt.hex() + "$" + digest.hex()


def password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", "32768", "8", "1") or not 10 <= len(password) <= 128:
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768, r=8, p=1,
                                dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError, AttributeError):
        return False


_DUMMY_HASH = password_hash(secrets.token_urlsafe(24))


def username_value(value: str) -> str:
    value = str(value).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@+-]{2,99}", value):
        raise HTTPException(422, "账号需为3到100位字母、数字或邮箱字符")
    return value


def public_user(row) -> dict:
    result = {key: row[key] for key in ("id", "username", "display_name", "role", "active", "created_at")}
    result["active"] = bool(result["active"])
    return result


class PortalDB:
    def __init__(self, folder: Path):
        self.folder = folder.resolve()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "portal.sqlite3"
        from .db_backend import open_pool
        self._pool = open_pool(self.path, timeout=15, row_factory=sqlite3.Row, persistent=None)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
              display_name TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL,
              password_hash TEXT NOT NULL, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(digest TEXT PRIMARY KEY, user_id TEXT NOT NULL,
              csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(username TEXT, ip TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS ownership(kind TEXT, resource_id TEXT, user_id TEXT,
              created REAL, PRIMARY KEY(kind,resource_id));
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scans(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, body TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS task_user_idx ON tasks(user_id);
            CREATE INDEX IF NOT EXISTS task_status_idx ON tasks(json_extract(body,'$.status'));
            CREATE INDEX IF NOT EXISTS ownership_user_idx ON ownership(user_id,kind);
            CREATE INDEX IF NOT EXISTS session_user_idx ON sessions(user_id);
            CREATE TABLE IF NOT EXISTS config(id INTEGER PRIMARY KEY CHECK(id=1), ciphertext BLOB NOT NULL);
            """)
        if self.path.exists():
            self.path.chmod(0o600)

    def connect(self):
        """每线程一个长期复用的连接（见 sqlite_pool）；用法与以前相同。"""
        return self._pool.connect()

    def close(self):
        self._pool.close_all()

    def needs_setup(self) -> bool:
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0

    def identity_user(self, identity):
        """Project a verified central ID without copying passwords or other app data."""
        uid = 'wuyu_' + identity['id']
        name = str(identity.get('name') or identity.get('email') or '无隅用户')[:100]
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR IGNORE INTO users VALUES(?,?,?,?,?,?,?)',
                (uid, uid, name, 'admin' if identity['role'] == 'admin' else 'user', 1,
                 'external_identity_only', time.time()))
            db.execute('UPDATE users SET display_name=? WHERE id=?', (name, uid))
            return public_user(db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone())

    def create_user(self, username, password, display_name="", role="user", *, bootstrap=False):
        username = username_value(username)
        if role not in ("user", "admin"):
            raise HTTPException(422, "角色无效")
        display_name = str(display_name or username).strip()
        if not 1 <= len(display_name) <= 100:
            raise HTTPException(422, "显示名称长度无效")
        encoded, uid = password_hash(password), uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if bootstrap and db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                raise HTTPException(409, "首次安装已完成")
            try:
                db.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)", (uid, username, display_name, role, 1, encoded, time.time()))
            except IntegrityError:
                raise HTTPException(409, "账号已存在") from None
            return public_user(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def login(self, username, password, ip):
        username = username_value(username)
        now = time.time()
        with self.connect() as db:
            db.execute("DELETE FROM attempts WHERE created<?", (now - 600,))
            # 只统计"失败"的尝试：同一账号 15 次失败后锁 10 分钟；同一出口 IP 的上限高得多，
            # 因为反向代理后面所有用户可能共用一个 IP，不能让一个人的失败影响全体登录。
            by_user = db.execute("SELECT COUNT(*) FROM attempts WHERE username=?", (username,)).fetchone()[0]
            by_ip = db.execute("SELECT COUNT(*) FROM attempts WHERE ip=?", (ip,)).fetchone()[0]
            if by_user >= 15 or by_ip >= 300:
                raise HTTPException(429, "登录尝试过多，请10分钟后重试")
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not password_matches(password, row["password_hash"] if row else _DUMMY_HASH) or not row or not row["active"]:
            with self.connect() as db:
                db.execute("INSERT INTO attempts VALUES(?,?,?)", (username, ip, now))
            raise HTTPException(401, "账号或密码错误，或账号已停用")
        return public_user(row)

    def issue_session(self, user_id):
        token, csrf = secrets.token_urlsafe(36), secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user_id, csrf, time.time()+SESSION_SECONDS))
        return token, csrf

    def session(self, token):
        if not token or len(token) > 200:
            return None
        with self.connect() as db:
            row = db.execute("SELECT u.*,s.csrf FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.digest=? AND s.expires>? AND u.active=1",
                             (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
        return {"user":public_user(row), "csrf_token":row["csrf"]} if row else None

    def logout(self, token):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (hashlib.sha256(token.encode()).hexdigest(),))

    def users(self):
        with self.connect() as db:
            return [public_user(row) for row in db.execute("SELECT * FROM users ORDER BY created_at")]

    def update_user(self, user_id, patch):
        if not patch or not set(patch) <= {"active", "role", "display_name", "password"}:
            raise HTTPException(422, "用户更新字段无效")
        if "role" in patch and patch["role"] not in ("user", "admin"):
            raise HTTPException(422, "角色无效")
        if "active" in patch and type(patch["active"]) is not bool:
            raise HTTPException(422, "active必须为布尔值")
        if "display_name" in patch and not 1 <= len(str(patch["display_name"]).strip()) <= 100:
            raise HTTPException(422, "显示名称无效")
        values = {key:value for key,value in patch.items() if key != "password"}
        if "password" in patch:
            values["password_hash"] = password_hash(patch["password"])
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone()
            if not old:
                raise HTTPException(404, "账号不存在")
            if old["role"] == "admin" and old["active"] and (patch.get("role") == "user" or patch.get("active") is False):
                if db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
                    raise HTTPException(409, "必须保留至少一个可登录管理员")
            for key,value in values.items():
                db.execute(f"UPDATE users SET {key}=? WHERE id=?", (value,user_id))
            if "password" in patch or any(key in patch and patch[key]!=old[key] for key in ("role","active")):
                db.execute("DELETE FROM sessions WHERE user_id=?",(user_id,))
            return public_user(db.execute("SELECT * FROM users WHERE id=?",(user_id,)).fetchone())

    def own(self, kind, resource_id, user_id):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO ownership VALUES(?,?,?,?)",(kind,resource_id,user_id,time.time()))
            owner = db.execute("SELECT user_id FROM ownership WHERE kind=? AND resource_id=?",(kind,resource_id)).fetchone()
            if owner[0] != user_id:
                raise HTTPException(404,"资源不存在")

    def require_owner(self, kind, resource_id, user_id):
        with self.connect() as db:
            row = db.execute("SELECT user_id FROM ownership WHERE kind=? AND resource_id=?",(kind,resource_id)).fetchone()
        if not row or row[0] != user_id:
            raise HTTPException(404,"资源不存在")

    def resources(self, user_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT kind,resource_id FROM ownership WHERE user_id=? AND kind IN ('image','document') ORDER BY created DESC",(user_id,))]

    def put_task(self, record):
        with self.connect() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET user_id=excluded.user_id,body=excluded.body",(record["id"],record["user_id"],json.dumps(record,ensure_ascii=False)))

    def task(self, task_id, user_id):
        with self.connect() as db:
            row = db.execute("SELECT body FROM tasks WHERE id=? AND user_id=?",(task_id,user_id)).fetchone()
        if not row:
            raise HTTPException(404,"任务不存在")
        return json.loads(row[0])

    def tasks(self, user_id, limit=100, offset=0):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT body FROM tasks WHERE user_id=? ORDER BY rowid DESC LIMIT ? OFFSET ?",(user_id,limit,offset))]

    def active_counts(self):
        """每个用户当前排队/规划/运行的任务数（单条聚合查询，走状态索引）。"""
        with self.connect() as db:
            rows = db.execute("SELECT user_id, COUNT(*) FROM tasks WHERE json_extract(body, '$.status') "
                              "IN ('queued','planning','running') GROUP BY user_id").fetchall()
        return {row[0]: row[1] for row in rows}

    def active_tasks(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT body FROM tasks WHERE json_extract(body, '$.status') IN ('queued','planning','running')")]
