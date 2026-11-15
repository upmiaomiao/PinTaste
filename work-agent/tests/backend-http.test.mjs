import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { setTimeout as pause } from 'node:timers/promises';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gzipSync, deflateSync, brotliCompressSync } from 'node:zlib';
import { backendFetch } from '../dist/backend-http.js';
import { BananaApiClient, BananaApiError } from '../dist/api-client.js';

async function fixture(t, handler, raw = false) {
    const sockets = new Set();
    const server = raw ? net.createServer(handler) : http.createServer(handler);
    server.on('connection', s => { sockets.add(s); s.on('error', () => {}); s.on('close', () => sockets.delete(s)); });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    t.after(async () => { for (const s of sockets) s.destroy(); await new Promise(r => server.close(r)); });
    return { url: `http://127.0.0.1:${server.address().port}`, sockets };
}
const options = extra => ({ redirect: 'error', signal: AbortSignal.timeout(2000), ...extra });
const client = (url, extra = {}) => {
    const api = new BananaApiClient({ baseUrl: url, username: 'self-fixture', password: 'self-fixture', ...extra });
    api.bearerToken = 'self-fixture-token';
    return api;
};

test('JSON and Authorization are preserved without calling global fetch', async t => {
    let seen;
    const f = await fixture(t, async (req, res) => {
        const chunks = []; for await (const chunk of req) chunks.push(chunk);
        seen = { method: req.method, auth: req.headers.authorization, body: Buffer.concat(chunks).toString() };
        res.setHeader('Content-Type', 'application/json'); res.end('{"ok":true}');
    });
    const old = globalThis.fetch; globalThis.fetch = () => { throw new Error('global fetch must not run'); };
    try { assert.deepEqual(await client(f.url).post('/api/self-fixture', { value: 3 }), { ok: true }); }
    finally { globalThis.fetch = old; }
    assert.deepEqual(seen, { method: 'POST', auth: 'Bearer self-fixture-token', body: '{"value":3}' });
});

for (const framing of ['length', 'chunked', 'eof'])
    test(`unconsumed body with FIN is completed safely: ${framing}`, async t => {
        const data = Buffer.alloc(96 * 1024, 97);
        const f = await fixture(t, socket => socket.once('data', () => {
            const frame = framing === 'length' ? `Content-Length: ${data.length}\r\n` : framing === 'chunked' ? 'Transfer-Encoding: chunked\r\n' : '';
            socket.write(`HTTP/1.1 200 OK\r\n${frame}Connection: close\r\n\r\n`);
            if (framing === 'chunked') socket.end(Buffer.concat([Buffer.from(data.length.toString(16) + '\r\n'), data, Buffer.from('\r\n0\r\n\r\n')]));
            else socket.end(data);
        }), true);
        const response = await backendFetch(f.url, options());
        await pause(80); // Keep body unread across peer FIN, preserving backpressure.
        assert.deepEqual(Buffer.from(await response.arrayBuffer()), data);
    });

for (const framing of ['length', 'chunked'])
    test(`truncated ${framing} rejects its request instead of false success`, async t => {
        const f = await fixture(t, socket => socket.once('data', () => {
            socket.end(framing === 'length'
                ? 'HTTP/1.1 200 OK\r\nContent-Length: 90000\r\nConnection: close\r\n\r\nshort'
                : 'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n10\r\nshort');
        }), true);
        await assert.rejects(async () => (await backendFetch(f.url, options())).arrayBuffer(), e => e.code === 'BACKEND_RESPONSE_TRUNCATED');
    });

test('reset before headers is a request-local network error', async t => {
    const f = await fixture(t, socket => socket.once('data', () => socket.destroy()), true);
    await assert.rejects(backendFetch(f.url, options()), e => e.code === 'BACKEND_NETWORK_ERROR');
});

test('deadline before response headers remains bounded', async t => {
    const f = await fixture(t, () => {});
    await assert.rejects(backendFetch(f.url, options({ signal: AbortSignal.timeout(35) })), e => e.code === 'BACKEND_REQUEST_TIMEOUT');
});

test('deadline covers stalled response body as well as headers', async t => {
    const f = await fixture(t, (_req, res) => { res.writeHead(200); res.write('first'); });
    const response = await backendFetch(f.url, options({ signal: AbortSignal.timeout(50) }));
    await assert.rejects(response.text(), e => e.code === 'BACKEND_REQUEST_TIMEOUT');
});

test('caller cancellation terminates an active response without uncaught errors', async t => {
    const f = await fixture(t, (_req, res) => { res.writeHead(200); res.write('first'); });
    const controller = new AbortController();
    const response = await backendFetch(f.url, options({ signal: controller.signal }));
    const consuming = response.text(); controller.abort();
    await assert.rejects(consuming, e => e.code === 'BACKEND_REQUEST_ABORTED');
});

test('already-aborted request does not connect', async t => {
    let calls = 0; const f = await fixture(t, (_req, res) => { calls++; res.end('unexpected'); });
    await assert.rejects(backendFetch(f.url, options({ signal: AbortSignal.abort() })));
    assert.equal(calls, 0);
});

test('cancel unread response releases its socket', async t => {
    const f = await fixture(t, (_req, res) => { res.writeHead(200); res.write(Buffer.alloc(65536)); });
    const response = await backendFetch(f.url, options()); await response.body.cancel();
    for (let i = 0; i < 30 && f.sockets.size; i++) await pause(5);
    assert.equal(f.sockets.size, 0);
});

for (const status of [301, 302, 303, 307, 308])
    test(`redirect ${status} cannot forward authentication`, async t => {
        let targetCalls = 0;
        const target = await fixture(t, (_req, res) => { targetCalls++; res.end('bad'); });
        const f = await fixture(t, (_req, res) => { res.writeHead(status, { Location: target.url }); res.end(); });
        await assert.rejects(backendFetch(f.url, options({ headers: { Authorization: 'Bearer self-private' } })), e => e.code === 'BACKEND_REDIRECT_REJECTED');
        assert.equal(targetCalls, 0);
    });

for (const status of [204, 205, 304])
    test(`bodyless ${status} response completes`, async t => {
        const f = await fixture(t, (_req, res) => { res.writeHead(status); res.end(); });
        const response = await backendFetch(f.url, options()); assert.equal(response.status, status); assert.equal(response.body, null);
    });

for (const [coding, compress] of [['gzip', gzipSync], ['deflate', deflateSync], ['br', brotliCompressSync]])
    test(`forced ${coding} is decoded with correct response lengths`, async t => {
        const data = Buffer.from('self-fixture-data'.repeat(1000)); const encoded = compress(data);
        const f = await fixture(t, (_req, res) => { res.writeHead(200, { 'Content-Encoding': coding, 'Content-Length': encoded.length }); res.end(encoded); });
        const response = await backendFetch(f.url, options()); assert.equal(response.headers.get('content-length'), null); assert.equal(response.headers.get('content-encoding'), null); assert.deepEqual(Buffer.from(await response.arrayBuffer()), data);
    });

test('malformed compressed response fails locally', async t => {
    const f = await fixture(t, (_req, res) => { res.writeHead(200, { 'Content-Encoding': 'gzip' }); res.end('not gzip'); });
    await assert.rejects(async () => (await backendFetch(f.url, options())).text(), e => e.code === 'BACKEND_RESPONSE_TRUNCATED');
});

test('unsupported encoding and compressed ranges cannot create corrupt downloads', async t => {
    for (const [status, coding] of [[200, 'unknown'], [206, 'gzip']]) {
        const f = await fixture(t, (_req, res) => { res.writeHead(status, { 'Content-Encoding': coding }); res.end('bad'); });
        await assert.rejects(backendFetch(f.url, options()), e => e.code === 'BACKEND_RESPONSE_ENCODING_UNSUPPORTED');
    }
});

test('protocol status outside Response range cannot throw from socket callback', async t => {
    const f = await fixture(t, socket => socket.once('data', () => socket.end('HTTP/1.1 700 Strange\r\nContent-Length: 0\r\n\r\n')), true);
    await assert.rejects(backendFetch(f.url, options()), e => e.code === 'BACKEND_RESPONSE_INVALID');
});

test('caller signal cannot disable configured client deadline', async t => {
    const f = await fixture(t, () => {});
    await assert.rejects(client(f.url, { timeoutMs: 35 }).get('/api/self-fixture', { signal: new AbortController().signal }), e => e.code === 'BACKEND_REQUEST_TIMEOUT');
});

test('non-success binary response is cancelled before throwing', async t => {
    const f = await fixture(t, (_req, res) => { res.writeHead(404); res.write(Buffer.alloc(65536)); });
    await assert.rejects(client(f.url).getBinary('/api/self-fixture'), e => e instanceof BananaApiError && e.status === 404);
    for (let i = 0; i < 30 && f.sockets.size; i++) await pause(5);
    assert.equal(f.sockets.size, 0);
});

test('invalid JSON, declared oversize, and HTTP failure keep original API contracts', async t => {
    for (const which of ['json', 'large', 'http']) {
        const f = await fixture(t, (_req, res) => {
            if (which === 'large') { res.writeHead(200, { 'Content-Length': 33554433 }); res.write('x'); }
            else { res.writeHead(which === 'http' ? 409 : 200, { 'Content-Type': 'application/json' }); res.end(which === 'http' ? '{"error":{"code":"REVISION_CONFLICT","message":"self-fixture"}}' : 'bad json'); }
        });
        await assert.rejects(client(f.url).get('/api/self-fixture'), e => e instanceof BananaApiError && e.status === (which === 'large' ? 413 : which === 'http' ? 409 : 502));
    }
});

test('multipart upload and one exact auth retry preserve body bytes', async t => {
    const directory = await mkdtemp(join(tmpdir(), 'wuyu-http142-'));
    t.after(() => rm(directory, { recursive: true, force: true }));
    const tokenPath = join(directory, 'actor.jwt'); await writeFile(tokenPath, 'self.fixture.signature');
    const file = join(directory, 'fixture.txt'); await writeFile(file, Buffer.alloc(512 * 1024, 97));
    let calls = 0, size = 0;
    const f = await fixture(t, async (req, res) => {
        calls++; let bytes = 0; for await (const chunk of req) bytes += chunk.length;
        if (calls === 1) { res.writeHead(401); return res.end(); }
        size = bytes; assert.equal(req.headers.authorization, 'Bearer self.fixture.signature');
        res.setHeader('Content-Type', 'application/json'); res.end('{"ok":true}');
    });
    const api = client(f.url, { actorTokenPath: tokenPath });
    assert.deepEqual(await api.postStreamingForm('/api/self-fixture', { scope: 'self' }, { path: file, name: 'fixture.txt', type: 'text/plain', size: 512 * 1024 }), { ok: true });
    assert.equal(calls, 2); assert(size > 512 * 1024);
});

test('FormData upload serialization remains supported', async t => {
    let body;
    const f = await fixture(t, async (req, res) => { const chunks = []; for await (const b of req) chunks.push(b); body = Buffer.concat(chunks).toString(); assert.match(req.headers['content-type'], /^multipart\/form-data; boundary=/); res.setHeader('Content-Type', 'application/json'); res.end('{"ok":true}'); });
    assert.deepEqual(await client(f.url).postForm('/api/self-fixture', { name: 'self' }, 'file', { data: Buffer.from('unique-fixture-bytes'), type: 'text/plain', name: 'fixture.txt' }), { ok: true });
    assert(body.includes('unique-fixture-bytes')); assert(body.includes('name="name"'));
});

test('safe transport logs never include URLs, tokens, or response prose', async t => {
    const logs = [], previous = console.warn; console.warn = line => logs.push(line);
    try {
        const f = await fixture(t, socket => socket.once('data', () => socket.destroy()), true);
        await assert.rejects(backendFetch(f.url + '/self-private-path?token=self-private', options({ headers: { Authorization: 'Bearer self-private' } })));
    } finally { console.warn = previous; }
    assert(logs.length === 1); assert(!logs.join().includes('self-private')); assert(!logs.join().includes('127.0.0.1')); assert.match(logs[0], /code=BACKEND_NETWORK_ERROR$/);
});

test('HEAD preserves metadata without opening a response body', async t => {
    const f = await fixture(t, (req, res) => { assert.equal(req.method, 'HEAD'); res.writeHead(200, { 'Content-Length': 123 }); res.end(); });
    const response = await backendFetch(f.url, options({ method: 'HEAD' }));
    assert.equal(response.body, null); assert.equal(response.headers.get('content-length'), '123');
});

test('binary range status, bytes, and range headers remain exact', async t => {
    const f = await fixture(t, (req, res) => { assert.equal(req.headers.range, 'bytes=3-6'); res.writeHead(206, { 'Content-Type': 'application/octet-stream', 'Content-Range': 'bytes 3-6/10', 'Content-Length': 4, 'Accept-Ranges': 'bytes' }); res.end('3456'); });
    const response = await client(f.url).getBinaryStream('/api/self-fixture', new AbortController().signal, 'bytes=3-6');
    assert.equal(response.status, 206); assert.equal(response.headers.get('content-range'), 'bytes 3-6/10'); assert.equal(await response.text(), '3456');
});

test('reject unsafe paths and byte ranges before any request', async t => {
    let calls = 0; const f = await fixture(t, (_req, res) => { calls++; res.end(); });
    const api = client(f.url);
    for (const p of ['//outside.invalid/', '/bad\\path', '/bad\r\npath']) await assert.rejects(api.get(p), e => e instanceof BananaApiError && e.status === 400);
    await assert.rejects(api.getBinaryStream('/api/self-fixture', new AbortController().signal, 'bytes=1-2,3-4'), e => e.status === 416);
    assert.equal(calls, 0);
});

test('stream upload handles a server rejecting before request body is consumed', async t => {
    const directory = await mkdtemp(join(tmpdir(), 'wuyu-http142-early-'));
    t.after(() => rm(directory, { recursive: true, force: true }));
    const file = join(directory, 'fixture.txt'); await writeFile(file, Buffer.alloc(2 * 1024 * 1024, 97));
    const f = await fixture(t, (_req, res) => { res.writeHead(413, { 'Content-Type': 'application/json' }); res.end('{"error":{"code":"FILE_TOO_LARGE"}}'); });
    await assert.rejects(client(f.url).postStreamingForm('/api/self-fixture', {}, { path: file, name: 'fixture.txt', type: 'text/plain', size: 2 * 1024 * 1024 }), e => e instanceof BananaApiError && e.status === 413);
});

test('network failures never automatically replay a POST', async t => {
    let calls = 0; const f = await fixture(t, async (req, res) => { calls++; for await (const _ of req) {} res.destroy(); });
    await assert.rejects(client(f.url).post('/api/self-fixture', { operation: 'self-fixture-only' }));
    assert.equal(calls, 1);
});
