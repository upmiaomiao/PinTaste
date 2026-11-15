// Runtime-only panel: add fixed English messages without rebuilding old authoring.
const fs = require('fs'), path = require('path'), crypto = require('crypto');
let ts;
for (const name of [process.env.WUYU_TYPESCRIPT_MODULE, 'typescript', '/opt/dsh-harness/node_modules/typescript'].filter(Boolean)) {
  try { ts = require(name); break; } catch {}
}
if (!ts) throw Error('TypeScript parser required');
const BASELINE_SHA = '5d771670fe93f7f9120efcb5aa887c79daac8eaa3783401355f65727dd4ccc51';
const HEAD = 'function q$(e){';
const GUARD = 'if(document.documentElement.lang==="en"){const capacity={capacity_queue_timeout:"The local model queue timed out. Materials and completed pages are retained; retry this step later.",capacity_queue_full:"The local model waiting queue is full. Materials and completed pages are retained; retry this step later.",capacity_request_body_timeout:"The local system timed out while receiving this request. Materials and completed pages are retained; retry this step later.",capacity_memory_limit:"This work item exceeds the current memory budget. No new model request was sent for this item; reduce reference materials or process them in batches. Materials and completed pages are retained."}[String(e.error_code||"").toLowerCase()];if(capacity)return capacity;}';
const sha = value => crypto.createHash('sha256').update(value).digest('hex');

function renderer(html) {
  const match = /<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html);
  if (!match) throw Error('Runtime module missing');
  const code = match[1], offset = match.index + match[0].indexOf(code);
  const ast = ts.createSourceFile('panel.js', code, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  if (ast.parseDiagnostics.length) throw Error('Runtime module parse failed');
  let target;
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === 'q$') {
      if (target) throw Error('Ambiguous failure renderer');
      target = node;
    }
    ts.forEachChild(node, visit);
  }
  visit(ast);
  if (!target || !target.getText(ast).startsWith(HEAD)) throw Error('Failure renderer changed');
  return {text: target.getText(ast), start: offset + target.getStart(ast), end: offset + target.getEnd()};
}

function transform(html) {
  const node = renderer(html), patched = node.text.startsWith(HEAD + GUARD);
  const original = patched ? HEAD + node.text.slice((HEAD + GUARD).length) : node.text;
  const baseline = html.slice(0, node.start) + original + html.slice(node.end);
  if (sha(baseline) !== BASELINE_SHA) throw Error('Actual panel baseline changed');
  const next = HEAD + GUARD + original.slice(HEAD.length);
  const result = html.slice(0, node.start) + next + html.slice(node.end);
  const after = renderer(result);
  const reversed = result.slice(0, after.start) + original + result.slice(after.end);
  if (reversed !== baseline) throw Error('Unexpected panel change');
  return {html: result, metadata: {
    before_sha256: sha(html), baseline_sha256: BASELINE_SHA, after_sha256: sha(result),
    changed_function: 'q$', idempotent: patched,
    only_expected_guard_changed: true, all_other_bytes_unchanged: true
  }};
}

module.exports = {transform, BASELINE_SHA};
if (require.main === module) {
  const [input, output] = process.argv.slice(2);
  if (!input || !output) throw Error('Usage: node provider-capacity-errors-ui.cjs baseline.html output.html');
  const result = transform(fs.readFileSync(input, 'utf8'));
  fs.mkdirSync(path.dirname(output), {recursive: true});
  fs.writeFileSync(output, result.html);
  console.log(JSON.stringify(result.metadata));
}
