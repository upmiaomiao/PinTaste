"""Authenticated per-user Host/MCP routing. Unknown or failed tenants never share a Host."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import http.client
import hashlib
from copy import deepcopy
from contextlib import closing
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import sqlite3
import time
import uuid
import re
import unicodedata
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web


HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer',
    'transfer-encoding', 'upgrade', 'content-length', 'sec-websocket-key', 'sec-websocket-version', 'sec-websocket-extensions'}
DEFAULT_RPC = frozenset({'session/list', 'session/search', 'session/create', 'session/selectModel',
    'session/modelCatalog', 'session/canOpenWorkspacePath', 'session/rename', 'session/fork', 'session/prompt',
    'session/attachment', 'session/updateQueue', 'session/cancel', 'session/page', 'session/follow',
    'session/control', 'session/controlFrames', 'sessionSkills/list', 'workspace/archiveSession', 'workspace/follow', '$events', '$events/result'})


def agent_path(path):
    """Keep legacy upload aliases on the selected account's MCP service."""
    return any(path == prefix or path.startswith(prefix + '/') for prefix in ['/mcp', '/ppt-agent', '/materials'])


def private_write(path, content):
    """Replace one server-owned file without exposing partial credentials."""
    path = Path(path)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(content)
        os.replace(temporary, path)
        if os.name != 'nt':
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


async def bounded_body(response, limit):
    """Read through EOF without trusting chunk boundaries or Content-Length."""
    chunks, size = [], 0
    async for chunk in response.content.iter_chunked(65536):
        size += len(chunk)
        if size > limit:
            raise ValueError('Internal response exceeds limit')
        chunks.append(chunk)
    return b''.join(chunks)


def own_cgroup_memory_current():
    """Read this service's cgroup v2 charge, without accepting a caller path."""
    locations = []
    for line in Path('/proc/self/cgroup').read_text(encoding='ascii').splitlines():
        fields = line.split(':', 2)
        if len(fields) == 3 and fields[:2] == ['0', '']:
            locations.append(fields[2])
    if len(locations) != 1:
        raise ValueError('Cgroup memory accounting unavailable')
    relative = locations[0]
    if not relative.startswith('/') or '\\' in relative or '\0' in relative or any(
            part in {'.', '..'} for part in relative.split('/')):
        raise ValueError('Cgroup memory accounting unavailable')
    value = (Path('/sys/fs/cgroup') / relative.lstrip('/') / 'memory.current').read_text(encoding='ascii').strip()
    if not re.fullmatch(r'[0-9]{1,20}', value):
        raise ValueError('Cgroup memory accounting unavailable')
    current = int(value)
    if current > 2**63 - 1:
        raise ValueError('Cgroup memory accounting unavailable')
    return current


async def host_running(client, tenant, authority):
    """Exchange only this Host's launch grant and read its bounded session status."""
    from urllib.parse import urlencode
    try:
        async with client.get(tenant.host_url + '/?' + urlencode({'token': tenant.launch_token}),
                headers={'Host': authority}, allow_redirects=False, auto_decompress=True,
                timeout=ClientTimeout(total=5)) as exchange:
            cookie = '; '.join(value.split(';', 1)[0] for value in exchange.headers.getall('Set-Cookie', []))
            if exchange.status not in {200, 302, 303} or not cookie:
                return None
            await bounded_body(exchange, 65536)
        async with client.post(tenant.host_url + '/api/session/list',
                headers={'Host': authority, 'Cookie': cookie, 'Content-Type': 'application/json'},
                json={'type': 'client-request', 'rpcId': str(uuid.uuid4()), 'method': 'session/list',
                    'payload': {'args': {'_request': {}}}},
                timeout=ClientTimeout(total=5), allow_redirects=False, auto_decompress=True) as response:
            raw = await bounded_body(response, 4 * 1024 * 1024)
            if response.status != 200:
                return None
            data = json.loads(raw)
            if data.get('result', {}).get('ok') is not True:
                return None
            items = data['result']['value']['items']
            if not isinstance(items, list) or not all(isinstance(value, dict)
                    and type(value.get('running')) is bool for value in items):
                return None
            return sum(value.get('running') is True for value in items)
    except (ValueError, KeyError, TypeError, AttributeError, ClientError, TimeoutError):
        return None  # Initialization failures stay unavailable without retaining response bodies.


async def host_history(client,tenant,authority):
    """Read only this account's native session summary through its own launch grant."""
    from urllib.parse import urlencode
    async with client.get(tenant.host_url+'/?'+urlencode({'token':tenant.launch_token}),headers={'Host':authority},allow_redirects=False,auto_decompress=True,timeout=ClientTimeout(total=8))as exchange:
        cookie='; '.join(value.split(';',1)[0]for value in exchange.headers.getall('Set-Cookie',[]))
        if exchange.status not in(200,302,303)or not cookie:raise web.HTTPServiceUnavailable(text='Workspace history unavailable')
        await bounded_body(exchange,65536)
    async with client.post(tenant.host_url+'/api/session/list',headers={'Host':authority,'Cookie':cookie,'Content-Type':'application/json'},json={'type':'client-request','rpcId':str(uuid.uuid4()),'method':'session/list','payload':{'args':{'_request':{'excludeArchived':True}}}},allow_redirects=False,auto_decompress=True,timeout=ClientTimeout(total=15))as response:
        if response.status!=200:raise web.HTTPServiceUnavailable(text='Workspace history unavailable')
        value=json.loads(await bounded_body(response,4*1024*1024))
        if value.get('result',{}).get('ok')is not True:raise web.HTTPServiceUnavailable(text='Workspace history unavailable')
        items=value.get('result',{}).get('value',{}).get('items')
        if not isinstance(items,list):raise web.HTTPServiceUnavailable(text='Workspace history unavailable')
        safe=[]
        for row in items:
            if not isinstance(row,dict)or not isinstance(row.get('sessionId'),str)or not re.fullmatch(r'session-[0-9a-f-]{36}',row['sessionId'])or type(row.get('blank'))is not bool or type(row.get('running'))is not bool or type(row.get('updatedAt'))not in(int,float)or not math.isfinite(row['updatedAt']):raise web.HTTPServiceUnavailable(text='Workspace history metadata invalid')
            try:uuid.UUID(row['sessionId'][8:])
            except ValueError:raise web.HTTPServiceUnavailable(text='Workspace history metadata invalid')from None
            title=row.get('projections',{}).get('values',{}).get('title')
            safe.append({'id':row['sessionId'],'title':title[:300]if isinstance(title,str)else'工作对话','updated_at':row['updatedAt']/1000,'blank':row['blank'],'running':row['running']})
        return safe


async def initialize_workspace(client, tenant, authority):
    """Register only the server-selected account directory before publishing its Host."""
    from urllib.parse import urlencode
    try:
        async with client.get(tenant.host_url + '/?' + urlencode({'token': tenant.launch_token}),
                headers={'Host': authority}, allow_redirects=False, auto_decompress=True,
                timeout=ClientTimeout(total=5)) as exchange:
            cookie = '; '.join(value.split(';', 1)[0] for value in exchange.headers.getall('Set-Cookie', []))
            if exchange.status not in {200, 302, 303} or not cookie:
                return None
            await bounded_body(exchange, 65536)
        async with client.post(tenant.host_url + '/api/workspace/create',
                headers={'Host': authority, 'Cookie': cookie, 'Content-Type': 'application/json'},
                json={'type': 'client-request', 'rpcId': str(uuid.uuid4()), 'method': 'workspace/create',
                    'payload': {'args': {'request': {'path': tenant.workspace}}}},
                timeout=ClientTimeout(total=5), allow_redirects=False, auto_decompress=True) as response:
            raw = await bounded_body(response, 65536)
            if response.status != 200:
                return None
            result = json.loads(raw)['result']
            workspace = result['value']['workspace']
            ident = workspace['workspaceId']
            if result.get('ok') is not True or workspace['path'] != tenant.workspace or str(uuid.UUID(ident)) != ident:
                return None
            return ident
    except (ValueError, KeyError, TypeError, AttributeError, ClientError, TimeoutError):
        return None


@dataclass
class Tenant:
    user_id: str
    host_url: str
    agent_url: str
    actor_path: Path
    launch_token: str
    workspace: str
    mcp_token_path: Path | None = None
    legacy: bool = False
    processes: tuple = ()
    workspace_id: str | None = None
    last_activity: float = field(default_factory=time.monotonic)
    leases: int = 0
    retiring: bool = False
    recovery_retirement: bool = False


def probe_workspace_port(port):
    """Reject live listeners while permitting POSIX TCP TIME_WAIT after restart."""
    with socket.socket() as probe:
        if os.name == 'posix':
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('127.0.0.1', port))


class TenantManager:
    """Launch supported dsh profiles with separate homes, files and MCP credentials."""

    def __init__(self, config):
        self.config, self.tenants, self.lock = config, {}, asyncio.Lock()
        self.root = Path(config['tenant_root']).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.seed = Path(config['seed_dir']).resolve()
        if not self.seed.is_dir() or not self.root.is_absolute():
            raise ValueError('Tenant seed and root are required')
        if os.name != 'nt':
            self.root.chmod(0o700)
        self.idle_seconds = config.get('idle_seconds', 0)
        self.idle_check_seconds = config.get('idle_check_seconds', 30)
        if (type(self.idle_seconds) is not int or not 0 <= self.idle_seconds <= 86400
                or self.idle_seconds and self.idle_seconds < (1 if config.get('testing') else 60)
                or type(self.idle_check_seconds) is not int or not 1 <= self.idle_check_seconds <= 300):
            raise ValueError('Invalid workspace idle timing')
        self.idle_guard_backend = config.get('idle_guard_backend', 'sqlite')
        if self.idle_guard_backend not in {'sqlite', 'http'}:
            raise ValueError('Invalid workspace idle guard backend')
        self.idle_probe_url = None
        self.idle_database = None
        if self.idle_seconds:
            if self.idle_guard_backend == 'http':
                self.idle_probe_url = config.get('idle_probe_url')
                if self.idle_probe_url != 'http://127.0.0.1:18131/health/idle':
                    raise ValueError('Workspace idle probe requires the fixed local API')
            else:
                if config.get('idle_probe_url') is not None:
                    raise ValueError('Conflicting workspace idle guard configuration')
                self.idle_database = Path(config['idle_database_path'])
                if not self.idle_database.is_absolute():
                    raise ValueError('Workspace idle guard requires an absolute database path')
        self.idle_client = None
        self.idle_counters = {'retired': 0, 'woken': 0, 'blocked_busy': 0, 'blocked_unknown': 0, 'recovered': 0, 'cache_cleanup_failed': 0}
        self.workspace_memory_admission_bytes = config.get('workspace_memory_admission_bytes', 0)
        self.workspace_memory_reserve_bytes = config.get('workspace_memory_reserve_bytes', 0)
        if (type(self.workspace_memory_admission_bytes) is not int
                or not 0 <= self.workspace_memory_admission_bytes <= 2**63 - 1
                or type(self.workspace_memory_reserve_bytes) is not int
                or not 0 <= self.workspace_memory_reserve_bytes <= self.workspace_memory_admission_bytes):
            raise ValueError('Invalid workspace memory admission budget')
        self.workspace_memory_counters = {'blocked_capacity': 0, 'blocked_unknown': 0}
        self.workspace_memory_last_current_bytes = None

    def _check_workspace_memory_admission(self):
        """Only gate a new launch; existing workspaces and running tasks stay owned."""
        if not self.workspace_memory_admission_bytes:
            return
        try:
            current = own_cgroup_memory_current()
            if type(current) is not int or current < 0:
                raise ValueError('Invalid cgroup memory charge')
        except (OSError, ValueError, UnicodeError):
            self.workspace_memory_last_current_bytes = None
            self.workspace_memory_counters['blocked_unknown'] += 1
            raise web.HTTPServiceUnavailable(text='Workspace memory status is temporarily unavailable',
                headers={'Retry-After': '30'}) from None
        self.workspace_memory_last_current_bytes = current
        if current + self.workspace_memory_reserve_bytes > self.workspace_memory_admission_bytes:
            self.workspace_memory_counters['blocked_capacity'] += 1
            raise web.HTTPServiceUnavailable(text='Workspace memory capacity is temporarily full',
                headers={'Retry-After': '30'})

    def identity(self, user):
        ident = user.get('id')
        try:
            if str(uuid.UUID(ident)) != ident:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise web.HTTPForbidden(text='Invalid account identity') from None
        if ident == self.config['legacy_user_id'] and user.get('role') != 'admin':
            raise web.HTTPForbidden(text='Company workspace access revoked')
        return ident

    async def resolve(self, user, token, *, touch=True):
        ident = self.identity(user)
        existing = self.tenants.get(ident)
        if existing is not None and not existing.retiring:
            if any(process.returncode is not None for process in existing.processes):
                raise web.HTTPServiceUnavailable(text='Account workspace requires recovery')
            private_write(existing.actor_path, token)
            if touch:
                existing.last_activity = time.monotonic()
            return existing
        async with self.lock:
            tenant = self.tenants.get(ident)
            if tenant is None:
                if ident == self.config['legacy_user_id']:
                    legacy = self.config['legacy']
                    tenant = Tenant(ident, legacy['host_url'], legacy['agent_url'], Path(legacy['actor_path']),
                        Path(legacy['launch_token_path']).read_text(encoding='utf-8').strip(), legacy['workspace'],
                        mcp_token_path=Path(legacy['mcp_token_path']), legacy=True)
                else:
                    if self.idle_client is not None and sum(not t.legacy for t in self.tenants.values()) >= self.config['max_tenants']:
                        await self._reap_locked(self.idle_client)
                    if sum(not value.legacy for value in self.tenants.values()) >= self.config['max_tenants']:
                        raise web.HTTPServiceUnavailable(text='Workspace capacity is temporarily full')
                    self._check_workspace_memory_admission()
                    waking = (self.root / ident / 'idle-history.json').is_file()
                    tenant = await self._launch(ident, token)
                    # The successful children must remain owned even if optional
                    # summary-cache cleanup fails. Active Hosts never serve it.
                    self.tenants[ident] = tenant
                    if waking:
                        self.idle_counters['woken'] += 1
                        try:
                            (self.root / ident / 'idle-history.json').unlink(missing_ok=True)
                        except OSError:
                            self.idle_counters['cache_cleanup_failed'] += 1
                self.tenants[ident] = tenant
            if tenant.retiring or any(process.returncode is not None for process in tenant.processes):
                raise web.HTTPServiceUnavailable(text='Account workspace requires recovery')
            private_write(tenant.actor_path, token)
            if touch:
                tenant.last_activity = time.monotonic()
            return tenant

    def acquire(self, tenant):
        tenant.leases += 1

    def release(self, tenant, *, touch=False):
        tenant.leases -= 1
        if tenant.leases < 0:
            raise RuntimeError('Unbalanced workspace lease')
        if touch:
            tenant.last_activity = time.monotonic()

    def cached_history(self, user):
        """Read a sleeping account's private summary without launching its processes."""
        ident = self.identity(user)
        if ident in self.tenants or ident == self.config['legacy_user_id']:
            return None
        directory = self.root / ident
        if directory.is_symlink():
            raise web.HTTPServiceUnavailable(text='Unsafe workspace directory')
        if not (directory / 'home').exists():
            return []
        path = directory / 'idle-history.json'
        try:
            if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError()
            value = json.loads(path.read_text(encoding='utf-8'))
            items = value['items']
            if value.get('user_id') != ident or not isinstance(items, list):
                raise ValueError()
            for item in items:
                if (not isinstance(item, dict) or set(item) != {'id', 'title', 'updated_at', 'blank', 'running'}
                        or not isinstance(item['id'], str) or not re.fullmatch(r'session-[0-9a-f-]{36}', item['id'])
                        or str(uuid.UUID(item['id'][8:])) != item['id'][8:]
                        or not isinstance(item['title'], str) or len(item['title']) > 300
                        or type(item['updated_at']) not in (int, float) or not math.isfinite(item['updated_at'])
                        or type(item['blank']) is not bool or item['running'] is not False):
                    raise ValueError()
            return items
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise web.HTTPServiceUnavailable(text='Workspace history unavailable') from None

    def backend_idle(self):
        """Fail closed for unfinished work; an explicit HTTP guard never uses SQLite."""
        if self.idle_guard_backend == 'http':
            connection = None
            try:
                connection = http.client.HTTPConnection('127.0.0.1', 18131, timeout=2)
                connection.request('GET', '/health/idle', headers={'Accept': 'application/json'})
                response = connection.getresponse()
                if response.status != 200:
                    return None
                if response.getheader('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                    return None
                length = response.getheader('Content-Length')
                if length is not None and (not length.isascii() or not length.isdigit() or int(length) > 1024):
                    return None
                raw = response.read(1025)
                if len(raw) > 1024:
                    return None
                def unique_object(pairs):
                    value = dict(pairs)
                    if len(value) != len(pairs):
                        raise ValueError('Duplicate idle response fields')
                    return value
                value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_object)
                if (type(value) is not dict or set(value) != {'schema_version', 'idle'}
                        or type(value['schema_version']) is not int or value['schema_version'] != 1
                        or type(value['idle']) is not bool):
                    return None
                return value['idle']
            except (OSError, ValueError, TypeError, UnicodeError, http.client.HTTPException):
                return None
            finally:
                if connection is not None:
                    connection.close()
        try:
            with closing(sqlite3.connect(self.idle_database.as_uri() + '?mode=ro', uri=True, timeout=.2)) as db:
                db.execute('PRAGMA query_only=ON')
                for table in ('tasks', 'wuyu_workflow_jobs'):
                    terminal = ('completed', 'failed', 'cancelled', 'canceled', 'interrupted')
                    if table == 'tasks':
                        terminal += ('partial',)
                    placeholders = ','.join('?' for _ in terminal)
                    if db.execute(f"SELECT 1 FROM {table} WHERE status IS NULL OR lower(status) NOT IN "
                            f"({placeholders}) LIMIT 1", terminal).fetchone():
                        return False
            return True
        except (sqlite3.Error, OSError, ValueError, AttributeError):
            return None

    async def reap_idle(self, client):
        if not self.idle_seconds:
            return
        async with self.lock:
            await self._reap_locked(client)

    @staticmethod
    def _agent_failed_host_alive(tenant):
        """Only the owned two-process launch layout has a recoverable dead Agent.

        A dead/unknown Host cannot attest native task state and stays blocked.
        """
        return (not tenant.legacy and len(tenant.processes) == 2
            and tenant.processes[0].returncode is not None
            and tenant.processes[1].returncode is None)

    async def _reap_locked(self, client):
        eligible = [t for t in self.tenants.values() if not t.legacy and t.leases == 0
            and (t.retiring or time.monotonic() - t.last_activity >=
                (self.idle_check_seconds if self._agent_failed_host_alive(t) else self.idle_seconds))]
        if not eligible:
            return
        idle = await asyncio.to_thread(self.backend_idle)
        if idle is not True:
            self.idle_counters['blocked_busy' if idle is False else 'blocked_unknown'] += 1
            return
        authority = urlsplit(self.config['public_origin']).netloc
        for tenant in eligible:
            if tenant.retiring:
                await self._finish_idle_stop(tenant)
                continue
            failed_agent = self._agent_failed_host_alive(tenant)
            if any(p.returncode is not None for p in tenant.processes) and not failed_agent:
                self.idle_counters['blocked_unknown'] += 1
                continue
            running = await host_running(client, tenant, authority)
            if type(running) is not int or running != 0:
                self.idle_counters['blocked_busy' if type(running) is int and running != 0 else 'blocked_unknown'] += 1
                continue
            try:
                items = await host_history(client, tenant, authority)
                if any(item['running'] for item in items):
                    self.idle_counters['blocked_busy'] += 1
                    continue
                idle = await asyncio.to_thread(self.backend_idle)
                if idle is not True:
                    self.idle_counters['blocked_busy' if idle is False else 'blocked_unknown'] += 1
                    continue
                quiet_seconds = self.idle_check_seconds if failed_agent else self.idle_seconds
                if (self.tenants.get(tenant.user_id) is not tenant or tenant.leases
                        or time.monotonic() - tenant.last_activity < quiet_seconds):
                    continue
                if failed_agent and not self._agent_failed_host_alive(tenant):
                    self.idle_counters['blocked_unknown'] += 1
                    continue
                private_write(self.root / tenant.user_id / 'idle-history.json',
                    json.dumps({'user_id': tenant.user_id, 'items': items}, ensure_ascii=False))
            except (web.HTTPException, ClientError, OSError, ValueError, KeyError, TypeError, TimeoutError):
                self.idle_counters['blocked_unknown'] += 1
                continue
            tenant.retiring = True
            tenant.recovery_retirement = failed_agent
            await self._finish_idle_stop(tenant)

    async def _finish_idle_stop(self, tenant):
        """Keep the slot reserved until both children have exited and both ports are free."""
        for process in tenant.processes:
            if process.returncode is None:
                try:
                    if os.name != 'nt':
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
        for process in tenant.processes:
            try:
                await asyncio.wait_for(process.wait(), 10)
            except TimeoutError:
                self.idle_counters['blocked_unknown'] += 1
                return
        try:
            for url in (tenant.host_url, tenant.agent_url):
                probe_workspace_port(urlsplit(url).port)
        except OSError:
            self.idle_counters['blocked_unknown'] += 1
            return
        if self.tenants.get(tenant.user_id) is tenant:
            del self.tenants[tenant.user_id]
            self.idle_counters['retired'] += 1
            if tenant.recovery_retirement:
                self.idle_counters['recovered'] += 1

    def _render(self, value, replacements):
        if not isinstance(value, str):
            raise ValueError('Process arguments and environment values must be strings')
        for name, replacement in replacements.items():
            value = value.replace('@@' + name + '@@', str(replacement))
        if '@@' in value or '\0' in value:
            raise ValueError('Unresolved tenant configuration')
        return value

    async def _launch(self, ident, token):
        directory = self.root / ident
        if directory.is_symlink():
            raise web.HTTPServiceUnavailable(text='Unsafe workspace directory')
        directory.mkdir(exist_ok=True, mode=0o700)
        home, workspace = directory / 'home', directory / 'workspace'
        workspace.mkdir(exist_ok=True, mode=0o700)
        if not home.exists():
            if any(path.is_symlink() for path in self.seed.rglob('*')):
                raise ValueError('Tenant seed must not contain symlinks')
            staging = directory / ('seed-' + secrets.token_hex(8))
            shutil.copytree(self.seed, staging)
            staging.rename(home)
        for path in [directory, home, workspace]:
            if os.name != 'nt':
                path.chmod(0o700)
        actor = directory / 'actor.jwt'
        private_write(actor, token)
        values = {}
        for name in ['mcp', 'capability', 'launch']:
            path = directory / (name + '.key')
            if not path.exists():
                private_write(path, secrets.token_urlsafe(48))
            values[name.upper() + '_TOKEN'] = path.read_text(encoding='utf-8').strip()
        occupied = {urlsplit(t.host_url).port for t in self.tenants.values() if not t.legacy}
        candidates = [self.config['port_start'] + i * 2 for i in range(self.config['max_tenants'])]
        host_port = next((port for port in candidates if port not in occupied), None)
        if host_port is None:
            raise web.HTTPServiceUnavailable(text='Workspace capacity is temporarily full')
        for port in [host_port, host_port + 1]:
            try:
                probe_workspace_port(port)
            except OSError:
                raise web.HTTPServiceUnavailable(text='Workspace ports are unavailable') from None
        values.update(USER_ID=ident, HOME=home, WORKSPACE=workspace, ACTOR_PATH=actor,
            HOST_PORT=host_port, AGENT_PORT=host_port + 1)
        for relative, template in self.config.get('rendered_files', {}).items():
            path = (home / relative).resolve()
            if not path.is_relative_to(home.resolve()):
                raise ValueError('Rendered config must stay in the account home')
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_write(path, self._render(template, values))
        processes = []
        try:
            for kind in ['agent', 'host']:
                command = [self._render(argument, values) for argument in self.config[kind + '_argv']]
                if kind == 'host' and '--profile' not in command and 'web' not in command:
                    raise ValueError('Hosts must launch through a supported dsh profile')
                environment = {key: value for key, value in os.environ.items()
                    if not any(word in key.upper() for word in ['KEY', 'SECRET', 'TOKEN', 'PASSWORD']) and key != 'DSH_HOME'}
                environment.update({key: self._render(value, values) for key, value in self.config[kind + '_environment'].items()})
                # Host boot and SDK proxy hooks must not reroute private service
                # credentials, including when an old deployment override persists.
                environment = {key: value for key, value in environment.items()
                    if key.upper() not in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'FTP_PROXY',
                        'NO_PROXY', 'NODE_OPTIONS', 'NODE_USE_ENV_PROXY'} and not key.upper().startswith('GLOBAL_AGENT_')}
                environment['NO_PROXY'] = '*'
                with (directory / (kind + '.log')).open('ab') as child_log:
                    processes.append(await asyncio.create_subprocess_exec(*command, cwd=str(workspace), env=environment,
                        stdout=child_log, stderr=asyncio.subprocess.STDOUT, start_new_session=os.name != 'nt'))
            for port in [host_port, host_port + 1]:
                await self._ready(port, processes)
            tenant = Tenant(ident, f'http://127.0.0.1:{host_port}', f'http://127.0.0.1:{host_port + 1}',
                actor, values['LAUNCH_TOKEN'], str(workspace), mcp_token_path=directory / 'mcp.key', processes=tuple(processes))
            await self._ready_host(tenant)
        except BaseException:
            await self._stop(processes)
            raise
        return tenant

    async def _ready_host(self, tenant):
        """Do not publish a listening Host until authenticated RPC initialization completes."""
        try:
            async with asyncio.timeout(self.config['startup_timeout']):
                async with ClientSession(trust_env=False) as client:
                    while True:
                        if any(process.returncode is not None for process in tenant.processes):
                            raise web.HTTPServiceUnavailable(text='Account workspace did not start')
                        authority = urlsplit(self.config['public_origin']).netloc
                        if await host_running(client, tenant, authority) is not None:
                            tenant.workspace_id = await initialize_workspace(client, tenant, authority)
                            if tenant.workspace_id is not None:
                                return
                        await asyncio.sleep(.1)
        except TimeoutError:
            raise web.HTTPServiceUnavailable(text='Account workspace initialization timed out') from None

    async def _ready(self, port, processes):
        async with asyncio.timeout(self.config['startup_timeout']):
            while True:
                if any(process.returncode is not None for process in processes):
                    raise web.HTTPServiceUnavailable(text='Account workspace did not start')
                try:
                    reader, writer = await asyncio.open_connection('127.0.0.1', port)
                    writer.close(); await writer.wait_closed()
                    return
                except OSError:
                    await asyncio.sleep(.1)

    async def _stop(self, processes):
        for process in processes:
            if process.returncode is None:
                try:
                    if os.name != 'nt':
                        os.killpg(process.pid, signal.SIGTERM)
                    else:
                        process.terminate()
                except ProcessLookupError:
                    pass
        for process in processes:
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                if os.name != 'nt':
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()

    async def close(self):
        await self._stop([process for tenant in self.tenants.values() for process in tenant.processes])

    async def activity(self, client, authority):
        """Read active tenant sessions for a guarded release; uncertainty is busy."""
        result = []
        for tenant in list(self.tenants.values()):
            item = {'user_id': tenant.user_id, 'legacy': tenant.legacy, 'running': None, 'available': False}
            if self.idle_seconds:
                item.update(idle_for_seconds=round(time.monotonic() - tenant.last_activity),
                    active_requests=tenant.leases, retiring=tenant.retiring)
            if not any(process.returncode is not None for process in tenant.processes):
                count = await host_running(client, tenant, authority)
                if count is not None:
                    item.update(running=count, available=True)
            if any(process.returncode is not None for process in tenant.processes):
                recoverable = self._agent_failed_host_alive(tenant)
                item.update(failure='agent_exited' if recoverable else 'host_or_process_exited',
                    recovery='retiring_after_verified_idle' if tenant.retiring else
                        'await_disconnected_verified_idle' if recoverable else 'operator_required')
            result.append(item)
        value = {'tenants': result, 'busy': any(not item['available'] or item['running'] != 0 for item in result)}
        if self.idle_seconds:
            value['idle'] = {'seconds': self.idle_seconds, 'check_seconds': self.idle_check_seconds,
                'counters': dict(self.idle_counters), 'scope': 'disconnected_workspaces'}
        value['memory_admission'] = {'enabled': bool(self.workspace_memory_admission_bytes),
            'admission_bytes': self.workspace_memory_admission_bytes,
            'reserve_bytes': self.workspace_memory_reserve_bytes,
            'last_checked_current_bytes': self.workspace_memory_last_current_bytes,
            'counters': dict(self.workspace_memory_counters), 'scope': 'new_workspace_launches'}
        return value


async def workspace_host_cookie(client, tenant, authority):
    """Exchange only the selected account's launch grant for internal Workspace RPCs."""
    from urllib.parse import urlencode
    async with client.get(tenant.host_url + '/?' + urlencode({'token': tenant.launch_token}),
            headers={'Host': authority}, allow_redirects=False, auto_decompress=True,
            timeout=ClientTimeout(total=8)) as response:
        cookie = '; '.join(value.split(';', 1)[0] for value in response.headers.getall('Set-Cookie', []))
        await bounded_body(response, 65536)
        if response.status not in {200, 302, 303} or not cookie:
            raise web.HTTPServiceUnavailable(text='工作区服务暂不可用，请稍后重试。')
        return cookie


async def workspace_host_rpc(client, tenant, authority, cookie, method, request):
    """Call one server-selected Workspace command without exposing its filesystem path."""
    async with client.post(tenant.host_url + '/api/' + method,
            headers={'Host': authority, 'Cookie': cookie, 'Content-Type': 'application/json'},
            json={'type': 'client-request', 'rpcId': str(uuid.uuid4()), 'method': method,
                'payload': {'args': {'request': request}}},
            allow_redirects=False, auto_decompress=True, timeout=ClientTimeout(total=12)) as response:
        raw = await bounded_body(response, 2 * 1024 * 1024)
        try:
            body = json.loads(raw)
            result = body['result']
            if response.status != 200 or result.get('ok') is not True:
                raise ValueError()
            return result['value']
        except (ValueError, KeyError, TypeError, AttributeError):
            raise web.HTTPServiceUnavailable(text='工作区创建未完成，请重试；已创建的文件会保留。') from None


async def workspace_host_baseline(client, tenant, authority, cookie, origin):
    """Read one actual Host registry baseline, then close its read-only stream."""
    stream_id = str(uuid.uuid4())
    async with client.ws_connect(tenant.host_url + '/api/remote.mux',
            headers={'Host': authority, 'Cookie': cookie, 'Origin': origin},
            max_msg_size=2 * 1024 * 1024, timeout=8) as upstream:
        await upstream.send_json({'type': 'open', 'streamId': stream_id,
            'endpoint': 'workspace/follow', 'payload': {'args': {}}})
        message = await upstream.receive(timeout=8)
        try:
            body = message.json() if message.type == WSMsgType.TEXT else None
            if not isinstance(body, dict) or body.get('type') != 'item' or body.get('streamId') != stream_id:
                raise ValueError()
            frame = body['value']
            if frame.get('type') != 'baseline' or not isinstance(frame['value']['items'], list):
                raise ValueError()
            items = frame['value']['items']
            if len(items) > 1000 or any(not isinstance(row, dict)
                    or str(uuid.UUID(row['workspaceId'])) != row['workspaceId']
                    or not isinstance(row.get('path'), str) or not isinstance(row.get('title'), str)
                    for row in items):
                raise ValueError()
            await upstream.send_json({'type': 'cancel', 'streamId': stream_id})
            return items
        except (ValueError, KeyError, TypeError, AttributeError):
            raise web.HTTPServiceUnavailable(text='工作区登记状态暂不可用，请刷新后重试。') from None


class NamedWorkspaces:
    """Allocate named directories inside one account scope; keep registration receipts private."""

    def __init__(self, root, limit=100):
        self.root = Path(root).absolute() / 'workspace-registry' if root else None
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Workspace limit must be between 1 and 500')
        self.limit, self.locks = limit, {}

    def ledger_path(self, tenant, *, create=False):
        if self.root is None or str(uuid.UUID(tenant.user_id)) != tenant.user_id:
            raise web.HTTPServiceUnavailable(text='工作区服务尚未配置，请联系管理员。')
        if create:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or (self.root.exists() and self.root.resolve() != self.root):
            raise web.HTTPForbidden(text='工作区存储范围不可用。')
        path = self.root / (tenant.user_id + '.json')
        if path.is_symlink():
            raise web.HTTPForbidden(text='工作区登记范围不可用。')
        return path

    def load(self, tenant):
        path = self.ledger_path(tenant)
        if not path.exists():
            return {'version': 1, 'user_id': tenant.user_id, 'requests': {}}
        try:
            if path.stat().st_size > 512 * 1024:
                raise ValueError()
            value = json.loads(path.read_text(encoding='utf-8'))
            if value.get('version') != 1 or value.get('user_id') != tenant.user_id or not isinstance(value.get('requests'), dict) or len(value['requests']) > self.limit:
                raise ValueError()
            for key, row in value['requests'].items():
                if str(uuid.UUID(key)) != key or not isinstance(row, dict) or set(row) != {'name', 'directory_id', 'workspace_id'} or not isinstance(row['name'], str) or str(uuid.UUID(row['directory_id'])) != row['directory_id']:
                    raise ValueError()
                if row['workspace_id'] is not None and str(uuid.UUID(row['workspace_id'])) != row['workspace_id']:
                    raise ValueError()
            return value
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise web.HTTPServiceUnavailable(text='工作区登记需要检查，请联系管理员；现有文件仍保留。') from None

    @staticmethod
    def scoped_path(tenant, directory_id=None, *, create=False):
        root = Path(tenant.workspace)
        if not root.is_absolute() or not root.is_dir() or root.is_symlink() or root.resolve() != root:
            raise web.HTTPForbidden(text='工作区目录范围不可用。')
        if directory_id is None:
            return root
        if str(uuid.UUID(directory_id)) != directory_id:
            raise web.HTTPForbidden(text='工作区目录范围不匹配。')
        parent, child = root / '.wuyu-workspaces', root / '.wuyu-workspaces' / directory_id
        if create:
            # Refuse symlink substitution while opening/creating the two owned levels.
            flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
            if os.name == 'posix':
                descriptor = os.open(root, flags)
                try:
                    try:
                        os.mkdir('.wuyu-workspaces', mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    parent_fd = os.open('.wuyu-workspaces', flags, dir_fd=descriptor)
                    try:
                        try:
                            os.mkdir(directory_id, mode=0o700, dir_fd=parent_fd)
                        except FileExistsError:
                            pass
                        child_fd = os.open(directory_id, flags, dir_fd=parent_fd)
                        os.close(child_fd)
                    finally:
                        os.close(parent_fd)
                finally:
                    os.close(descriptor)
            else:
                if parent.is_symlink() or child.is_symlink():
                    raise web.HTTPForbidden(text='工作区目录范围不匹配。')
                parent.mkdir(exist_ok=True)
                child.mkdir(exist_ok=True)
        if parent.is_symlink() or child.is_symlink() or not child.is_dir() or parent.resolve() != parent or child.resolve() != child:
            raise web.HTTPForbidden(text='工作区目录范围不匹配。')
        return child

    async def create(self, client, tenant, authority, origin, payload):
        if not isinstance(payload, dict) or set(payload) != {'name', 'requestId'}:
            raise web.HTTPBadRequest(text='请填写工作区名称。')
        name, request_id = payload['name'], payload['requestId']
        if not isinstance(name, str) or not isinstance(request_id, str):
            raise web.HTTPBadRequest(text='工作区名称或创建请求无效。')
        name = unicodedata.normalize('NFC', name.strip())
        if not 1 <= len(name) <= 80 or name in {'.', '..'} or any(c in '/\\' or unicodedata.category(c).startswith('C') for c in name):
            raise web.HTTPBadRequest(text='工作区名称须为1至80字，不能包含路径或控制字符。')
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except ValueError:
            raise web.HTTPBadRequest(text='创建请求无效，请重新打开新建工作区。') from None
        async with self.locks.setdefault(tenant.user_id, asyncio.Lock()):
            ledger = self.load(tenant)
            row = ledger['requests'].get(request_id)
            if row is not None and row['name'] != name:
                raise web.HTTPConflict(text='此创建请求已经用于其他名称，请重新打开后再创建。')
            self.scoped_path(tenant)
            cookie = await workspace_host_cookie(client, tenant, authority)
            items = await workspace_host_baseline(client, tenant, authority, cookie, origin)
            if row is None:
                if any(item['name'] == name for item in ledger['requests'].values()) or any(item['title'] == name for item in items):
                    raise web.HTTPConflict(text='已有同名工作区，请使用其他名称。')
                if len(ledger['requests']) >= self.limit:
                    raise web.HTTPConflict(text='工作区数量已达上限，请联系管理员。')
                row = {'name': name, 'directory_id': str(uuid.uuid4()), 'workspace_id': None}
                ledger['requests'][request_id] = row
                private_write(self.ledger_path(tenant, create=True), json.dumps(ledger, ensure_ascii=False))
            child = self.scoped_path(tenant, row['directory_id'], create=True)
            if row['workspace_id'] is not None:
                workspace = next((item for item in items if item['workspaceId'] == row['workspace_id'] and item['path'] == str(child)), None)
                if workspace is None:
                    raise web.HTTPConflict(text='此工作区登记已变化，请刷新后检查；文件仍保留。')
                self.scoped_path(tenant, row['directory_id'])
                return {'workspaceId': workspace['workspaceId'], 'title': workspace['title']}
            else:
                workspace = (await workspace_host_rpc(client, tenant, authority, cookie, 'workspace/create', {'path': str(child)}))['workspace']
                if not isinstance(workspace, dict) or workspace.get('path') != str(child) or str(uuid.UUID(workspace['workspaceId'])) != workspace['workspaceId']:
                    raise web.HTTPServiceUnavailable(text='工作区登记未完成，请重试。')
            if workspace['title'] != name:
                workspace_id = workspace['workspaceId']
                workspace = (await workspace_host_rpc(client, tenant, authority, cookie, 'workspace/rename', {'workspaceId': workspace['workspaceId'], 'title': name}))['workspace']
                if workspace.get('workspaceId') != workspace_id:
                    raise web.HTTPServiceUnavailable(text='工作区名称未保存，请重试。')
            if workspace.get('title') != name or workspace.get('path') != str(child):
                raise web.HTTPServiceUnavailable(text='工作区名称未保存，请重试。')
            self.scoped_path(tenant, row['directory_id'])
            row['workspace_id'] = workspace['workspaceId']
            private_write(self.ledger_path(tenant, create=True), json.dumps(ledger, ensure_ascii=False))
            return {'workspaceId': workspace['workspaceId'], 'title': name}

    async def select(self, client, tenant, authority, origin, method, value):
        if method != 'session/create':
            return value
        payload = value.get('payload', {})
        args = payload.get('args', {}) if isinstance(payload, dict) else None
        request = args.get('request', {}) if isinstance(args, dict) else None
        if not isinstance(request, dict):
            raise web.HTTPBadRequest(text='新会话请求无效。')
        chosen = request.get('workspaceId')
        if chosen is None and not tenant.legacy:
            if self.root is not None:
                self.scoped_path(tenant)
            return fixed_workspace_request(method, value, tenant)
        registered = None
        if chosen is not None:
            try:
                if not isinstance(chosen, str) or str(uuid.UUID(chosen)) != chosen:
                    raise ValueError()
            except ValueError:
                raise web.HTTPForbidden(text='所选工作区不属于当前账号。') from None
            if chosen != tenant.workspace_id:
                if self.root is None:
                    if tenant.workspace_id is not None:
                        raise web.HTTPForbidden(text='所选工作区不属于当前账号。')
                else:
                    registered = next((item for item in self.load(tenant)['requests'].values() if item['workspace_id'] == chosen), None)
                if registered is None and tenant.workspace_id is not None:
                    raise web.HTTPForbidden(text='所选工作区不属于当前账号。')
        cookie = await workspace_host_cookie(client, tenant, authority)
        items = await workspace_host_baseline(client, tenant, authority, cookie, origin)
        root = self.scoped_path(tenant)
        if chosen is None:
            workspace = next((item for item in items if item['path'] == str(root)), None)
        else:
            workspace = next((item for item in items if item['workspaceId'] == chosen), None)
        if workspace is None:
            raise web.HTTPForbidden(text='所选工作区不属于当前账号，或已被移除。')
        if workspace['path'] != str(root):
            row = registered or next((item for item in self.load(tenant)['requests'].values() if item['workspace_id'] == workspace['workspaceId']), None)
            if row is None or str(self.scoped_path(tenant, row['directory_id'])) != workspace['path']:
                raise web.HTTPForbidden(text='所选工作区不在当前账号的授权范围。')
        else:
            tenant.workspace_id = workspace['workspaceId']
        copied = deepcopy(value)
        target_request = copied.setdefault('payload', {}).setdefault('args', {}).setdefault('request', {})
        target_request.pop('cwd', None)
        target_request['workspaceId'] = workspace['workspaceId']
        if not tenant.legacy:
            target_request['agentPreset'] = 'wuyu-work'
        return copied


def rpc_allowed(method, value, tenant, allowed):
    """Limit tenant APIs to conversation operations and their fixed workspace."""
    if method in {'directoryPicker/list', 'directoryPicker/createDirectory', 'directoryPicker/pick', 'workspace/create'}:
        return False
    if tenant.legacy:
        return True
    if method not in allowed:
        return False
    def safe_ids(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {'sessionId', 'parentSessionId', 'childSessionId'} and (not isinstance(child, str)
                        or not re.fullmatch(r'(?:session-)?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', child)):
                    return False
                if not safe_ids(child):
                    return False
        elif isinstance(item, list):
            return all(safe_ids(child) for child in item)
        return True
    if not safe_ids(value):
        return False
    if method == 'workspace/archiveSession':
        # Each verified tenant has its own Host registry. Expose only its
        # single-session archive operation, never workspace management.
        payload = value.get('payload')
        args = payload.get('args') if isinstance(payload, dict) else None
        request = args.get('request') if isinstance(args, dict) else None
        return (isinstance(args, dict) and set(args) == {'request'}
                and isinstance(request, dict) and set(request) == {'sessionId'}
                and isinstance(request['sessionId'], str))
    if method == 'session/create':
        payload = value.get('payload', {})
        if not isinstance(payload, dict):
            return False
        args = payload.get('args', {})
        if not isinstance(args, dict):
            return False
        request = args.get('request', {})
        if not isinstance(request, dict):
            return False
        if request.get('agentPreset', 'wuyu-work') != 'wuyu-work':
            return False
    return True


def fixed_workspace_request(method, value, tenant):
    """A shared browser cannot select a server directory or another registry entry."""
    if tenant.legacy or method != 'session/create':
        return value
    copied = deepcopy(value)
    args = copied.setdefault('payload', {}).setdefault('args', {})
    request = args.setdefault('request', {})
    request.pop('workspaceId', None)
    request['agentPreset'] = 'wuyu-work'
    if tenant.workspace_id is not None:
        request.pop('cwd', None)
        request['workspaceId'] = tenant.workspace_id
    else:
        request['cwd'] = tenant.workspace
    return copied


def create_app(config, *, resolver=None):
    manager = resolver or TenantManager(config)
    named_workspaces = NamedWorkspaces(config.get('tenant_root') or getattr(manager, 'root', None),
        config.get('max_workspaces_per_tenant', 100))
    public = urlsplit(config['public_origin'])
    if public.scheme != 'https' and not config.get('testing'):
        raise ValueError('The public origin must use HTTPS')
    allowed = frozenset(config.get('allowed_rpc', DEFAULT_RPC))
    app = web.Application(client_max_size=config['max_body'])
    control_key = Path(config['control_key_path']).read_text(encoding='utf-8').strip() if config.get('control_key_path') else None
    if control_key is not None and len(control_key) < 32:
        raise ValueError('A private control credential is required')

    def renewed_cookie(request, response):
        token = request.get('renewed_work_token')
        if token and not response.prepared:
            # Preserve unrelated upstream cookies, but a stale upstream work
            # cookie must not overwrite the freshly verified short-lived one.
            retained = []
            for value in response.headers.getall('Set-Cookie', []):
                parsed = SimpleCookie()
                try:parsed.load(value)
                except Exception:parsed = SimpleCookie()
                if 'banana_staging_auth_token' not in parsed:retained.append(value)
            response.headers.popall('Set-Cookie', None)
            for value in retained:response.headers.add('Set-Cookie', value)
            response.set_cookie('banana_staging_auth_token', token, secure=True, httponly=True,
                samesite='Lax', path='/', max_age=3500)
            response.headers['Cache-Control'] = 'no-store'
        return response

    @web.middleware
    async def identity_cookie_response(request, handler):
        try:
            try:response = await handler(request)
            except web.HTTPException as error:
                renewed_cookie(request, error)
                raise
            return renewed_cookie(request, response)
        finally:
            tenant = request.get('workspace_lease')
            if tenant is not None:
                manager.release(tenant, touch=request.get('workspace_lease_touch', False))

    app.middlewares.append(identity_cookie_response)

    async def lifecycle(application):
        application['client'] = ClientSession(timeout=ClientTimeout(total=None, sock_connect=20, sock_read=1800),
            trust_env=False, auto_decompress=False)
        idle_task = None
        if getattr(manager, 'idle_seconds', 0):
            manager.idle_client = application['client']
            async def idle_loop():
                while True:
                    await asyncio.sleep(manager.idle_check_seconds)
                    try:
                        await manager.reap_idle(application['client'])
                    except Exception:
                        manager.idle_counters['blocked_unknown'] += 1
            idle_task = asyncio.create_task(idle_loop())
        yield
        if idle_task is not None:
            idle_task.cancel()
            await asyncio.gather(idle_task, return_exceptions=True)
        await application['client'].close()
        await manager.close()
    app.cleanup_ctx.append(lifecycle)

    async def activity(request):
        supplied = request.headers.get('Authorization', '').removeprefix('Bearer ')
        if control_key is None or not hmac.compare_digest(control_key, supplied):
            raise web.HTTPUnauthorized(text='Control authentication required')
        return web.json_response(await manager.activity(app['client'], public.netloc), headers={'Cache-Control': 'no-store'})

    async def identity_history(request):
        path=config.get('identity_key_path')
        try:
            expected=Path(path).read_text(encoding='ascii').strip()if path else''
        except(OSError,ValueError):raise web.HTTPServiceUnavailable(text='Identity bridge unavailable')from None
        supplied=request.headers.get('X-Wuyu-Identity-Key','')
        if len(expected)<32 or not hmac.compare_digest(expected.encode(),supplied.encode()):raise web.HTTPUnauthorized(text='Identity bridge authentication required')
        if set(request.query)!={'work_user_id'}:raise web.HTTPBadRequest(text='Invalid history scope')
        tenant=await authenticate(request, history_only=True)
        if request.query['work_user_id']!=request['verified_work_user_id']:raise web.HTTPForbidden(text='History scope denied')
        if tenant is None:
            return web.json_response({'items': request['sleeping_history']}, headers={'Cache-Control': 'no-store'})
        try:items=await host_history(app['client'],tenant,public.netloc)
        except(ValueError,KeyError,TypeError,AttributeError,ClientError,TimeoutError):raise web.HTTPServiceUnavailable(text='Workspace history unavailable')from None
        return web.json_response({'items':items},headers={'Cache-Control':'no-store'})

    async def authenticated_user(token):
        try:
            async with app['client'].get(config['backend_url'].rstrip('/') + '/api/auth/me',
                    headers={'Authorization': 'Bearer ' + token}, allow_redirects=False,
                    auto_decompress=True, timeout=ClientTimeout(total=15)) as response:
                raw = await bounded_body(response, 65536)
                if response.status == 403:
                    raise web.HTTPForbidden(text='Account permission denied')
                if response.status == 401:
                    try:code = json.loads(raw).get('error', {}).get('code')
                    except (ValueError, TypeError, AttributeError):code = None
                    return None, code
                if response.status != 200:
                    raise web.HTTPServiceUnavailable(text='Account authentication unavailable')
                body = json.loads(raw)
                user = body['data']
                if body.get('success') is not True or not isinstance(user, dict):
                    raise ValueError()
                return user, None
        except (ClientError, TimeoutError, ValueError, KeyError, TypeError):
            raise web.HTTPServiceUnavailable(text='Account authentication unavailable') from None

    async def renew_identity(request, central):
        # This uses the central authority's live revocation/feature checks.
        # No claim from an expired or unverified work JWT selects the account.
        if request.get('auth_response_prepared'):
            raise web.HTTPUnauthorized(text='Reconnect to renew account authentication')
        raw = central.value if central else ''
        if not raw or len(raw) > 4096 or not raw.isascii() or any(char.isspace() for char in raw):
            raise web.HTTPUnauthorized(text='Central authentication required')
        try:
            path = Path(config['identity_key_path'])
            if path.stat().st_size > 4096:raise ValueError()
            key = path.read_text(encoding='ascii').strip()
            if not 32 <= len(key) <= 4096 or any(char.isspace() for char in key):raise ValueError()
            async with app['client'].post(config['backend_url'].rstrip('/') + '/api/auth/internal/identity/exchange',
                    headers={'X-Wuyu-Identity-Key': key}, json={'session_hash': hashlib.sha256(raw.encode('ascii')).hexdigest()},
                    allow_redirects=False, auto_decompress=True, timeout=ClientTimeout(total=10)) as response:
                body = json.loads(await bounded_body(response, 65536))
                if response.status == 403:
                    raise web.HTTPForbidden(text='Work access is disabled')
                if response.status == 401:
                    if isinstance(body, dict) and body.get('error', {}).get('code') == 'INTERNAL_IDENTITY_AUTH_REQUIRED':
                        raise web.HTTPServiceUnavailable(text='Identity bridge unavailable')
                    raise web.HTTPUnauthorized(text='Central authentication required')
                if response.status != 200 or not isinstance(body, dict) or set(body) != {'token', 'work_user_id'}:
                    raise web.HTTPServiceUnavailable(text='Identity bridge unavailable')
                token, ident = body['token'], body['work_user_id']
                if (not isinstance(token, str) or not token or len(token) > 16384 or any(char.isspace() for char in token)
                        or not isinstance(ident, str) or str(uuid.UUID(ident)) != ident):
                    raise ValueError()
        except (ClientError, OSError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            raise web.HTTPServiceUnavailable(text='Identity bridge unavailable') from None
        user, _ = await authenticated_user(token)
        if user is None:
            raise web.HTTPUnauthorized(text='Authentication required')
        if user.get('id') != ident:
            raise web.HTTPForbidden(text='Identity scope denied')
        request['renewed_work_token'] = token
        return user, token

    async def authenticate(request, *, history_only=False):
        cookies = SimpleCookie()
        try:
            cookies.load(request.headers.get('Cookie', ''))
        except Exception:
            raise web.HTTPUnauthorized(text='Authentication required') from None
        cookie = cookies.get('banana_staging_auth_token')
        central = cookies.get('wuyu_staging_identity_session')
        auth = request.headers.get('Authorization', '')
        bearer = auth[7:] if auth.startswith('Bearer ') else ''
        token = request.get('verified_work_token') or (cookie.value if cookie else bearer)
        cookie_actor = cookie is not None or (central is not None and not bearer)
        if cookie_actor and request.method not in {'GET', 'HEAD', 'OPTIONS'} and request.headers.get('Origin') != config['public_origin']:
            raise web.HTTPForbidden(text='Same-origin submission required')
        if request.headers.get('Origin') and request.headers['Origin'] != config['public_origin']:
            raise web.HTTPForbidden(text='Origin denied')
        can_renew = config.get('identity_enabled') is True and central is not None and cookie_actor
        if not token:
            if not can_renew:raise web.HTTPUnauthorized(text='Authentication required')
            user, token = await renew_identity(request, central)
        else:
            if len(token) > 16384 or any(char.isspace() for char in token):
                raise web.HTTPUnauthorized(text='Authentication required')
            user, code = await authenticated_user(token)
            if user is None:
                if not can_renew or code != 'TOKEN_EXPIRED':
                    raise web.HTTPUnauthorized(text='Authentication required')
                user, token = await renew_identity(request, central)
        request['verified_work_token'] = token
        request['verified_work_user_id'] = user.get('id')
        if getattr(manager, 'idle_seconds', 0):
            if history_only:
                cached = manager.cached_history(user)
                if cached is not None:
                    request['sleeping_history'] = cached
                    return None
            tenant = await manager.resolve(user, token, touch=not history_only)
            if request.get('workspace_lease') is None:
                manager.acquire(tenant)
                request['workspace_lease'] = tenant
            request['workspace_lease_touch'] = request.get('workspace_lease_touch', False) or not history_only
            return tenant
        return await manager.resolve(user, token)

    async def create_named_workspace(request):
        tenant = await authenticate(request)
        if request.content_type != 'application/json':
            raise web.HTTPBadRequest(text='工作区请求必须使用 JSON 格式。')
        raw = bytearray()
        async for chunk in request.content.iter_chunked(2048):
            raw.extend(chunk)
            if len(raw) > 4096:
                raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(raw))
        try:
            payload = json.loads(raw)
        except ValueError:
            raise web.HTTPBadRequest(text='工作区请求格式无效。') from None
        try:
            value = await named_workspaces.create(app['client'], tenant, public.netloc, config['public_origin'], payload)
            return web.json_response(value, headers={'Cache-Control': 'no-store'})
        except (ClientError, OSError, TimeoutError, ValueError, KeyError, TypeError, AttributeError):
            raise web.HTTPServiceUnavailable(text='工作区创建未完成，请重试；现有文件仍保留。') from None

    def headers_for(request, tenant=None, *, agent=False):
        headers = {key: value for key, value in request.headers.items() if key.lower() not in HOP_HEADERS
            and not key.lower().startswith('x-wuyu-') and key.lower() not in {'host', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto'}
            and (agent or key.lower() != 'authorization')}
        headers['Host'] = public.netloc
        headers['X-Forwarded-Proto'] = public.scheme
        if agent:
            # Browser bundles are shared. They never select or receive the
            # private MCP credential belonging to the verified account.
            try:
                if tenant is None or tenant.mcp_token_path is None or tenant.mcp_token_path.stat().st_size > 16384:
                    raise ValueError()
                token = tenant.mcp_token_path.read_text(encoding='utf-8').strip()
                if not token or any(character.isspace() for character in token):
                    raise ValueError()
            except (OSError, ValueError):
                raise web.HTTPServiceUnavailable(text='Account service authentication unavailable') from None
            headers = {key: value for key, value in headers.items() if key.lower() != 'authorization'}
            headers['Authorization'] = 'Bearer ' + token
        return headers

    async def websocket(request, tenant, target):
        headers = headers_for(request)
        async with app['client'].ws_connect(target, headers=headers, max_msg_size=config['max_body'], autoping=True) as upstream:
            downstream = web.WebSocketResponse(max_msg_size=config['max_body'], heartbeat=30)
            renewed_cookie(request, downstream)
            await downstream.prepare(request)
            request['auth_response_prepared'] = True
            async def inbound():
                async for message in downstream:
                    if message.type == WSMsgType.TEXT:
                        try:
                            value = json.loads(message.data)
                            if not isinstance(value, dict) or value.get('type') not in {'open', 'cancel'}:
                                raise ValueError()
                            if value.get('type') == 'open' and not rpc_allowed(value.get('endpoint'), value, tenant, allowed):
                                raise ValueError()
                        except (ValueError, TypeError):
                            await downstream.close(code=1008, message=b'Conversation operation denied')
                            return
                        if value.get('type')=='open'and value.get('endpoint')!='session/cancel':
                            try:
                                verified=await authenticate(request)
                                if verified.user_id!=tenant.user_id:raise web.HTTPUnauthorized()
                            except(web.HTTPException,ClientError,TimeoutError):
                                await downstream.close(code=1008,message=b'Account permission changed');return
                        try:
                            value = await named_workspaces.select(app['client'], tenant, public.netloc,
                                config['public_origin'], value.get('endpoint'), value)
                        except (web.HTTPException, ClientError, OSError, TimeoutError):
                            await downstream.close(code=1008, message=b'Workspace scope unavailable')
                            return
                        await upstream.send_str(json.dumps(value, ensure_ascii=False, separators=(',', ':')))
                    elif message.type == WSMsgType.BINARY:
                        await downstream.close(code=1008, message=b'Text protocol required')
                        return
            async def outbound():
                async for message in upstream:
                    if message.type == WSMsgType.TEXT:
                        await downstream.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await downstream.send_bytes(message.data)
            tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await downstream.close()
            return downstream

    async def proxy(request):
        active_response = None
        try:
            tenant = await authenticate(request)
            agent = agent_path(request.path)
            path = request.path.removeprefix('/ppt-agent') if agent else request.path
            if request.headers.get('Upgrade', '').lower() == 'websocket':
                if path != '/api/remote.mux' or agent:
                    raise web.HTTPForbidden(text='WebSocket operation denied')
                return await websocket(request, tenant, tenant.host_url + path)
            if not agent and (path.startswith('/api/directoryPicker/') or path == '/api/workspace/create'):
                raise web.HTTPForbidden(text='请使用工作区名称创建；不支持浏览服务器目录。')
            raw = None
            if not tenant.legacy and path.startswith('/api/'):
                method = path.removeprefix('/api/')
                if path in {'/api/wuyu/material-context', '/api/wuyu/presentation-settings'}:
                    pass
                elif method not in allowed:
                    raise web.HTTPForbidden(text='Conversation operation denied')
                else:
                    raw = await request.read()
                    try:
                        value = json.loads(raw)
                    except ValueError:
                        raise web.HTTPBadRequest(text='Invalid conversation request') from None
                    if not isinstance(value, dict) or value.get('method') != method or not rpc_allowed(method, value, tenant, allowed):
                        raise web.HTTPForbidden(text='Conversation operation denied')
                    value = await named_workspaces.select(app['client'], tenant, public.netloc,
                        config['public_origin'], method, value)
                    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            elif tenant.legacy and path == '/api/session/create':
                raw = await request.read()
                try:
                    value = json.loads(raw)
                except ValueError:
                    raise web.HTTPBadRequest(text='新会话请求格式无效。') from None
                if not isinstance(value, dict) or value.get('method') != 'session/create':
                    raise web.HTTPBadRequest(text='新会话请求格式无效。')
                value = await named_workspaces.select(app['client'], tenant, public.netloc,
                    config['public_origin'], 'session/create', value)
                raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            origin = tenant.agent_url if agent else tenant.host_url
            target = origin.rstrip('/') + path + ('?' + request.query_string if request.query_string else '')
            headers = headers_for(request, tenant, agent=agent)
            body = raw if raw is not None else request.content.iter_chunked(65536) if request.can_read_body else None
            response = await app['client'].request(request.method, target, data=body, headers=headers, allow_redirects=False)
            # Exchange only this account's launch token, entirely on the server.
            if not agent and request.method == 'GET' and path == '/' and response.status == 401:
                response.release()
                from urllib.parse import urlencode
                query = dict(request.query); query['token'] = tenant.launch_token
                response = await app['client'].get(origin.rstrip('/') + '/?' + urlencode(query), headers=headers, allow_redirects=False)
            async with response:
                result_headers = [(key, value) for key, value in response.headers.items() if key.lower() not in HOP_HEADERS]
                stream = web.StreamResponse(status=response.status, headers=result_headers)
                active_response = stream
                renewed_cookie(request, stream)
                await stream.prepare(request)
                async for chunk in response.content.iter_chunked(65536):
                    await stream.write(chunk)
                await stream.write_eof()
                return stream
        except web.HTTPUnauthorized:
            if request.method == 'GET' and not request.path.startswith('/api/') and not agent_path(request.path):
                if config.get('identity_enabled')and request.cookies.get('wuyu_staging_identity_session'):
                    raise web.HTTPFound('/api/auth/sso')from None
                raise web.HTTPFound(config.get('identity_login_url','/login')) from None
            raise
        except (ClientError, OSError, TimeoutError):
            if active_response is not None and active_response.prepared:
                if request.transport is not None:
                    request.transport.close()
                raise asyncio.CancelledError() from None
            raise web.HTTPServiceUnavailable(text='Account workspace temporarily unavailable') from None

    app.router.add_get('/__internal/activity', activity)
    app.router.add_get('/__internal/identity/history',identity_history)
    app.router.add_post('/api/workspace/named', create_named_workspace)
    app.router.add_route('*', '/{all:.*}', proxy)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    app = create_app(config)
    web.run_app(app, host=config['host'], port=config['port'], access_log=None, print=None, handler_cancellation=True)


if __name__ == '__main__':
    main()
