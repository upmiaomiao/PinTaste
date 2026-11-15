"""Single-process, authenticated, bounded multi-Windows job gateway.

Desktop work is serialized per node. Files are copied into gateway storage
before completion, so later requests never randomly land on another worker.
This intentionally uses one uvicorn worker; no process-local lock is claimed
to coordinate multiple gateway processes.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Literal
from urllib.parse import urlsplit
import uuid

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ROOT = Path(__file__).resolve().parents[1]
OPTICAL = {'bing', 'amap', 'tencent', 'google', 'mapbox', 'mapquest', 'arcgis',
           'tianditu', 'siwei', 'siwei_daily'}
MULTI = {'sentinel2', 'landsat', 'hls_s30', 'hls_l30'}
HYPER = {'emit', 'enmap'}
SAFE_ID = r'^[A-Za-z0-9_-]{1,64}$'
Operation = Literal['optical.latest', 'multispectral.search', 'multispectral.subset',
                    'multispectral.asset', 'hyperspectral.search', 'hyperspectral.asset',
                    'sar.search', 'sar.assets', 'sar.crop', 'sar.preview']


def error(status: int, code: str, message: str):
    return HTTPException(status, detail={'code': code, 'message': message})


class Node(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=SAFE_ID)
    base_url: str
    api_key: str = Field(min_length=32, repr=False)
    optical_sources: list[str] = Field(default_factory=lambda: sorted(OPTICAL - {'tianditu', 'siwei', 'siwei_daily'}))
    science_slots: int = Field(default=2, ge=1, le=4)
    enabled: bool = True

    @model_validator(mode='after')
    def validate_node(self):
        u = urlsplit(self.base_url)
        if u.scheme not in {'http', 'https'} or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in {'', '/'}:
            raise ValueError('Node URL must be an HTTP(S) origin without credentials or path')
        # Plain HTTP is restricted to explicitly configured private LAN/loopback.
        if u.scheme == 'http':
            import ipaddress
            try:
                ip = ipaddress.ip_address(u.hostname)
                if not (ip.is_private or ip.is_loopback):
                    raise ValueError('Public nodes require HTTPS')
            except ValueError:
                if u.hostname != 'localhost':
                    raise ValueError('HTTP nodes require a private IP or localhost') from None
        if any(s not in OPTICAL for s in self.optical_sources):
            raise ValueError('Unknown optical source in node configuration')
        if self.enabled and self.api_key.startswith('REPLACE_'):
            raise ValueError('Enabled node requires its real worker key')
        self.base_url = self.base_url.rstrip('/')
        return self


class Client(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=SAFE_ID)
    api_key: str = Field(min_length=32, repr=False)

    @model_validator(mode='after')
    def reject_placeholder(self):
        if self.api_key.startswith('REPLACE_'):
            raise ValueError('Client requires a generated private key')
        return self


class Cluster(BaseModel):
    model_config = ConfigDict(extra='forbid')
    nodes: list[Node] = Field(min_length=1, max_length=12)
    clients: list[Client] = Field(min_length=1, max_length=100)
    max_pending: int = Field(default=50, ge=1, le=1000)
    per_client_pending: int = Field(default=10, ge=1, le=100)
    per_client_running: int = Field(default=3, ge=1, le=16)
    per_client_requests_per_minute: int = Field(default=60, ge=1, le=10000)
    retention_seconds: int = Field(default=86400, ge=60, le=604800)
    max_storage_mb: int = Field(default=2048, ge=1, le=32768)
    allowed_origins: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_ids(self):
        for items in (self.nodes, self.clients):
            if len({i.id for i in items}) != len(items):
                raise ValueError('Duplicate node/client ID')
        if len({c.api_key for c in self.clients}) != len(self.clients):
            raise ValueError('Client keys must be distinct')
        if not any(n.enabled for n in self.nodes):
            raise ValueError('At least one node must be enabled')
        if '*' in self.allowed_origins:
            raise ValueError('Use explicit website origins')
        return self


class AssetRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str
    scene_id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,220}$')
    asset_id: str = Field(pattern=r'^[A-Za-z0-9_.-]{1,220}$')


class JobRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Operation
    parameters: dict[str, Any]
    deadline_seconds: int = Field(default=180, ge=5, le=600)

    @model_validator(mode='after')
    def validate_parameters(self):
        from .optical import OpticalRequest
        from .spectral import SpectralSearchRequest, SpectralSubsetRequest
        from .sar import SarSearchRequest, SarCropRequest, SarAssetRequest
        op = self.operation
        if op == 'optical.latest':
            params = dict(self.parameters)
            source = params.get('source', 'bing')
            if source not in OPTICAL or 'map_type' in params or 'reference_image_id' in params:
                raise ValueError('Use a supported logical source; map_type is node-local')
            self.parameters = OpticalRequest.model_validate(params).model_dump(
                mode='json', exclude={'map_type', 'reference_image_id'}, exclude_none=True)
            if source == 'arcgis' and self.parameters['coord_type'] != 0:
                raise ValueError('ArcGIS requires WGS84 coordinates')
        else:
            cls = (SpectralSearchRequest if op.endswith('.search') and not op.startswith('sar.') else
                   SpectralSubsetRequest if op == 'multispectral.subset' else
                   AssetRequest if op.endswith('.asset') else
                   SarSearchRequest if op == 'sar.search' else
                   SarCropRequest if op == 'sar.crop' else SarAssetRequest)
            params = dict(self.parameters)
            if op.startswith('multispectral.'):
                params.setdefault('source', 'sentinel2')
            elif op.startswith('hyperspectral.'):
                params.setdefault('source', 'emit')
            self.parameters = cls.model_validate(params).model_dump(mode='json')
            source = self.parameters['source']
            if op.startswith('multispectral.') and source not in MULTI or op.startswith('hyperspectral.') and source not in HYPER:
                raise ValueError('Source does not match the spectral API group')
        return self


class Runtime:
    def __init__(self, config: Cluster, folder: Path, transport=None):
        self.config, self.folder, self.transport = config, folder, transport
        folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(folder / 'jobs.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, owner TEXT, operation TEXT, payload TEXT,
            state TEXT, created REAL, deadline REAL, node TEXT, result TEXT,
            failure TEXT, file TEXT, media TEXT, idem TEXT, fingerprint TEXT,
            UNIQUE(owner,idem))''')
        self.db.execute("UPDATE jobs SET state='failed', failure=? WHERE state IN ('queued','running')",
                        (json.dumps({'code': 'gateway_restarted', 'message': 'Interrupted jobs are not automatically replayed.'}),))
        self.db.commit()
        self.nodes = {n.id: {'config': n, 'online': False, 'desktop_ready': False,
                            'desktop_checked_at': None, 'optical': 0, 'science': 0, 'cooldown': 0}
                      for n in config.nodes if n.enabled}
        self.tasks: set[asyncio.Task] = set()
        self.http = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                     limits=httpx.Limits(max_connections=32))
        self.rotation = 0
        self.submissions: dict[str, list[float]] = {}

    def row(self, job_id: str):
        return self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()

    def update(self, job_id: str, **values):
        # Column names are internal constants; user data is always bound.
        allowed = {'state', 'node', 'result', 'failure', 'file', 'media'}
        assert values.keys() <= allowed
        self.db.execute('UPDATE jobs SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?',
                        (*values.values(), job_id))
        self.db.commit()

    def submit(self, owner: str, body: JobRequest, idem: str | None):
        payload = json.dumps(body.parameters, sort_keys=True, separators=(',', ':'))
        fingerprint = hashlib.sha256((body.operation + payload).encode()).hexdigest()
        if idem:
            existing = self.db.execute('SELECT * FROM jobs WHERE owner=? AND idem=?', (owner, idem)).fetchone()
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise error(409, 'idempotency_conflict', 'This key was used for a different request.')
                return existing['id']
        now = time.time()
        recent = [t for t in self.submissions.get(owner, []) if now-t < 60]
        if len(recent) >= self.config.per_client_requests_per_minute:
            raise error(429, 'submission_rate_limit', 'Per-client task submission rate exceeded.')
        if body.operation == 'optical.latest' and not any(body.parameters['source'] in s['config'].optical_sources for s in self.nodes.values()):
            raise error(409, 'source_not_configured', 'No enabled node is configured for this optical source.')
        active = self.db.execute("SELECT owner FROM jobs WHERE state IN ('queued','running')").fetchall()
        if len(active) >= self.config.max_pending or sum(r['owner'] == owner for r in active) >= self.config.per_client_pending:
            raise error(429, 'queue_full', 'Pending task limit reached; retry after earlier jobs finish.')
        job_id, now = uuid.uuid4().hex, time.time()
        self.db.execute('INSERT INTO jobs(id,owner,operation,payload,state,created,deadline,idem,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)',
                        (job_id, owner, body.operation, payload, 'queued', now, now+body.deadline_seconds, idem, fingerprint))
        self.db.commit()
        self.submissions[owner] = recent+[now]
        return job_id

    async def probe(self):
        async def one(state):
            node = state['config']
            headers = {'Authorization': 'Bearer '+node.api_key, 'ngrok-skip-browser-warning': '1'}
            try:
                r = await self.http.get(node.base_url+'/health', headers=headers, timeout=3)
                state['online'] = r.status_code == 200 and r.json().get('status') == 'ok'
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                state['online'] = False
            if not state['online']:
                state['desktop_ready'] = False
                return
            if not state['optical']:
                # Omap readiness is a separate capability probe. A slow,
                # unavailable or malformed desktop response must not mark the
                # healthy HTTP worker offline and block science/Esri jobs.
                try:
                    r = await self.http.get(node.base_url+'/api/current', headers=headers, timeout=3)
                    state['desktop_ready'] = r.status_code == 200 and isinstance(r.json().get('center'), list)
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    state['desktop_ready'] = False
                state['desktop_checked_at'] = time.time()
        await asyncio.gather(*(one(s) for s in self.nodes.values()))

    async def scheduler(self):
        next_probe = 0
        probe_task = None
        try:
            while True:
                now = time.time()
                if now >= next_probe and (probe_task is None or probe_task.done()):
                    probe_task = asyncio.create_task(self.probe())
                    next_probe = now+10
                    self.cleanup(now)
                queued = self.db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created").fetchall()
                for row in queued:
                    if row['deadline'] <= now:
                        self.update(row['id'], state='failed', failure=json.dumps({'code': 'deadline_exceeded', 'message': 'Expired while waiting for an available node.'}))
                        continue
                    running = self.db.execute("SELECT count(*) FROM jobs WHERE owner=? AND state='running'", (row['owner'],)).fetchone()[0]
                    if running >= self.config.per_client_running:
                        continue
                    optical = row['operation'] == 'optical.latest'
                    kind = 'optical' if optical else 'science'
                    params = json.loads(row['payload'])
                    candidates = list(self.nodes.values())
                    candidates = candidates[self.rotation % len(candidates):]+candidates[:self.rotation % len(candidates)]
                    chosen = next((s for s in candidates if s['online'] and s['cooldown'] <= now and
                                   s[kind] < (1 if optical else s['config'].science_slots) and
                                   (not optical or params['source'] in s['config'].optical_sources and
                                    (params['source'] == 'arcgis' or s['desktop_ready']))), None)
                    if chosen:
                        chosen[kind] += 1
                        self.rotation += 1
                        self.update(row['id'], state='running', node=chosen['config'].id)
                        task = asyncio.create_task(self.execute(row, chosen, kind))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                await asyncio.sleep(.05)
        finally:
            if probe_task:
                probe_task.cancel()
                await asyncio.gather(probe_task, return_exceptions=True)

    async def read(self, node: Node, method: str, path: str, *, payload=None, params=None, timeout=120, maximum=5*1024*1024):
        headers = {'Authorization': 'Bearer '+node.api_key, 'ngrok-skip-browser-warning': '1'}
        async with self.http.stream(method, node.base_url+path, json=payload, params=params, headers=headers, timeout=timeout) as r:
            content = bytearray()
            async for block in r.aiter_bytes():
                content.extend(block)
                if len(content) > maximum:
                    raise error(502, 'worker_response_too_large', 'Worker response exceeds the configured size limit.')
            if r.status_code != 200:
                detail = {}
                try:
                    raw = json.loads(content).get('detail', {})
                    if isinstance(raw, dict):
                        detail = {k: raw[k] for k in ('code', 'upstream_code') if k in raw}
                except (ValueError, AttributeError):
                    pass
                raise HTTPException(502, detail={'code': 'worker_rejected', 'upstream_status': r.status_code, 'detail': detail})
            return bytes(content), r.headers.get('content-type', '').split(';')[0]

    async def execute(self, row, state, kind):
        node, job_id = state['config'], row['id']
        try:
            remaining = row['deadline']-time.time()
            async with asyncio.timeout(remaining):
                params = json.loads(row['payload'])
                op = row['operation']
                paths = {'optical.latest': '/api/optical/latest',
                         'multispectral.search': '/api/spectral/search', 'hyperspectral.search': '/api/spectral/search',
                         'multispectral.subset': '/api/spectral/subset',
                         'multispectral.asset': '/api/spectral/asset', 'hyperspectral.asset': '/api/spectral/asset',
                         'sar.search': '/api/sar/search', 'sar.assets': '/api/sar/assets',
                         'sar.crop': '/api/sar/crop', 'sar.preview': '/api/sar/preview'}
                is_get = op.endswith('.asset') or op == 'sar.preview'
                if op == 'sar.preview':
                    params.pop('sign', None)
                if op == 'optical.latest':
                    params['timeout_seconds'] = min(params['timeout_seconds'], max(1, remaining-2))
                content, media = await self.read(node, 'GET' if is_get else 'POST', paths[op],
                    params=params if is_get else None, payload=None if is_get else params,
                    timeout=min(180, remaining), maximum=32*1024*1024 if op == 'sar.preview' else 5*1024*1024)
                result = {'preview_is_analysis_data': False} if op == 'sar.preview' else json.loads(content)
                if not isinstance(result, dict):
                    raise ValueError('Worker result must be an object')
                file = None
                if op != 'sar.preview':
                    file_url = result.get('image_url') or result.get('file_url')
                    if file_url:
                        if not re.fullmatch(r'/api/(?:images/[0-9a-f]{32}\.png|spectral/files/[0-9a-f]{32}\.tif|sar/files/[0-9a-f]{32}\.tif)', file_url):
                            raise error(502, 'invalid_worker_file', 'Worker returned an unsupported local file path.')
                        content, media = await self.read(node, 'GET', file_url, timeout=120, maximum=64*1024*1024)
                        file = self.save_file(job_id, content, media)
                        result['file_url' if 'file_url' in result else 'image_url'] = f'/api/jobs/{job_id}/file'
                else:
                    file = self.save_file(job_id, content, media)
                    result['file_url'] = f'/api/jobs/{job_id}/file'
                # Worker preview_api is relative to the worker and not a public
                # proxy path. Provide a new task request instead.
                for item in result.get('items', []):
                    if isinstance(item, dict) and 'preview_api' in item:
                        item.pop('preview_api')
                        item['preview_request'] = {'operation': 'sar.preview', 'parameters': {'source': item['source'], 'item_id': item['id']}}
                self.update(job_id, state='succeeded', result=json.dumps(result, ensure_ascii=False), file=file, media=media if file else None)
        except HTTPException as exc:
            self.update(job_id, state='failed', failure=json.dumps(exc.detail))
        except (TimeoutError, httpx.HTTPError):
            state['cooldown'] = time.time()+30
            self.update(job_id, state='failed', failure=json.dumps({'code': 'worker_timeout_or_connection', 'message': 'Worker did not complete before the deadline. The job was not replayed elsewhere.'}))
        except asyncio.CancelledError:
            self.update(job_id, state='failed', failure=json.dumps({'code': 'gateway_stopped'}))
            raise
        except (ValueError, OSError, TypeError, KeyError):
            self.update(job_id, state='failed', failure=json.dumps({'code': 'invalid_worker_result', 'message': 'Worker response or gateway file storage failed.'}))
        except Exception:
            # A malformed upstream value must not leave a job running forever;
            # never publish exception strings containing configured credentials.
            self.update(job_id, state='failed', failure=json.dumps({'code': 'gateway_execution_failed'}))
        finally:
            state[kind] -= 1

    def save_file(self, job_id, content, media):
        if media in {'image/png', 'image/jpeg'}:
            with Image.open(io.BytesIO(content)) as image:
                image.verify()
            ext = '.png' if media == 'image/png' else '.jpg'
        elif media in {'image/tiff', 'application/geotiff', 'application/octet-stream'} and content[:4] in {b'II*\x00', b'MM\x00*', b'II+\x00', b'MM\x00+'}:
            ext = '.tif'
        else:
            raise ValueError('Unsupported gateway artifact')
        usage = sum(p.stat().st_size for p in self.folder.glob('*.*') if p.suffix in {'.png', '.jpg', '.tif'})
        if usage+len(content) > self.config.max_storage_mb*1024*1024:
            raise error(507, 'storage_limit', 'Gateway artifact storage is full.')
        filename = job_id+ext
        tmp = self.folder/(filename+'.tmp')
        tmp.write_bytes(content)
        tmp.replace(self.folder/filename)
        return filename

    def cleanup(self, now):
        rows = self.db.execute("SELECT id,file FROM jobs WHERE created<? AND state NOT IN ('queued','running')", (now-self.config.retention_seconds,)).fetchall()
        for row in rows:
            filename = row['file']
            if filename and re.fullmatch(r'[0-9a-f]{32}\.(?:png|jpg|tif)', filename):
                (self.folder/filename).unlink(missing_ok=True)
            self.db.execute('DELETE FROM jobs WHERE id=?', (row['id'],))
        self.db.commit()


def create_gateway(config: Cluster | dict, folder: Path | str, transport=None):
    config = config if isinstance(config, Cluster) else Cluster.model_validate(config)
    @asynccontextmanager
    async def lifespan(app):
        runtime = Runtime(config, Path(folder), transport)
        app.state.runtime = runtime
        task = asyncio.create_task(runtime.scheduler())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for work in list(runtime.tasks):
                work.cancel()
            await asyncio.gather(*runtime.tasks, return_exceptions=True)
            await runtime.http.aclose()
            runtime.db.close()

    app = FastAPI(title='多节点遥感 API 网关', version='0.3.0', lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=config.allowed_origins, allow_methods=['GET', 'POST'],
                       allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key'], allow_credentials=False)
    bearer = HTTPBearer(auto_error=False)
    async def owner(auth: HTTPAuthorizationCredentials | None = Depends(bearer)):
        candidate = auth.credentials if auth and auth.scheme.lower() == 'bearer' else ''
        for client in config.clients:
            if hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), hashlib.sha256(client.api_key.encode()).digest()):
                return client.id
        raise HTTPException(401, detail={'code': 'invalid_api_key'}, headers={'WWW-Authenticate': 'Bearer'})

    @app.middleware('http')
    async def limit_body(request: Request, call_next):
        if request.method == 'POST':
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 65536:
                    from fastapi.responses import JSONResponse
                    return JSONResponse({'detail': {'code': 'request_too_large'}}, status_code=413)
            request._body = bytes(body)
        return await call_next(request)

    def response(job_id):
        return {'job_id': job_id, 'status_url': f'/api/jobs/{job_id}', 'file_url': f'/api/jobs/{job_id}/file', 'poll_after_seconds': 2}

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'service': 'imagery_gateway'}

    @app.get('/api/nodes')
    async def nodes(request: Request, client=Depends(owner)):
        return {'nodes': [{'id': n['config'].id, 'http_online': n['online'], 'desktop_ready': n['desktop_ready'],
                          'desktop_checked_at': n['desktop_checked_at'], 'optical_active': n['optical'],
                          'optical_capacity': 1, 'science_active': n['science'], 'science_capacity': n['config'].science_slots,
                          'optical_sources': n['config'].optical_sources} for n in request.app.state.runtime.nodes.values()],
                'note': 'Desktop readiness is a recent connection probe; source permissions and image delivery are checked per job.'}

    for group in ('optical', 'multispectral', 'hyperspectral', 'sar'):
        def catalogue_endpoint(name):
            async def catalogue(request: Request, client=Depends(owner)):
                runtime = request.app.state.runtime
                if name == 'optical':
                    from .optical import SOURCE_SPECS
                    items = [{'source': s, 'name': spec[1], 'provider': spec[2],
                              'requires_desktop': s != 'arcgis',
                              'configured_nodes': [n['config'].id for n in runtime.nodes.values() if s in n['config'].optical_sources]}
                             for s, spec in SOURCE_SPECS.items()]
                elif name in {'multispectral', 'hyperspectral'}:
                    from .spectral import SOURCES, _public_source
                    allowed = MULTI if name == 'multispectral' else HYPER
                    items = [_public_source(v) for k, v in SOURCES.items() if k in allowed]
                else:
                    from .sar import SOURCES
                    items = [{'source': k, **v} for k, v in SOURCES.items()]
                return {'group': name, 'sources': items,
                        'optical_alignment': ({'automatic_reference': 'arcgis', 'registration_modes': ['context', 'strict'],
                            'scope': 'per_job_relative_optical_alignment_only',
                            'default_spatial_registration': 'unknown', 'absolute_survey_accuracy_verified': False}
                            if name == 'optical' else None),
                        'note': 'Configured capability catalogue; actual coverage, account access and upstream availability are checked per task.'}
            return catalogue
        app.add_api_route(f'/api/{group}/sources', catalogue_endpoint(group), methods=['GET'], name=group+'.sources')

    @app.post('/api/jobs', status_code=202)
    async def submit(body: JobRequest, request: Request, client=Depends(owner), idempotency_key: str | None = Header(default=None)):
        if idempotency_key and not re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', idempotency_key):
            raise error(422, 'invalid_idempotency_key', 'Use at most 96 safe ASCII characters.')
        return response(request.app.state.runtime.submit(client, body, idempotency_key))

    @app.get('/api/jobs/{job_id}')
    async def status(job_id: str, request: Request, client=Depends(owner)):
        row = request.app.state.runtime.row(job_id)
        if not row or row['owner'] != client:
            raise error(404, 'job_not_found', 'Task not found.')
        return {'job_id': row['id'], 'state': row['state'], 'operation': row['operation'], 'node': row['node'],
                'created_at_unix': row['created'], 'deadline_at_unix': row['deadline'],
                'result': json.loads(row['result']) if row['result'] else None,
                'error': json.loads(row['failure']) if row['failure'] else None}

    @app.get('/api/jobs/{job_id}/file')
    async def file(job_id: str, request: Request, client=Depends(owner)):
        runtime = request.app.state.runtime
        row = runtime.row(job_id)
        if not row or row['owner'] != client:
            raise error(404, 'job_not_found', 'Task not found.')
        if row['state'] != 'succeeded' or not row['file']:
            raise error(409, 'file_not_ready', 'This task has no completed local image file.')
        path = runtime.folder/row['file']
        if not path.is_file() or path.is_symlink():
            raise error(404, 'file_expired', 'Result file is no longer available.')
        return FileResponse(path, media_type=row['media'], filename=row['file'], headers={'Cache-Control': 'private, no-store'})

    # Four product API groups share the same task protocol; historical routes
    # intentionally have no public gateway entry point.
    for public, operation in {'/api/optical/latest': 'optical.latest',
        '/api/multispectral/search': 'multispectral.search', '/api/multispectral/subset': 'multispectral.subset',
        '/api/multispectral/asset': 'multispectral.asset', '/api/hyperspectral/search': 'hyperspectral.search',
        '/api/hyperspectral/asset': 'hyperspectral.asset', '/api/sar/search': 'sar.search',
        '/api/sar/assets': 'sar.assets', '/api/sar/crop': 'sar.crop', '/api/sar/preview': 'sar.preview'}.items():
        def endpoint(op):
            async def invoke(parameters: dict, request: Request, client=Depends(owner),
                             idempotency_key: str | None = Header(default=None), deadline_seconds: int = 180):
                try:
                    body = JobRequest(operation=op, parameters=parameters, deadline_seconds=deadline_seconds)
                except ValidationError:
                    raise error(422, 'invalid_parameters', 'Parameters do not satisfy this operation schema.') from None
                return await submit(body, request, client, idempotency_key)
            return invoke
        app.add_api_route(public, endpoint(operation), methods=['POST'], status_code=202, name=operation)
    return app


def load_gateway():
    path = Path(os.environ.get('OMAP_CLUSTER_CONFIG', ROOT/'cluster.json'))
    try:
        config = Cluster.model_validate(json.loads(path.read_text(encoding='utf-8-sig')))
    except (OSError, ValueError, ValidationError):
        # Avoid Pydantic validation repr containing any config secrets.
        raise RuntimeError('Missing or invalid private cluster configuration. Run setup-gateway first.') from None
    return create_gateway(config, Path(os.environ.get('OMAP_GATEWAY_OUTPUT_DIR', ROOT/'outputs'/'gateway')))


# Use uvicorn omap_api.gateway:load_gateway --factory, so importing models for
# tests or documentation never requires a private cluster configuration.
