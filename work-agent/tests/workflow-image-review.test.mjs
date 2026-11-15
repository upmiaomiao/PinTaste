/** Current registered handlers + real Zod + Host text/pruner functions, no network. */
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath, pathToFileURL } from 'node:url';
import ts from 'typescript';
import { z } from 'zod/v4';

const runtime = process.env.WORKFLOW_IMAGE_REVIEW_RUNTIME
  ? pathToFileURL(path.resolve(process.env.WORKFLOW_IMAGE_REVIEW_RUNTIME) + path.sep)
  : new URL('../dist/', import.meta.url);
const harness = process.env.WORKFLOW_HOST_HARNESS_ROOT
  ? path.resolve(process.env.WORKFLOW_HOST_HARNESS_ROOT)
  : fileURLToPath(new URL('../../harness/', import.meta.url));
const tools = await import(new URL('workflow-tools.js', runtime));
const contract = await import(new URL('workflow-action-contract.js', runtime));
const { issueProjectHandle } = await import(new URL('tokens.js', runtime));
const { BananaApiError } = await import(new URL('api-client.js', runtime));
const config = { capabilitySecret: 'invented-test-secret-only', frontendUrl: 'https://example.invalid', publicAgentUrl: 'https://example.invalid/ppt-agent' };
const project = 'synthetic-image-review';
const handle = issueProjectHandle(project, config.capabilitySecret);
const request = '00000000-0000-4000-8000-000000000164';
const versions = (count = 12, value = 1) => Object.fromEntries(Array.from({ length: count }, (_, i) => [String(i + 1), value]));
function snapshot(count = 12, preference = 'editable', phase = 'final_image_review') {
  return { project_id: project, revision: 17,
    brief: { creation_kind: 'deck', page_count: count, output_preference: preference, topic: 'Invented test', language: 'zh' },
    workflow: { project_id: project, phase, mode: 'batch', review_policy: 'template_and_final', output_preference: preference,
      template: { id: 'synthetic-template', revision: 3, confirmed_revision: 3 },
      pages: Array.from({ length: count }, (_, i) => ({ number: i + 1, generation_status: 'ready', image_version: 1,
        confirmed_image_version: null, trace_version: 0, artifact: { image_uri: `https://example.invalid/page-${i + 1}.png` } })) },
    export_capabilities: { image_pptx: phase !== 'final_image_review', hybrid_pptx: false },
    template_selection: { required: false, protocol_version: 1 }, materials: [], artifacts: [], jobs: [] };
}
function registration(value = snapshot(), postError) {
  const registry = new Map(), posts = [], gets = [];
  const api = { async get(url) { gets.push(url); return { data: structuredClone(value) }; },
    async post(url, body) { posts.push({ url, body: structuredClone(body) }); if (postError) throw postError; return { data: structuredClone(value) }; } };
  tools.registerWorkflowTools({ registerTool(name, definition, handler) { assert.ok(!registry.has(name)); registry.set(name, { definition, handler }); } }, api, config);
  return { action: registry.get('presentation_workflow_action'), get: registry.get('get_presentation_workflow'), registry, posts, gets };
}
const invalid = [undefined, null, {}, [], { image_versions: {} }, { image_versions: [] }, { image_versions: null },
  { image_versions: { 0: 1 } }, { image_versions: { 81: 1 } }, { image_versions: { '01': 1 } },
  { image_versions: { '1.0': 1 } }, { image_versions: { '+1': 1 } }, { image_versions: { '１': 1 } },
  ...[0, -1, 1.5, NaN, Infinity, true, '1', null].map(value => ({ image_versions: { 1: value } })),
  { image_versions: versions(), approval: true }, { image_versions: versions(), number: 1 },
  JSON.parse('{"image_versions":{"__proto__":1}}'), { versions: versions() }];

test('real Zod exposes an exact image-review payload schema', () => {
  assert.equal(typeof contract.imageReviewPayload?.safeParse, 'function');
  for (const count of [1, 12, 80]) assert.equal(contract.imageReviewPayload.safeParse({ image_versions: versions(count) }).success, true);
  for (const payload of invalid) assert.equal(contract.imageReviewPayload.safeParse(payload).success, false);
});
test('actual registered schema advertises current map and explicit image export', () => {
  const { action } = registration();
  const schema = z.toJSONSchema(z.object(action.definition.inputSchema));
  assert.ok(schema.properties.payload.properties.image_versions);
  assert.match(action.definition.description, /conversation\.review_target\.image_versions/);
  assert.match(action.definition.description, /format:'pptx'/);
  assert.match(action.definition.description, /actual same-version|actual current-version/);
});
test('malformed confirmations return the fixed local reason with zero API posts', async () => {
  const r = registration();
  for (const payload of invalid) {
    const result = await r.action.handler({ project_handle: handle, action: 'confirm_images', payload, expected_revision: 17, request_id: request });
    assert.equal(result.isError, true);
    assert.equal(result.structuredContent.workflow_action_error.error_code, 'IMAGE_REVIEW_INPUT_INVALID');
    assert.equal(result.structuredContent.workflow_action_error.dispatch_outcome, 'rejected_before_dispatch');
    assert.doesNotMatch(JSON.stringify(result), /尚不能确定原因|当前步骤不支持/);
  }
  assert.deepEqual(r.posts, []); assert.deepEqual(r.gets, []);
});
test('valid confirmation is forwarded exactly once without fabricated fields', async () => {
  const r = registration(), payload = { image_versions: versions() };
  await r.action.handler({ project_handle: handle, action: 'confirm_images', payload, expected_revision: 17, request_id: request });
  assert.deepEqual(r.posts, [{ url: `/api/projects/${project}/workflow/action`, body: { action: 'confirm_images', payload, expected_revision: 17, request_id: request } }]);
  assert.deepEqual(payload, { image_versions: versions() });
});
test('structurally valid partial map is never auto-filled and backend guard remains', async () => {
  const r = registration(snapshot(), new BananaApiError('Invented rejected current-image guard', 400, { error: { code: 'INVALID_WORKFLOW_ACTION' } }));
  const payload = { image_versions: { 1: 1 } };
  const result = await r.action.handler({ project_handle: handle, action: 'confirm_images', payload, expected_revision: 17, request_id: request });
  assert.equal(r.posts.length, 1); assert.deepEqual(r.posts[0].body.payload, payload); assert.equal(result.isError, true);
  assert.equal(result.structuredContent.workflow_action_error.error_code, 'INVALID_WORKFLOW_ACTION');
});
test('fixed input guidance is exact-status and never echoes arbitrary error bodies', () => {
  const bait = 'invented-secret-token-marker /root/invented-private-path';
  const payload = { error: { code: 'IMAGE_REVIEW_INPUT_INVALID', message: bait, diagnostics: { token: bait } } };
  const guidance = contract.actionRecovery('confirm_images', 400, payload);
  assert.equal(guidance.dispatch_outcome, 'rejected_before_dispatch'); assert.doesNotMatch(JSON.stringify(guidance), /invented-secret/);
  for (const status of [undefined, 401, 403, 409, 422, 500, '400']) {
    assert.equal(contract.workflowErrorGuidance(payload, status), null);
    assert.equal(Object.hasOwn(contract.actionRecovery('confirm_images', status, payload), 'dispatch_outcome'), false);
  }
});
test('unknown errors retain conservative guidance and existing draft rejection stays exact', () => {
  const unknown = contract.actionRecovery('confirm_images', 409, { error: { code: 'INVENTED_UNKNOWN', message: 'invented-secret' } });
  assert.equal(unknown.error_code, 'WORKFLOW_ACTION_FAILED'); assert.equal(Object.hasOwn(unknown, 'dispatch_outcome'), false);
  assert.doesNotMatch(JSON.stringify(unknown), /invented-secret/);
  assert.equal(contract.actionRecovery('continue_from_draft', 409, { error: { code: 'DRAFT_NOT_READY' } }).dispatch_outcome, 'rejected_before_dispatch');
});

function compile(source) { return ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText; }
function actualHostTextPipeline() {
  const prunerRoot = path.join(harness, 'packages/compaction/compaction-tool-result-pruner/src');
  const configSource = fs.readFileSync(path.join(prunerRoot, 'config.ts'), 'utf8');
  const exports = {};
  vm.runInNewContext(compile(configSource), { exports, structuredClone, require: () => ({ deepFreeze: Object.freeze }) });
  const prunerSource = fs.readFileSync(path.join(prunerRoot, 'index.ts'), 'utf8');
  const parsed = ts.createSourceFile('actual-pruner.ts', prunerSource, ts.ScriptTarget.Latest, true);
  const cls = parsed.statements.find(n => ts.isClassDeclaration(n) && n.name?.text === 'ToolResultPruner');
  const methods = cls.members.filter(n => ['measureContent', 'pruneContent'].includes(n.name?.getText(parsed))).map(n => n.getText(parsed));
  assert.equal(methods.length, 2);
  const context = { codePointLength: exports.codePointLength, PRUNE_MARKER: exports.PRUNE_MARKER };
  vm.runInNewContext(compile(`class ActualPruner { ${methods.join('\n')} };globalThis.pruner=new ActualPruner();`), context);
  context.pruner.config = exports.resolveConfig();
  const bridgeSource = fs.readFileSync(path.join(harness, 'packages/mcp/mcp-client/src/tools.ts'), 'utf8');
  const bridge = ts.createSourceFile('actual-bridge.ts', bridgeSource, ts.ScriptTarget.Latest, true);
  const functions = bridge.statements.filter(n => ts.isFunctionDeclaration(n) && ['extractText', 'projectContent', 'isRecord', 'imageDiagnostic'].includes(n.name?.text)).map(n => n.getText(bridge));
  assert.equal(functions.length, 4);
  vm.runInNewContext(compile(functions.join('\n') + '\nglobalThis.extract=extractText;'), context);
  return { extract: context.extract, pruner: context.pruner };
}
function firstDecision(blocks) {
  const first = blocks.find(b => b.type === 'text').text.split('\n')[0];
  const value = JSON.parse(first); assert.equal(value.kind, 'wuyu_workflow_decision'); assert.equal(value.version, 1); return value;
}
test('real registered read puts the computed current 12-page decision first', async () => {
  const r = registration(), result = await r.get.handler({ project_handle: handle });
  const decision = firstDecision(result.content);
  assert.equal(decision.revision, 17); assert.equal(decision.required_action, 'confirm_images');
  assert.deepEqual(decision.review_target.image_versions, versions()); assert.equal(decision.requires_user_confirmation, true);
  assert.equal(decision.same_version_user_decision_required, true); assert.equal(decision.read_starts_work, false);
  assert.deepEqual(r.posts, []); assert.equal(r.gets.length, 1);
});
for (const count of [12, 80]) test(`actual Host text extraction and pruning preserve all ${count} current page versions`, async () => {
  const value = snapshot(count); value.brief.requirements = 'Invented long material prose. '.repeat(1600);
  if (count === 80) for (const page of value.workflow.pages) page.image_version = Number.MAX_SAFE_INTEGER;
  const r = registration(value), result = await r.get.handler({ project_handle: handle });
  assert.ok(result.content[0].text.length < 4096);
  const pipeline = actualHostTextPipeline();
  const text = pipeline.extract(result.content, 'get_presentation_workflow'); assert.ok(text.length > 8192);
  const pruned = pipeline.pruner.pruneContent([{ type: 'text', text }]); assert.ok(pruned);
  assert.ok(pruned[0].text.includes('[... tool result middle pruned ...]'));
  const decision = firstDecision(pruned);
  assert.deepEqual(decision.review_target.image_versions, versions(count, count === 80 ? Number.MAX_SAFE_INTEGER : 1));
  assert.equal(decision.automatic_version_fill_allowed, false); assert.equal(decision.preview_is_not_approval, true);
  console.info('IMAGE_REVIEW_METRIC ' + JSON.stringify({ pages: count, decision_chars: result.content[0].text.length,
    before_prune_chars: text.length, after_prune_chars: pipeline.pruner.measureContent(pruned),
    map_count: Object.keys(decision.review_target.image_versions).length, workflow_phase: decision.workflow_phase }));
  assert.deepEqual(r.posts, []);
  assert.equal(result._meta['wuyu/workflow-snapshot'].brief.requirements, value.brief.requirements);
  assert.equal(result._meta['wuyu/workflow-snapshot'].workflow.pages.length, count);
});
test('decision block is a closed projection and cannot forward arbitrary prose or secrets', () => {
  const bait = 'invented-sensitive-marker';
  const value = snapshot(); value.revision = bait;
  value.conversation = { required_action: bait, requires_user_confirmation: true, summary: bait, failure: { message: bait },
    required_action_payload: { token: bait }, review_target: { image_versions: { 1: bait }, token: bait }, authority: bait };
  const decision = firstDecision(tools.workflowContent(value, handle));
  assert.equal(decision.revision, null); assert.equal(decision.required_action, null);
  assert.doesNotMatch(JSON.stringify(decision), new RegExp(bait)); assert.deepEqual(decision.review_target, {});
});
test('image-only required export format is explicit and starts no work on read', async () => {
  const r = registration(snapshot(12, 'image', 'ready_to_export'));
  const result = await r.get.handler({ project_handle: handle }); const decision = firstDecision(result.content);
  assert.equal(decision.required_action, 'export'); assert.deepEqual(decision.required_action_payload, { format: 'pptx' });
  assert.deepEqual(decision.image_only_export.payload, { format: 'pptx' }); assert.equal(decision.image_only_export.may_call_paid_model, false);
  assert.deepEqual(r.posts, []);
});
test('editable conversion remains separately explicit and may call a paid model', async () => {
  const r = registration(snapshot(12, 'image', 'exported'));
  const result = await r.get.handler({ project_handle: handle }); const decision = firstDecision(result.content);
  const conversion = result.structuredContent.conversation.editable_conversion;
  assert.deepEqual(conversion.payload, { format: 'editable_pptx' });
  assert.equal(conversion.requires_explicit_user_request, true); assert.equal(conversion.may_call_paid_model, true);
  assert.equal(decision.editable_conversion_requires_explicit_request_and_cost_scope, true); assert.deepEqual(r.posts, []);
});
test('image review wording offers current image delivery without promising native tracing', async () => {
  const result = await registration().get.handler({ project_handle: handle });
  const conversation = result.structuredContent.conversation;
  assert.match(conversation.choices.find(c => c.id === 'confirm_images').label, /当前全部图片/);
  assert.doesNotMatch(conversation.next_question, /确认没问题后再开始可编辑描摹/);
  assert.match(conversation.next_question, /明确请求.*费用/);
});
