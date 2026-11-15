import test from 'node:test';
import assert from 'node:assert/strict';
import {SubmissionKeys} from './submission-keys.mjs';

test('submission identities survive transport failure and isolate users and changed requests', () => {
  let n = 0;
  const keys = new SubmissionKeys(() => String(++n));
  const first = keys.begin('a','/tasks','POST','payload');
  assert.equal(keys.begin('a','/tasks','POST','payload').key, first.key);
  assert.notEqual(keys.begin('b','/tasks','POST','payload').key, first.key);
  assert.notEqual(keys.begin('a','/tasks','POST','changed').key, first.key);
  keys.finish(first, {ok:false,status:409,headers:new Headers({'X-Submission-State':'pending'})});
  assert.equal(keys.begin('a','/tasks','POST','payload').key, first.key);
  keys.finish(first, {ok:true,status:202,headers:new Headers()});
  assert.notEqual(keys.begin('a','/tasks','POST','payload').key, first.key);
  assert.equal(keys.begin('a','/config','PATCH','{}'), null);
});
