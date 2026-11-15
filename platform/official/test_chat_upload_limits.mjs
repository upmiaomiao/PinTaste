/** Run from any directory: node --test platform/official/test_chat_upload_limits.mjs */
import test from 'node:test';
import assert from 'node:assert/strict';
import {chatUploadIssue, MIB} from './web/chatUploadLimits.mjs';
import english from './web/messages/chat.mjs';

const config = {max_files: 5, file_mb: 20, image_mb: 10, total_mb: 50};
const file = (name, size, type = '') => ({name, size, type});
const check = (incoming, existing = [], limits = config) => chatUploadIssue(limits, existing, incoming);

test('document byte boundary is inclusive and uses MiB', () => {
  assert.equal(MIB, 1048576);
  assert.equal(check([file('report.txt', 20 * MIB - 1)]), null);
  assert.equal(check([file('report.txt', 20 * MIB)]), null);
  assert.deepEqual(check([file('report.txt', 20 * MIB + 1)]), {
    message: '文档“{v0}”不能超过 {v1} MiB。', values: {v0: 'report.txt', v1: 20},
  });
});

test('image suffixes choose the image limit, not the browser MIME', () => {
  for (const ext of ['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp']) {
    assert.equal(check([file('image' + ext, 10 * MIB, 'text/plain')]), null);
    assert.deepEqual(check([file('image' + ext.toUpperCase(), 10 * MIB + 1, 'text/plain')]), {
      message: '图片“{v0}”不能超过 {v1} MiB。', values: {v0: 'image' + ext.toUpperCase(), v1: 10},
    });
  }
  assert.equal(check([file('report.txt', 11 * MIB, 'image/png')]), null);
});

test('existing attachments count toward the same message count and byte limits', () => {
  const four = Array.from({length: 4}, () => file('existing.txt', 1));
  assert.equal(check([file('fifth.txt', 1)], four), null);
  assert.deepEqual(check([file('fifth.txt', 1), file('sixth.txt', 1)], four), {
    message: '每轮最多 {v0} 个文件。', values: {v0: 5},
  });
  const existing = [file('existing.txt', 20 * MIB)];
  assert.equal(check([file('a.txt', 20 * MIB), file('b.txt', 10 * MIB)], existing), null);
  assert.deepEqual(check([file('a.txt', 20 * MIB), file('b.txt', 10 * MIB + 1)], existing), {
    message: '每轮附件合计不能超过 {v0} MiB。', values: {v0: 50},
  });
});

test('an empty or later invalid file rejects the entire selection without mutating inputs', () => {
  const good = Object.freeze(file('valid.txt', 1));
  const tooBig = Object.freeze(file('large.png', 10 * MIB + 1));
  const incoming = Object.freeze([good, tooBig]);
  const existing = Object.freeze([]);
  assert.deepEqual(check(incoming, existing), {
    message: '图片“{v0}”不能超过 {v1} MiB。', values: {v0: 'large.png', v1: 10},
  });
  assert.equal(incoming[0], good);assert.equal(incoming[1], tooBig);assert.equal(existing.length, 0);
  assert.deepEqual(check([file('empty.txt', 0)]), {
    message: '文件“{v0}”为空，请选择其他文件。', values: {v0: 'empty.txt'},
  });
});

test('preflight respects changed limits from the API instead of assuming defaults', () => {
  const limits = {...config, file_mb: 2, image_mb: 1};
  assert.equal(check([file('small.txt', 2 * MIB)], [], limits), null);
  assert.equal(check([file('small.txt', 2 * MIB + 1)], [], limits).values.v1, 2);
  assert.equal(check([file('small.png', MIB + 1)], [], limits).values.v1, 1);
});

test('missing configuration or invalid file sizes refuse rather than bypass preflight', () => {
  for (const limits of [null, {}, {...config, max_files: 0}, {...config, image_mb: NaN}, {...config, total_mb: Infinity}]) {
    assert.deepEqual(check([file('report.txt', 1)], [], limits), {message: '上传配置尚未就绪，请刷新后重试。'});
  }
  for (const size of [undefined, -1, '1', NaN, Infinity, 0.5]) {
    assert.deepEqual(check([file('report.txt', size)]), {message: '无法读取附件大小，请重新选择文件。'});
  }
});

test('file suffix handling remains consistent for paths and leading-dot names', () => {
  for (const name of ['folder/map.PNG', 'C:\\folder\\map.PNG', 'map.P\0NG', '..png', '😀'.repeat(90) + '.PNG']) {
    assert.equal(check([file(name, 10 * MIB + 1)]).values.v1, 10);
  }
  for (const name of ['.png', 'map.png.', 'a'.repeat(160) + '.png', '😀'.repeat(160) + '.PNG']) {
    assert.equal(check([file(name, 10 * MIB + 1)]), null);
  }
});

test('every new issue and the visible tooltip has a corresponding English message', () => {
  const issues = [
    check([file('image.png', 10 * MIB + 1)]), check([file('report.txt', 20 * MIB + 1)]),
    check([file('empty.txt', 0)]), check([file('invalid.txt', NaN)]),
    check([file('config.txt', 1)], [], null),
    check([file('a.txt', 20 * MIB), file('b.txt', 10 * MIB + 1)], [file('old.txt', 20 * MIB)]),
  ];
  for (const issue of issues) assert.ok(typeof english[issue.message] === 'string' && english[issue.message].length > 0);
  assert.equal(english['图片“{v0}”不能超过 {v1} MiB。'], 'Image “{v0}” cannot exceed {v1} MiB.');
  assert.equal(english['文档“{v0}”不能超过 {v1} MiB。'], 'Document “{v0}” cannot exceed {v1} MiB.');
  assert.equal(english['每轮最多 {v0} 个文件 · 单个文档 {v1} MiB / 单张图片 {v2} MiB · 合计 {v3} MiB'],
    'Per message: up to {v0} files · Each document {v1} MiB / image {v2} MiB · Total {v3} MiB');
});
