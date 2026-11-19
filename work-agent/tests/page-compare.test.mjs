// Offline checks for the read-only page comparison: labels, pairing, rendering, events and the pinned transformer.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { withImageHistory } from '../dist/image-history.js';

const require = createRequire(import.meta.url);
const { transform, baselineOf, BASELINE_SHA } = require('../tools/page-compare-ui.cjs');
const read = name => fs.readFileSync(new URL(name, import.meta.url), 'utf8');
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const pid = '11111111-1111-4111-8111-111111111111';
const artifact = (id, number = 1, kind = 'slide_image', extra = {}) => ({ id, kind, sha256: 'sha-' + id, metadata: { number, ...extra }, created_at: '2026-11-04T10:00:00Z', mime_type: 'image/png',
  url: `/api/projects/${pid}/workflow/artifacts/${id}`, preview_url: `/api/projects/${pid}/workflow/artifacts/${id}`, download_url: `/api/projects/${pid}/workflow/artifacts/${id}?download=1` });
const page = (number, current, extra = {}) => ({ number, generation_status: 'ready', image_version: 1, artifact: current ? { image_uri: artifact(current).url } : null, ...extra });
// Labels first, then opaque absolute URLs, as presentWorkflow hands the snapshot to the panel.
function present(snapshot) {
  const labelled = withImageHistory({ project_id: pid, revision: 1, ...snapshot }, pid);
  return JSON.parse(JSON.stringify(labelled).replaceAll(`/api/projects/${pid}/workflow/artifacts/`, 'https://assets.fixture.invalid/a/'));
}
function panel(snapshot, lang = 'zh-CN') {
  const listeners = {}, focused = [], renders = [];
  const context = { URL, document: { documentElement: { lang }, baseURI: 'https://panel.fixture.invalid/', elements: {},
      addEventListener: (type, handler) => { (listeners[type] ||= []).push(handler); },
      getElementById: id => context.document.elements[id] || null, querySelector: () => null },
    m: { handle: 'h'.repeat(24), snapshot }, ke: () => { renders.push(context.m.previewCompare.page); },
    $: value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]),
    Y: value => { try { const url = new URL(value, 'https://panel.fixture.invalid/'); return ['http:', 'https:'].includes(url.protocol) ? url.href : ''; } catch { return ''; } } };
  for (const id of ['wuyu-compare-open', 'wuyu-compare-before', 'wuyu-compare']) context.document.elements[id] = { focus: () => focused.push(id) };
  vm.createContext(context);
  vm.runInContext(read('../tools/image-history-panel.js') + '\n' + read('../tools/page-compare-panel.js'), context);
  const run = code => vm.runInContext(code, context);
  const fire = (type, event) => listeners[type].forEach(handler => handler(event));
  const click = data => fire('click', { target: { closest: selector => selector.startsWith('button[data-compare-open]') ? { disabled: false, dataset: data } : null } });
  const pairs = () => Object.fromEntries([...run('__wuyuComparePairs(m.snapshot)')].map(([number, pair]) => [number, { current: pair.current.id, options: Array.from(pair.options, option => option.key) }]));
  return { context, run, fire, click, pairs, focused, renders, button: number => run(`__wuyuCompareButton(m.snapshot.workflow.pages[${number - 1}])`), view: number => run(`__wuyuCompareView(m.snapshot.workflow.pages[${number - 1}])`) };
}
const revisions = () => present({ workflow: { pages: [page(1, 'p1c', { image_version: 3, requested_change: '把标题  改成蓝色\n并放大' }), page(2, 'p2a'), page(3, null, { image_version: 0 })] },
  artifacts: [artifact('p1a'), artifact('p2a', 2), artifact('p1b'), artifact('p3a', 3), artifact('p3b', 3), artifact('p1c')] });
// As the backend lists them: the original page renders the prepared re-layout uses, by artifact URL.
const originals = (...ids) => ids.map((id, index) => ({ number: index + 1, image_uri: artifact(id).url }));
const relayout = (extra = {}) => present({ relayout: { status: 'ready', source_sha256: 'deck-2', previews: originals('src1', 'src2', 'src3') },
  workflow: { pages: [page(1, 'new1'), page(2, 'src2'), page(3, 'new3b', { image_version: 2 })] },
  artifacts: [artifact('stale1', 1, 'relayout_source_page', { source_sha256: 'deck-1' }), artifact('src1', 1, 'relayout_source_page', { source_sha256: 'deck-2' }), artifact('src2', 2, 'relayout_source_page', { source_sha256: 'deck-2' }),
    artifact('src3', 3, 'relayout_source_page', { source_sha256: 'deck-2' }), artifact('new1'), artifact('new3a', 3), artifact('new3b', 3)], ...extra });

test('original re-layout pages get a verified label without becoming numbered image records', () => {
  const forged = artifact('foreign', 1, 'relayout_source_page', { relayout_source: { project_id: pid, page_number: 1, is_current: false, in_current_source: true } });
  forged.url = forged.url.replace(pid, '22222222-2222-4222-8222-222222222222');
  const claimed = artifact('claimed', 2, 'relayout_source_page', { relayout_source: { project_id: pid, page_number: 2, is_current: false, in_current_source: true } });
  const input = { project_id: pid, relayout: { previews: [...originals('src1', 'src2'), { number: 1, image_uri: forged.url }, { number: 3, image_uri: artifact('claimed').url }, null] },
    workflow: { pages: [page(1, 'new1'), page(2, 'src2')] }, artifacts: [artifact('src1', 1, 'relayout_source_page'), artifact('new1'), artifact('src2', 2, 'relayout_source_page'), forged, claimed] };
  const before = structuredClone(input), result = withImageHistory(input, pid);
  assert.deepEqual(input, before);
  assert.deepEqual(result.artifacts[0].metadata.relayout_source, { project_id: pid, page_number: 1, is_current: false, in_current_source: true });
  assert.equal(result.artifacts[2].metadata.relayout_source.in_current_source, true);
  // Only the prepared re-layout's own list makes an original current: not what the artifact claims, not another page's entry.
  assert.deepEqual(result.artifacts[4].metadata.relayout_source, { project_id: pid, page_number: 2, is_current: false, in_current_source: false });
  for (const value of [undefined, null, { previews: 'x' }, { previews: {} }, { status: 'registering_pages' }])
    assert.equal(withImageHistory({ ...input, relayout: value }, pid).artifacts[0].metadata.relayout_source.in_current_source, false);
  assert.equal(result.artifacts[0].metadata.image_history, undefined);
  assert.equal(result.artifacts[1].metadata.image_history.record_number, 1);
  assert.equal(result.artifacts[2].metadata.relayout_source.is_current, true);
  assert.equal(result.artifacts[3].metadata.relayout_source, undefined);
});
test('each page pairs its current image with earlier records, newest first', () => {
  assert.deepEqual(panel(revisions()).pairs(), { 1: { current: 'p1c', options: ['record:p1b', 'record:p1a'] } });
});
test('a page with a single record or without a current image exposes no control', () => {
  const ui = panel(revisions());
  assert.match(ui.button(1), /id="wuyu-compare-open"[^>]*data-compare-open="1"[^>]*aria-expanded="false"[^>]*>修改前后对比</);
  assert.equal(ui.button(2), '');
  assert.equal(ui.button(3), '');
  assert.equal(ui.view(1), '');
});
test('later records and unlabelled or foreign artifacts are never offered', () => {
  const snapshot = revisions();
  snapshot.artifacts.push({ ...artifact('raw'), url: 'https://assets.fixture.invalid/a/raw' }, { ...snapshot.artifacts[0], id: 'other', metadata: { number: 1, image_history: { project_id: 'other-project', page_number: 1, record_number: 1, is_current: false } } });
  const relabelled = present({ workflow: { pages: [page(1, 'p1b', { image_version: 2 })] }, artifacts: [artifact('p1a'), artifact('p1b'), artifact('p1c')] });
  assert.deepEqual(panel(relabelled).pairs(), { 1: { current: 'p1b', options: ['record:p1a'] } });
  assert.deepEqual(panel(snapshot).pairs()[1].options, ['record:p1b', 'record:p1a']);
});
test('re-layout pages pair the original page of the current source deck with the restyled page', () => {
  const ui = panel(relayout());
  assert.deepEqual(ui.pairs(), { 1: { current: 'new1', options: ['source:src1'] }, 3: { current: 'new3b', options: ['source:src3', 'record:new3a'] } });
  assert.match(ui.button(1), />原稿对比</);
  assert.equal(ui.button(2), '', 'a page that still shows its original image has nothing to compare');
  ui.click({ compareOpen: '1' });
  const html = ui.view(1);
  assert.deepEqual([...html.matchAll(/<span class="status-chip">([^<]+)<\/span>/g)].map(match => match[1]), ['原稿', '重排后']);
  assert.deepEqual([...html.matchAll(/<img [^>]*src="([^"]+)"/g)].map(match => match[1]), ['https://assets.fixture.invalid/a/src1', 'https://assets.fixture.invalid/a/new1']);
  assert.doesNotMatch(html, /wuyu-compare-request/);
});
test('an original page reused from an earlier upload is still the current original', () => {
  // The backend keeps one artifact per identical page render, so after a second upload the untouched pages
  // still carry the first deck's source_sha256 while the prepared re-layout lists them as its originals.
  const reused = present({ relayout: { status: 'ready', source_sha256: 'deck-2', previews: originals('first1', 'first2', 'second3') },
    workflow: { pages: [page(1, 'new1'), page(2, 'new2'), page(3, 'new3')] },
    artifacts: [artifact('first1', 1, 'relayout_source_page', { source_sha256: 'deck-1' }), artifact('first2', 2, 'relayout_source_page', { source_sha256: 'deck-1' }),
      artifact('first3', 3, 'relayout_source_page', { source_sha256: 'deck-1' }), artifact('second3', 3, 'relayout_source_page', { source_sha256: 'deck-2' }),
      artifact('new1'), artifact('new2', 2), artifact('new3', 3)] });
  const ui = panel(reused);
  assert.deepEqual(ui.pairs(), { 1: { current: 'new1', options: ['source:first1'] }, 2: { current: 'new2', options: ['source:first2'] }, 3: { current: 'new3', options: ['source:second3'] } });
  for (const number of [1, 2, 3]) assert.match(ui.button(number), />原稿对比</);
  // While a new upload is being prepared nothing is listed yet, so no original is offered for it.
  const preparing = structuredClone(reused); delete preparing.relayout.previews;
  assert.deepEqual(panel(present(preparing)).pairs(), {});
});
test('only the images a page has had since its draft was last reset are offered', () => {
  // image_version restarts with every new draft and for a page added again: two earlier records, one of them from the old draft.
  const drafts = version => present({ workflow: { pages: [page(1, 'now2', { image_version: version }), page(2, 'again1')] },
    artifacts: [artifact('old1'), artifact('old2'), artifact('gone1', 2), artifact('now1'), artifact('now2'), artifact('again1', 2)] });
  assert.deepEqual(panel(drafts(2)).pairs(), { 1: { current: 'now2', options: ['record:now1'] } });
  assert.deepEqual(panel(drafts(1)).pairs(), {}, 'the first image of a draft has no earlier version, whatever the project kept');
  assert.deepEqual(panel(drafts(4)).pairs()[1].options, ['record:now1', 'record:old2', 'record:old1']);
  assert.deepEqual(panel(drafts(9)).pairs()[1].options, ['record:now1', 'record:old2', 'record:old1']);
  for (const version of [0, -1, 2.5, '2', null, undefined]) assert.deepEqual(panel(drafts(version)).pairs(), {});
  const ui = panel(drafts(2));
  assert.equal(ui.button(2), '', 'a page removed and added again starts without a comparison');
  ui.click({ compareOpen: '1' });
  assert.deepEqual([...ui.view(1).matchAll(/<option [^>]*value="([^"]+)"/g)].map(match => match[1]), ['record:now1']);
  const missing = drafts(2); delete missing.workflow;
  assert.deepEqual(panel(missing).pairs(), {});
});
test('restyled pages saved before the current re-layout source was prepared are not offered', () => {
  // Second upload: page 1 was first shown as its original and then restyled, so image_version is 2 with a single
  // restyled record of this upload. The earlier record restyles the previous deck and predates the new page 3 render.
  const second = present({ relayout: { status: 'ready', source_sha256: 'deck-2', previews: originals('first1', 'first2', 'second3') },
    workflow: { pages: [page(1, 'new1', { image_version: 2 }), page(2, 'first2'), page(3, 'new3b', { image_version: 2 })] },
    artifacts: [artifact('first1', 1, 'relayout_source_page'), artifact('first2', 2, 'relayout_source_page'), artifact('first3', 3, 'relayout_source_page'),
      artifact('earlier1'), artifact('earlier3', 3), artifact('second3', 3, 'relayout_source_page'), artifact('new1'), artifact('new3a', 3), artifact('new3b', 3)] });
  assert.deepEqual(panel(second).pairs(), { 1: { current: 'new1', options: ['source:first1'] }, 3: { current: 'new3b', options: ['source:second3', 'record:new3a'] } });
});
test('original pages are ignored outside a re-layout and when identical to the current image', () => {
  assert.deepEqual(panel(relayout({ relayout: null })).pairs(), { 3: { current: 'new3b', options: ['record:new3a'] } });
  const same = relayout(); same.artifacts.find(item => item.id === 'src1').sha256 = 'sha-new1';
  assert.equal(panel(same).pairs()[1], undefined);
});
test('the view shows the previous and the current image with labels, a selector and the change request', () => {
  const ui = panel(revisions());
  ui.click({ compareOpen: '1' });
  assert.deepEqual(ui.focused, ['wuyu-compare-before']);
  const html = ui.view(1);
  assert.match(ui.button(1), /aria-expanded="true"/);
  assert.match(html, /^<section id="wuyu-compare" class="wuyu-compare" role="region" aria-label="第 1 页对比">/);
  assert.deepEqual([...html.matchAll(/<span class="status-chip">([^<]+)<\/span>/g)].map(match => match[1]), ['修改前', '修改后']);
  assert.deepEqual([...html.matchAll(/<img [^>]*src="([^"]+)"/g)].map(match => match[1]), ['https://assets.fixture.invalid/a/p1b', 'https://assets.fixture.invalid/a/p1c']);
  assert.equal((html.match(/loading="lazy" decoding="async"/g) || []).length, 2);
  assert.deepEqual([...html.matchAll(/<option data-preview-key="compare-option-([^"]+)" value="([^"]+)" (selected)?/g)].map(match => [match[1], match[2], !!match[3]]),
    [['record:p1b', 'record:p1b', true], ['record:p1a', 'record:p1a', false]]);
  assert.match(html, /<p class="preview-note wuyu-compare-request" title="把标题 改成蓝色 并放大">本页最近的修改要求：把标题 改成蓝色 并放大<\/p>/);
  ui.fire('change', { target: { matches: selector => selector === '[data-compare-before]', value: 'record:p1a' } });
  const older = ui.view(1);
  assert.match(older, /src="https:\/\/assets\.fixture\.invalid\/a\/p1a"/);
  assert.doesNotMatch(older, /wuyu-compare-request/, 'the stored request only describes the latest revision');
});
test('the change request is omitted while the page is not ready or the text is not a string', () => {
  for (const change of [{ generation_status: 'pending' }, { requested_change: { text: 'x' } }, { requested_change: null }]) {
    const snapshot = revisions(); Object.assign(snapshot.workflow.pages[0], change);
    const ui = panel(snapshot); ui.click({ compareOpen: '1' });
    assert.doesNotMatch(ui.view(1), /wuyu-compare-request/);
  }
});
test('slider mode overlays both images behind one keyboard-operable range input', () => {
  const ui = panel(revisions());
  ui.click({ compareOpen: '1' }); ui.click({ compareMode: 'slider' });
  const html = ui.view(1);
  assert.match(html, /<div class="wuyu-compare-stage" data-compare-stage style="--wuyu-compare-pos:50%">/);
  assert.match(html, /<input type="range" id="wuyu-compare-range" data-compare-range min="0" max="100" step="1" value="50" aria-label="[^"]+">/);
  assert.equal((html.match(/<img /g) || []).length, 2);
  const styles = {};
  ui.fire('input', { target: { matches: selector => selector === '[data-compare-range]', value: '130', closest: () => ({ style: { setProperty: (name, value) => { styles[name] = value; } } }) } });
  assert.deepEqual(styles, { '--wuyu-compare-pos': '100%' });
  assert.match(ui.view(1), /--wuyu-compare-pos:100%/);
});
test('Escape closes the view and returns focus to the control; an open dialog keeps priority', () => {
  const ui = panel(revisions());
  ui.click({ compareOpen: '1' });
  let prevented = 0;
  ui.context.document.querySelector = selector => selector === 'dialog[open]' ? {} : null;
  ui.fire('keydown', { key: 'Escape', preventDefault: () => { prevented += 1; } });
  assert.equal(ui.context.m.previewCompare.page, 1);
  ui.context.document.querySelector = () => null;
  ui.fire('keydown', { key: 'Escape', preventDefault: () => { prevented += 1; } });
  assert.equal(prevented, 1);
  assert.equal(ui.context.m.previewCompare.page, 0);
  assert.deepEqual(ui.focused, ['wuyu-compare-before', 'wuyu-compare-open']);
  assert.equal(ui.view(1), '');
});
test('one Escape that closes a dialog above the comparison leaves the comparison open', () => {
  // The panel's own keydown handler is registered before the appended comparison code and closes the dialog first.
  const deployed = read('../dist/ui/index.html'), own = 'addEventListener("keydown",e=>{if(e.key==="Escape"){const n=[...document.querySelectorAll("dialog[open]")].at(-1);n&&(e.preventDefault(),n.close())}';
  assert.equal(deployed.split(own).length, 2);
  assert.ok(deployed.indexOf(own) < deployed.indexOf('function __wuyuCompareText('));
  const ui = panel(revisions()), dialogs = [{ close() { dialogs.pop(); } }];
  ui.context.document.querySelector = selector => selector === 'dialog[open]' ? dialogs.at(-1) || null : null;
  const main = event => { if (event.key === 'Escape') { const open = dialogs.at(-1); if (open) { event.preventDefault(); open.close(); } } };
  const press = () => { const event = { key: 'Escape', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }; main(event); ui.fire('keydown', event); return event; };
  ui.click({ compareOpen: '1' });
  assert.equal(press().defaultPrevented, true);
  assert.deepEqual([dialogs.length, ui.context.m.previewCompare.page, ui.focused], [0, 1, ['wuyu-compare-before']]);
  assert.match(ui.view(1), /id="wuyu-compare"/);
  press();
  assert.deepEqual([ui.context.m.previewCompare.page, ui.focused], [0, ['wuyu-compare-before', 'wuyu-compare-open']]);
  // A key some other handler already consumed is not acted on either.
  ui.click({ compareOpen: '1' });
  ui.fire('keydown', { key: 'Escape', defaultPrevented: true, preventDefault() { throw Error('already handled'); } });
  assert.equal(ui.context.m.previewCompare.page, 1);
});
test('selector options are keyed by record so an in-place update keeps the shown choice with its record', () => {
  // The panel updates the DOM in place and moves a child that carries one of these attributes instead of
  // rewriting whichever node sits at the same position.
  assert.match(read('../dist/ui/index.html'), /function fr\(e\)\{if\(e\.nodeType!==1\)return"";const n=e;for\(const r of\["id","data-preview-key",/);
  const snapshot = (...ids) => present({ workflow: { pages: [page(1, ids.at(-1), { image_version: ids.length })] }, artifacts: ids.map(id => artifact(id)) });
  const ui = panel(snapshot('A', 'B', 'C', 'D')); ui.click({ compareOpen: '1' });
  const keys = () => [...ui.view(1).matchAll(/<option data-preview-key="([^"]+)" value="([^"]+)" (selected)?/g)].map(match => [match[1], match[2], !!match[3]]);
  ui.fire('change', { target: { matches: selector => selector === '[data-compare-before]', value: 'record:B' } });
  assert.deepEqual(keys(), [['compare-option-record:C', 'record:C', false], ['compare-option-record:B', 'record:B', true], ['compare-option-record:A', 'record:A', false]]);
  ui.context.m.snapshot = snapshot('A', 'B', 'C', 'D', 'E');
  assert.deepEqual(keys(), [['compare-option-record:D', 'record:D', false], ['compare-option-record:C', 'record:C', false], ['compare-option-record:B', 'record:B', true], ['compare-option-record:A', 'record:A', false]]);
  assert.equal(new Set(keys().map(([key]) => key)).size, 4);
});
test('the view closes when its page stops being comparable or another project is shown', () => {
  const ui = panel(revisions());
  ui.click({ compareOpen: '2' });
  assert.equal(ui.context.m.previewCompare.page, 0, 'a page without a pair cannot be opened');
  ui.click({ compareOpen: '1' });
  ui.context.m.snapshot = present({ workflow: { pages: [page(1, 'p1c')] }, artifacts: [artifact('p1c')] });
  assert.equal(ui.view(1), '');
  ui.context.m.snapshot = revisions(); ui.click({ compareOpen: '1' });
  assert.equal(ui.context.m.previewCompare.page, 1);
  ui.context.m.snapshot = { ...revisions(), project_id: 'another-project' };
  assert.equal(ui.run('__wuyuCompareState().page'), 0);
});
test('the panel is view-only: no workflow call, message or restore action exists', () => {
  const source = read('../tools/page-compare-panel.js');
  assert.doesNotMatch(source, /\bTi\(|sendMessage|openLink|fetch\(|XMLHttpRequest|callServerTool|download=/);
  const ui = panel(revisions()); ui.click({ compareOpen: '1' });
  assert.deepEqual([...ui.view(1).matchAll(/<button [^>]*data-compare-(\w+)/g)].map(match => match[1]), ['mode', 'mode', 'close']);
});
test('English labels follow the document language', () => {
  const ui = panel(relayout(), 'en');
  assert.match(ui.button(3), />Compare with original</);
  ui.click({ compareOpen: '3' });
  assert.deepEqual([...ui.view(3).matchAll(/<span class="status-chip">([^<]+)<\/span>/g)].map(match => match[1]), ['Original', 'Restyled']);
  ui.fire('change', { target: { matches: selector => selector === '[data-compare-before]', value: 'record:new3a' } });
  assert.deepEqual([...ui.view(3).matchAll(/<span class="status-chip">([^<]+)<\/span>/g)].map(match => match[1]), ['Before', 'After']);
});
test('styles reuse the panel variables and contain no literal colours', () => {
  const css = read('../tools/page-compare.css');
  assert.doesNotMatch(css, /#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(/i);
  assert.deepEqual([...new Set([...css.matchAll(/var\((--[\w-]+)/g)].map(match => match[1]))].sort(), ['--accent', '--canvas', '--line', '--muted', '--wuyu-compare-pos']);
  assert.match(css, /@media\(max-width:600px\)\{\.wuyu-compare-side\{grid-template-columns:1fr\}/);
});
test('the transformer reproduces the deployed panel from its pinned baseline and is idempotent', () => {
  const deployed = read('../dist/ui/index.html'), baseline = baselineOf(deployed);
  assert.equal(sha(baseline), BASELINE_SHA);
  const first = transform(baseline), again = transform(deployed);
  assert.equal(first.html, deployed, 'dist/ui/index.html must be the exact output of tools/page-compare-ui.cjs');
  assert.equal(first.metadata.idempotent, false);
  assert.equal(again.html, deployed);
  assert.equal(again.metadata.idempotent, true);
  assert.equal(again.metadata.after, sha(deployed));
});
test('only the two slides-renderer fragments, the appended panel code and one style block differ', () => {
  const deployed = read('../dist/ui/index.html'), baseline = baselineOf(deployed);
  const style = '<style data-wuyu-page-compare>\n' + read('../tools/page-compare.css') + '\n</style>', code = '\n' + read('../tools/page-compare-panel.js');
  let rest = deployed.replace(style, '').replace(code + '</script>', '</script>');
  assert.equal(deployed.length - rest.length, style.length + code.length);
  rest = rest.replace('${__wuyuLockButton(n)}${__wuyuCompareButton(n)}', '${__wuyuLockButton(n)}').replace('${__wuyuCompareView(n)||Li(n.image_url,', '${Li(n.image_url,');
  assert.equal(rest, baseline);
  new vm.Script('async()=>{' + /<script type="module" crossorigin>([\s\S]*?)<\/script>/.exec(deployed)[1].replace(/\bimport\.meta\b/g, '({})') + '}');
});
test('any other document is refused', () => {
  const deployed = read('../dist/ui/index.html'), baseline = baselineOf(deployed);
  assert.throws(() => transform(baseline + '\n'), /baseline changed/);
  assert.throws(() => transform(baseline.replace('${__wuyuLockButton(n)}', '${__wuyuLockButton(n)} ')), /baseline changed|renderer changed/);
  assert.throws(() => transform(deployed.replace('wuyu-compare-open', 'wuyu-compare-0pen')), /baseline changed/);
  assert.throws(() => transform(deployed + deployed), /baseline changed|changed/);
});
