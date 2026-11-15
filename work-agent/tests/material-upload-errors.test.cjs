/* Offline contract tests: actual handler/client/queue; replace only HTTP, temp-root and XHR boundaries. */
const fs = require('node:fs'), fsp = require('node:fs/promises'), path = require('node:path'), os = require('node:os');
const assert = require('node:assert/strict'), crypto = require('node:crypto');
const { Readable } = require('node:stream'), { EventEmitter } = require('node:events');
const root = path.resolve(process.env.MATERIAL_TEST_SOURCE_ROOT || path.join(__dirname, '../..'));
const ts = require(process.env.TYPESCRIPT_PATH || path.join(root, 'harness/node_modules/typescript'));
const clientPath = 'harness/packages/client/ui-banana-panel/src/client/';
const report = { passed: false, real_network_calls: 0, real_database_calls: 0, model_calls: 0, checks: [], failures: [], source_files: {}, adaptations: [] };
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const source = name => { const bytes = fs.readFileSync(path.join(root, name)); report.source_files[name] = hash(bytes); return bytes.toString('utf8'); };
const replace = (text, old, value) => { assert.equal(text.split(old).length, 2, old); report.adaptations.push(old); return text.replace(old, value); };
const moduleFrom = (text, label) => import('data:text/javascript;base64,' + Buffer.from(text + '\n//# sourceURL=' + label).toString('base64'));
const compile = text => ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const check = async (name, run) => { try { const proof = await run(); report.checks.push({ name, passed: true, ...proof }); } catch (e) { report.checks.push({ name, passed: false }); report.failures.push({ name, type: e.name, message: e.message }); } };
const project = '11111111-2222-4333-8444-555555555555', uploadId = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
const requestId = '12345678-1234-4234-8234-123456789abc', secret = 'offline-test-capability-secret';
const filename = '自制中文资料BP-202611.pdf', payload = Buffer.alloc(Math.floor(12.3 * 1024 * 1024), 0x61);
payload.write('%PDF-transport-only-not-a-valid-document\n');
const baseHeaders = { origin: 'https://test-ai.wuyullm.com', authorization: 'Bearer local-test-only', 'x-filename': encodeURIComponent(filename), 'x-file-size': String(payload.length), 'x-batch-count': '1', 'x-batch-bytes': String(payload.length), 'x-material-role': 'material', 'x-upload-id': uploadId, 'content-length': String(payload.length), 'content-type': 'application/octet-stream', 'x-file-type': 'application/pdf' };
class ExpressResponse extends EventEmitter {
  constructor() { super(); this.statusCode = 200; this.headersSent = false; this.destroyed = false; this.writableEnded = false; this.headers = {}; }
  setHeader(k, v) { this.headers[k] = v; }
  status(s) { this.statusCode = s; return this; }
  json(value) { this.value = value; this.headersSent = true; this.writableEnded = true; return this; }
  end() { this.headersSent = true; this.writableEnded = true; return this; }
}
let tokens, apiModule, handler, front, temp;
const observedErrors = [];
const baseApi = () => ({ get: async () => ({ data: { project_id: project, materials: [], material_limits: { max_project_files: 50, max_project_bytes: 524288000 }, material_usage: { bytes: 0 } } }), postStreamingForm: async (_p, _f, file) => { assert.equal(file.name, filename); assert.equal(file.type, 'application/pdf'); assert.equal(file.size, payload.length); assert.equal(hash(await fsp.readFile(file.path)), hash(payload)); return { data: { uploaded_material_ids: ['offline-material'], revision: 1 } }; } });
function routes(api, timeout = 10000) {
  let middleware, upload;
  handler.registerMaterialUploadRoutes({ use: (_p, fn) => middleware = fn, get: () => {}, post: (_p, fn) => upload = fn }, { frontendUrl: 'https://test-ai.wuyullm.com', publicAgentUrl: 'https://test-ai.wuyullm.com/ppt-agent', capabilitySecret: secret, materialUploadTimeoutMs: timeout }, api, token => token === 'Bearer local-test-only');
  return async (bytes = payload, headers = {}) => {
    const req = Readable.from([bytes]), all = { ...baseHeaders, 'x-project-handle': tokens.issueProjectHandle(project, secret), ...headers };
    req.header = key => all[key.toLowerCase()]; req.method = 'POST';
    const res = new ExpressResponse(); let next = false;
    middleware(req, res, () => next = true); if (next) await upload(req, res);
    if (res.value?.error) observedErrors.push({ ...res.value.error, status: res.statusCode });
    assert.equal((await fsp.readdir(temp)).length, 0, 'owned temporary files must be removed');
    return res;
  };
}
function xhr(status, body, event = 'load', headers = {}) {
  return { status, responseText: typeof body === 'string' ? body : JSON.stringify(body), upload: {}, headers: {}, sentCount: 0,
    open(method, url) { this.method = method; this.url = url; }, setRequestHeader(k, v) { this.headers[k] = v; },
    getResponseHeader(key) { return headers[key] || null; },
    send(file) { this.file = file; this.sentCount++; queueMicrotask(() => this['on' + event]?.()); }, abort() { this.onabort?.(); } };
}
async function frontCase(status, body, event = 'load', headers) {
  const request = xhr(status, body, event, headers), file = new File([payload], filename, { type: 'application/pdf' });
  try { return { result: await front.uploadBinaryMaterial('/mcp', '', file, { projectHandle: 'offline-handle', role: 'material', uploadId, batchCount: 1, batchBytes: file.size }, new AbortController().signal, () => {}, () => request), xhr: request }; }
  catch (error) { return { error, xhr: request }; }
}
function diagnostic(res, status, code, stage) {
  assert.equal(res.statusCode, status); assert.equal(res.value.error.code, code); assert.equal(res.value.error.stage, stage);
  assert.match(res.value.error.request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  assert.equal(res.headers['X-Wuyu-Upload-Request-Id'], res.value.error.request_id);
  return res.value.error.request_id;
}
(async () => {
  const originalError = console.error, logs = [];
  console.error = text => logs.push(text);
  try {
    globalThis.location = { href: 'https://test-ai.wuyullm.com/work' };
    temp = await fsp.mkdtemp(path.join(os.tmpdir(), 'material-upload-contract-'));
    globalThis.__materialTestTemp = temp;
    tokens = await moduleFrom(source('work-agent/dist/tokens.js'), 'tokens-test.mjs');
    globalThis.__materialTestTokens = tokens;
    let apiText = source('work-agent/dist/api-client.js');
    apiText = replace(apiText, 'import { backendFetch } from "./backend-http.js";', 'const backendFetch=(...args)=>globalThis.__materialTestFetch(...args);');
    apiModule = await moduleFrom(apiText, 'api-client-test.mjs'); globalThis.__materialTestApi = apiModule;
    let text = source('work-agent/dist/material-upload-http.js');
    text = replace(text, 'import { tmpdir } from "node:os";', 'const tmpdir=()=>globalThis.__materialTestTemp;');
    text = replace(text, 'import { BananaApiError } from "./api-client.js";', 'const {BananaApiError}=globalThis.__materialTestApi;');
    text = replace(text, 'import { projectIdFromHandle } from "./tokens.js";', 'const {projectIdFromHandle}=globalThis.__materialTestTokens;');
    handler = await moduleFrom(text, 'material-upload-handler-test.mjs');
    let frontText = compile(source(clientPath + 'material-http.ts'));
    if (fs.existsSync(path.join(root, clientPath + 'material-upload-errors.ts'))) {
      globalThis.__materialTestErrors = await moduleFrom(compile(source(clientPath + 'material-upload-errors.ts')), 'material-upload-errors-test.mjs');
      frontText = replace(frontText, "import { MaterialUploadError, materialUploadFailure } from './material-upload-errors.ts';", 'const {MaterialUploadError,materialUploadFailure}=globalThis.__materialTestErrors;');
      frontText = replace(frontText, "export { MaterialUploadError, materialUploadFailure } from './material-upload-errors.ts';", 'export {MaterialUploadError,materialUploadFailure};');
    }
    front = await moduleFrom(frontText, 'material-http-test.mjs'); globalThis.__materialTestFront = front;

    await check('Chinese-name 12.3 MiB PDF MIME transport, SHA, and duplicate reuse unchanged', async () => {
      const api = baseApi(), post = api.postStreamingForm; let calls = 0;
      api.postStreamingForm = async (...args) => { calls++; return post(...args); };
      const invoke = routes(api), first = await invoke(), second = await invoke();
      assert.equal(first.statusCode, 200); assert.equal(first.value.received_bytes, payload.length); assert.equal(first.value.sha256, hash(payload));
      assert.equal(second.value.duplicate, true); assert.equal(calls, 1);
      return { bytes: payload.length, sha256: hash(payload), valid_pdf_parse: false };
    });
    const statuses = { 400: 'UPLOAD_REQUEST_REJECTED', 401: 'UNAUTHORIZED', 403: 'UPLOAD_ACCESS_DENIED', 404: 'UPLOAD_TARGET_UNAVAILABLE', 408: 'UPLOAD_TIMEOUT', 409: 'UPLOAD_STATE_CONFLICT', 413: 'UPLOAD_REQUEST_TOO_LARGE', 415: 'BINARY_FILE_REQUIRED', 422: 'UPLOAD_REQUEST_REJECTED', 429: 'UPLOAD_RATE_LIMITED', 502: 'UPLOAD_SERVICE_UNAVAILABLE' };
    for (const [statusText, code] of Object.entries(statuses)) await check('upstream status ' + statusText + ' has safe fixed fallback and register correlation', async () => {
      const status = Number(statusText), api = baseApi();
      api.postStreamingForm = async () => { throw new apiModule.BananaApiError('NEVER-RETURN-PRIVATE-MESSAGE', status, { error: { code: 'NEVER-RETURN-PRIVATE-CODE', message: filename, stage: filename, request_id: filename }, filename }); };
      const res = await routes(api)(); diagnostic(res, status, code, 'register');
      assert(!JSON.stringify(res.value).includes('PRIVATE')); assert(!JSON.stringify(res.value).includes(filename));
    });
    await check('project preflight 400 reports project stage without exposing body', async () => {
      const api = baseApi(); api.get = async () => { throw new apiModule.BananaApiError('private', 400, { error: { code: 'unknown' } }); };
      diagnostic(await routes(api)(), 400, 'UPLOAD_REQUEST_REJECTED', 'project');
    });
    for (const [status, code] of [[409, 'UPLOAD_ID_CONFLICT'], [409, 'MATERIAL_DELETE_PENDING'], [413, 'MATERIAL_PROJECT_LIMIT'], [400, 'RELAYOUT_SOURCE_INVALID']]) await check('allowlisted upstream ' + code + ' preserved', async () => {
      const api = baseApi(); api.postStreamingForm = async () => { throw new apiModule.BananaApiError('private', status, { error: { code } }); };
      diagnostic(await routes(api)(), status, code, 'register');
    });
    await check('backend checksum refusal survives handler and XHR without weakening SHA or resending', async () => {
      const api = baseApi(); let registrations = 0;
      api.postStreamingForm = async (_path, fields, file, options) => {
        registrations++; assert.equal(fields.content_sha256, hash(payload)); assert.equal(options.headers['X-Upload-Sha256'], hash(payload));
        assert.equal(hash(await fsp.readFile(file.path)), hash(payload));
        throw new apiModule.BananaApiError('NEVER-RETURN-INTEGRITY-PRIVATE-MESSAGE', 400, { error: { code: 'MATERIAL_HASH_MISMATCH', message: filename } });
      };
      const res = await routes(api)(); diagnostic(res, 400, 'MATERIAL_HASH_MISMATCH', 'register');
      const fc = await frontCase(res.statusCode, res.value);
      assert.equal(fc.error.code, 'MATERIAL_HASH_MISMATCH'); assert.equal(fc.error.status, 400); assert.equal(fc.error.requestId, res.value.error.request_id); assert.equal(fc.xhr.sentCount, 1); assert.equal(registrations, 1);
      assert.equal(globalThis.__materialTestErrors.materialUploadErrorKey(fc.error.code), 'upload.error.checksum');
    });
    await check('arbitrary object code cannot coerce into allowlist', async () => {
      const api = baseApi(); api.get = async () => { throw new apiModule.BananaApiError('private', 400, { error: { code: { toString: () => 'MATERIAL_PROJECT_LIMIT' } } }); };
      diagnostic(await routes(api)(), 400, 'UPLOAD_REQUEST_REJECTED', 'project');
    });
    const localCases = [
      ['invalid metadata', payload, { 'x-filename': '%invalid' }, 400, 'INVALID_UPLOAD_METADATA', 'metadata'],
      ['declared length mismatch', payload, { 'content-length': String(payload.length - 1) }, 400, 'UPLOAD_LENGTH_MISMATCH', 'metadata'],
      ['actual short stream', payload.subarray(0, payload.length - 1), {}, 400, 'UPLOAD_LENGTH_MISMATCH', 'receive'],
      ['oversized file', Buffer.alloc(1), { 'x-file-size': String(50 * 1024 * 1024), 'x-batch-bytes': String(50 * 1024 * 1024), 'content-length': String(50 * 1024 * 1024) }, 413, 'MATERIAL_UPLOAD_LIMIT', 'metadata'],
      ['invalid origin', payload, { origin: 'https://foreign.invalid' }, 403, 'UPLOAD_ORIGIN_DENIED', 'access'],
      ['missing auth', payload, { authorization: '' }, 401, 'UNAUTHORIZED', 'access'],
      ['bad project handle', payload, { 'x-project-handle': 'invalid' }, 403, 'PROJECT_CONTEXT_DENIED', 'project'],
    ];
    for (const [name, bytes, headers, status, code, stage] of localCases) await check(name + ' retains rejection and cleanup', async () => diagnostic(await routes(baseApi())(bytes, headers), status, code, stage));
    await check('request IDs are fresh server UUIDs independent of upload ID', async () => {
      const invoke = routes(baseApi()); const a = await invoke(payload, { 'x-filename': '%' }), b = await invoke(payload, { 'x-filename': '%' });
      const first = diagnostic(a, 400, 'INVALID_UPLOAD_METADATA', 'metadata'), second = diagnostic(b, 400, 'INVALID_UPLOAD_METADATA', 'metadata');
      assert.notEqual(first, second); assert.notEqual(first, uploadId);
    });
    await check('server timeout is distinct from user abort', async () => {
      const api = baseApi(); api.get = async () => { await new Promise(resolve => setTimeout(resolve, 15)); throw new Error('private timeout'); };
      diagnostic(await routes(api, 1)(), 408, 'UPLOAD_TIMEOUT', 'project');
    });

    for (const [statusText, code] of Object.entries(statuses)) await check('frontend HTML ' + statusText + ' retains status and safe header request ID', async () => {
      const status = Number(statusText), fc = await frontCase(status, '<html>private proxy body</html>', 'load', { 'X-Wuyu-Upload-Request-Id': requestId });
      assert.equal(fc.error.code, code); assert.equal(fc.error.status, status); assert.equal(fc.error.requestId, requestId); assert.equal(fc.error.stage, undefined); assert.equal(fc.xhr.sentCount, 1);
    });
    await check('frontend allowlisted JSON diagnostics preserved; arbitrary data stripped', async () => {
      const fc = await frontCase(409, { success: false, error: { code: 'UPLOAD_ID_CONFLICT', request_id: requestId, stage: 'register', message: 'PRIVATE' } });
      assert.equal(fc.error.code, 'UPLOAD_ID_CONFLICT'); assert.equal(fc.error.status, 409); assert.equal(fc.error.requestId, requestId); assert.equal(fc.error.stage, 'register'); assert(!JSON.stringify(fc.error).includes('PRIVATE'));
    });
    await check('frontend arbitrary code/request ID/stage cannot reach error or UI', async () => {
      const fc = await frontCase(400, { error: { code: 'PRIVATE-CODE', request_id: 'PRIVATE-ID', stage: 'PRIVATE-STAGE' } });
      assert.equal(fc.error.code, 'UPLOAD_REQUEST_REJECTED'); assert.equal(fc.error.status, 400); assert.equal(fc.error.requestId, undefined); assert.equal(fc.error.stage, undefined); assert(!JSON.stringify(fc.error).includes('PRIVATE'));
    });
    await check('frontend legacy generic JSON 400 upgraded to rejected request', async () => {
      assert.equal((await frontCase(400, { success: false, error: { code: 'UPLOAD_SERVICE_UNAVAILABLE' } })).error.code, 'UPLOAD_REQUEST_REJECTED');
    });
    for (const [event, code] of [['error', 'UPLOAD_NETWORK'], ['timeout', 'UPLOAD_TIMEOUT'], ['abort', 'UPLOAD_ABORTED']]) await check('frontend ' + event + ' remains explicit, no automatic resend', async () => {
      const fc = await frontCase(0, '', event); assert.equal(fc.error.code, code); assert.equal(fc.xhr.sentCount, 1); assert.equal(fc.error.status, undefined);
    });
    await check('valid frontend success and Chinese filename metadata remain unchanged', async () => {
      const fc = await frontCase(200, { success: true, uploaded_material_ids: ['offline-material'], project_id: project, received_bytes: payload.length, duplicate: false });
      assert.equal(fc.result.success, true); assert.equal(fc.xhr.headers['X-Filename'], encodeURIComponent(filename)); assert.equal(fc.xhr.file.size, payload.length); assert.equal(fc.xhr.withCredentials, false); assert.equal(fc.xhr.headers['X-Upload-Id'], uploadId);
    });
    await check('limits HTTP refusal retains status and safe correlation', async () => {
      await assert.rejects(front.readMaterialLimits('/mcp', '', async () => ({ ok: false, status: 403, headers: { get: () => requestId } })), error => error.status === 403 && error.code === 'UPLOAD_ACCESS_DENIED' && error.requestId === requestId);
    });
    await check('successful upload acceptance still requires a material ID', async () => {
      const fc = await frontCase(200, { success: true, uploaded_material_ids: [] }); assert.equal(fc.error.code, 'UPLOAD_RESULT_UNAVAILABLE'); assert.equal(fc.xhr.sentCount, 1);
    });
    await check('XHR 2xx malformed or invalid success is unconfirmed result, retains header ID, and never resends', async () => {
      for (const body of ['<html>unusable response</html>', null, { success: true }, { success: true, uploaded_material_ids: [42] }, { success: true, uploaded_material_ids: [' '] }]) {
        const fc = await frontCase(200, body, 'load', { 'X-Wuyu-Upload-Request-Id': requestId });
        assert.equal(fc.error.code, 'UPLOAD_RESULT_UNAVAILABLE'); assert.equal(fc.error.status, undefined); assert.equal(fc.error.requestId, requestId); assert.equal(fc.xhr.sentCount, 1);
      }
    });
    await check('actual multipart stream preserves Chinese PDF name, exact Content-Length and payload', async () => {
      const disk = path.join(temp, 'multipart-fixture'); await fsp.writeFile(disk, payload, { flag: 'wx' });
      try {
        const client = new apiModule.BananaApiClient({ baseUrl: 'http://offline.invalid', username: 'offline', password: 'offline' }); client.bearerToken = 'offline-test-token';
        globalThis.__materialTestFetch = async (_url, init) => {
          const chunks = []; for await (const chunk of init.body) chunks.push(Buffer.from(chunk)); const body = Buffer.concat(chunks);
          assert.equal(body.length, Number(init.headers['Content-Length'])); assert(body.includes(Buffer.from('filename="' + filename + '"'))); assert(body.includes(Buffer.from('Content-Type: application/pdf'))); assert(body.includes(payload));
          return new Response(JSON.stringify({ success: true, data: { uploaded_material_ids: ['offline-material'] } }), { status: 200, headers: { 'Content-Type': 'application/json' } });
        };
        assert((await client.postStreamingForm('/api/projects/' + project + '/workflow/materials', { role: 'material' }, { path: disk, name: filename, size: payload.length, type: 'application/pdf' })).success);
      } finally { await fsp.unlink(disk); }
    });

    const material = await moduleFrom(compile(source(clientPath + 'material-upload.ts')), 'material-upload-test.mjs'); globalThis.__materialTestUpload = material;
    let queueText = compile(source(clientPath + 'material-queue.ts'));
    queueText = replace(queueText, "import { MATERIAL_ACCEPT } from './material-upload.ts';", 'const {MATERIAL_ACCEPT}=globalThis.__materialTestUpload;');
    queueText = replace(queueText, "import { MaterialUploadError } from './material-http.ts';", 'const {MaterialUploadError}=globalThis.__materialTestFront;');
    const { MaterialUploadQueue, initialUploadSnapshot } = await moduleFrom(queueText, 'material-queue-test.mjs');
    await check('queue keeps diagnostic and original identity; retries only on explicit action', async () => {
      let snapshot = initialUploadSnapshot(), calls = 0, bindingCalls = 0; const ids = [];
      const store = { getSnapshot: () => snapshot, set: value => snapshot = value };
      const queue = new MaterialUploadQueue(store, { limits: async () => handler.DEFAULT_MATERIAL_UPLOAD_LIMITS, project: async () => ({ handle: 'offline-project', materialCount: 0 }), upload: async (_file, input) => { calls++; ids.push(input.uploadId); throw front.materialUploadFailure(400, { error: { request_id: requestId, stage: 'register' } }); }, bind: async () => { bindingCalls++; throw new Error('must not bind failure'); }, ready: () => {} });
      await queue.add([new File(['local-only'], 'queue.pdf', { type: 'application/pdf' })], 'material', null); await queue.confirm();
      assert.equal(calls, 1); assert.equal(bindingCalls, 0); assert.equal(snapshot.rows[0].phase, 'failed'); assert.equal(snapshot.rows[0].error, 'UPLOAD_REQUEST_REJECTED'); assert.deepEqual(snapshot.rows[0].errorDetails, { status: 400, requestId, stage: 'register' });
      await new Promise(resolve => setTimeout(resolve, 20)); assert.equal(calls, 1); assert(queue.blocksSubmission());
      queue.retry(snapshot.rows[0].id); while (snapshot.busy) await new Promise(resolve => setTimeout(resolve, 1));
      assert.equal(calls, 2); assert.equal(ids[0], ids[1]); assert.equal(bindingCalls, 0);
    });
    await check('queue retains preflight HTTP diagnostic without submitting file bytes', async () => {
      let snapshot = initialUploadSnapshot(), calls = 0;
      const queue = new MaterialUploadQueue({ getSnapshot: () => snapshot, set: value => snapshot = value }, { limits: async () => { throw front.materialUploadFailure(401, { error: { request_id: requestId, stage: 'access' } }); }, project: async () => { calls++; }, upload: async () => { calls++; }, bind: async () => { calls++; }, ready: () => {} });
      await queue.add([new File(['local-only'], 'preflight.pdf')], 'material', null);
      assert.equal(calls, 0); assert.equal(snapshot.rows[0].error, 'UNAUTHORIZED'); assert.deepEqual(snapshot.rows[0].errorDetails, { status: 401, requestId, stage: 'access' }); assert.equal(snapshot.rows[0].submitted, false);
    });
    await check('safe response diagnostic keys exclude user/private content; no new process logging', async () => {
      assert(observedErrors.length > 0); assert.equal(logs.length, 0);
      for (const value of observedErrors) { const line = JSON.stringify(value); assert.deepEqual(Object.keys(value).sort(), ['code', 'request_id', 'stage', 'status']); assert(!line.includes(filename)); assert(!line.includes('PRIVATE')); assert(!line.includes(project)); assert(!line.includes(secret)); }
    });
    await check('every public server failure has a named client message and allowed stage key', async () => {
      const contract = globalThis.__materialTestErrors;
      assert(contract);
      for (const log of observedErrors) {
        if (log.code !== 'UPLOAD_SERVICE_UNAVAILABLE') assert.notEqual(contract.materialUploadErrorKey(log.code), 'upload.error.service', log.code);
        assert.equal(typeof contract.materialUploadStageKey(log.stage), 'string');
      }
    });
    await check('server and client HTTP fallback tables stay equal and every emitted server code is accepted', async () => {
      const contract = globalThis.__materialTestErrors; assert(contract);
      const tree = ts.createSourceFile('material-upload-http.js', source('work-agent/dist/material-upload-http.js'), ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
      const codes = new Set(); let fallback;
      const visit = node => {
        if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'allowed') for (const entry of node.initializer.elements) codes.add(entry.text);
        if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'fallback') fallback = Object.fromEntries(node.initializer.properties.map(p => [p.name.getText(tree), p.initializer.text]));
        if (ts.isNewExpression(node) && node.expression.getText(tree) === 'UploadFailure' && ts.isStringLiteral(node.arguments?.[1])) codes.add(node.arguments[1].text);
        ts.forEachChild(node, visit);
      }; visit(tree);
      assert.deepEqual(fallback, contract.materialUploadHttpCodes);
      for (const code of codes) { assert.equal(front.materialUploadFailure(400, { error: { code } }).code, code); assert.notEqual(contract.materialUploadErrorKey(code), 'upload.error.service'); }
    });
    await check('all client error and stage messages exist in both current locale dictionaries', async () => {
      const text = source(clientPath + 'locales.ts'), tree = ts.createSourceFile('locales.ts', text, ts.ScriptTarget.Latest, true);
      const dictionaries = {};
      const visit = node => {
        if (ts.isVariableDeclaration(node) && ['zh', 'en'].includes(node.name.getText(tree))) {
          let value = node.initializer; while (ts.isSatisfiesExpression(value) || ts.isAsExpression(value)) value = value.expression;
          dictionaries[node.name.getText(tree)] = Object.fromEntries(value.properties.filter(p => p.name && ts.isStringLiteral(p.initializer)).map(p => [p.name.text, p.initializer.text]));
        } ts.forEachChild(node, visit);
      }; visit(tree);
      const helper = ts.createSourceFile('errors.ts', source(clientPath + 'material-upload-errors.ts'), ts.ScriptTarget.Latest, true);
      const keys = new Set(); const collect = node => { if (ts.isStringLiteral(node) && /^(upload\.error\.|taskEntry\.sourceInvalid$)/.test(node.text)) keys.add(node.text); ts.forEachChild(node, collect); }; collect(helper);
      for (const locale of ['zh', 'en']) for (const key of keys) assert.equal(typeof dictionaries[locale]?.[key], 'string', locale + ':' + key);
    });
    report.passed = report.failures.length === 0;
  } catch (error) { report.failures.push({ name: 'test setup or finalization', type: error.name, message: error.message }); }
  finally {
    console.error = originalError;
    if (temp) { report.temporary_files_remaining = await fsp.readdir(temp); if (!report.temporary_files_remaining.length) { await fsp.rmdir(temp); report.temporary_directory_removed = true; } }
    if (process.env.MATERIAL_TEST_REPORT) fs.writeFileSync(process.env.MATERIAL_TEST_REPORT, JSON.stringify(report, null, 2) + '\n');
    console.log(JSON.stringify({ passed: report.passed, checks: report.checks.length, failures: report.failures, temporary_directory_removed: report.temporary_directory_removed }, null, 2));
    process.exitCode = report.passed ? 0 : 1;
  }
})();
