const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');
const {transform, BASELINE_SHA} = require('./provider-capacity-errors-ui.cjs');
const ts = require(process.env.WUYU_TYPESCRIPT_MODULE || 'typescript');
const baselinePath = process.env.WUYU_PANEL_BASELINE;
if (!baselinePath) throw Error('WUYU_PANEL_BASELINE required');
const baseline = fs.readFileSync(baselinePath, 'utf8');

function renderFunction(html, lang) {
  const code = /<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(html)[1];
  const ast = ts.createSourceFile('panel.js', code, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  let node;
  function visit(n) {
    if (ts.isFunctionDeclaration(n) && n.name?.text === 'q$') node = n;
    ts.forEachChild(n, visit);
  }
  visit(ast);
  const context = {document: {documentElement: {lang}}, jt: value => value,
    __wuyu_panel18_text: value => value};
  vm.createContext(context);
  vm.runInContext(node.getText(ast), context);
  return {context, text: node.getText(ast), fn: context['q$']};
}

test('exact baseline generates reproducible candidate and only q$ changes', () => {
  const first = transform(baseline), second = transform(baseline);
  assert.equal(first.html, second.html);
  assert.equal(first.metadata.baseline_sha256, BASELINE_SHA);
  const old = renderFunction(baseline, 'zh').text, next = renderFunction(first.html, 'zh').text;
  assert.equal(first.html.replace(next, old), baseline);
  assert.equal(first.metadata.idempotent, false);
});

test('candidate is idempotent and unknown drift is rejected', () => {
  const first = transform(baseline), again = transform(first.html);
  assert.equal(again.html, first.html);
  assert.equal(again.metadata.idempotent, true);
  assert.throws(() => transform(baseline + '\n'), /baseline changed/);
  assert.throws(() => transform(first.html.replace('Materials and completed pages', 'Unexpected changed copy')), /baseline changed/);
});

test('four new codes show fixed English copy and cannot expose arbitrary error text', () => {
  const render = renderFunction(transform(baseline).html, 'en').fn;
  for (const error_code of ['capacity_queue_timeout', 'capacity_queue_full', 'capacity_request_body_timeout', 'capacity_memory_limit']) {
    const value = render({error_code, error_message: 'https://sensitive.invalid/private-response'});
    assert.match(value, /local model|local system|memory budget/);
    assert.match(value, /retained/);
    assert.doesNotMatch(value, /sensitive/);
    if (error_code === 'capacity_memory_limit') {
      assert.match(value, /No new model request was sent for this item/);
      assert.match(value, /reduce reference materials|batches/);
    }
  }
});

test('Chinese and existing unknown-code projection are unchanged', () => {
  const candidate = transform(baseline).html;
  for (const lang of ['zh', 'en']) {
    const before = renderFunction(baseline, lang).fn, after = renderFunction(candidate, lang).fn;
    for (const job of [{error_code: 'provider_unavailable', error_message: 'Fixed existing guidance'},
      {error_code: 'capacity_queue_timeout', error_message: '本地队列超时，已完成内容保留。'},
      {error_code: 'arbitrary', error_message: '/tmp/private-response'}]) {
      if (lang === 'en' && job.error_code === 'capacity_queue_timeout') continue;
      assert.equal(after(job), before(job));
    }
  }
});
