import assert from 'node:assert/strict';
import test from 'node:test';
import {workDestination,workSsoDestination} from './workNavigation.mjs';

test('work navigation follows the current environment without rebuilding', () => {
  for (const origin of ['https://ai.wuyullm.com','https://test-ai.wuyullm.com','https://preview.example.test:9443']) {
    assert.equal(workDestination(undefined,origin).href,origin+'/work');
    assert.equal(workDestination('/work?templates=review',origin).href,origin+'/work?templates=review');
    assert.equal(workDestination('/work/admin',origin).href,origin+'/work/admin');
    assert.equal(workSsoDestination(origin+'/work-auth/sso?next=%2Fwork',origin).href,origin+'/work-auth/sso?next=%2Fwork');
  }
});

test('work destinations preserve cross-origin, path and fragment rejection', () => {
  const origin='https://test-ai.wuyullm.com';
  for (const path of ['https://ai.wuyullm.com/work','https://evil.test/work','//evil.test/work','javascript:alert(1)','/admin','/work#fragment']) {
    assert.throws(()=>workDestination(path,origin));
  }
});

test('SSO responses must remain on the current origin and expected endpoint', () => {
  const origin='https://test-ai.wuyullm.com';
  for (const path of ['https://ai.wuyullm.com/work-auth/sso','https://evil.test/work-auth/sso',origin+'/work',origin+'/work-auth/sso#fragment','/work-auth/sso']) {
    assert.throws(()=>workSsoDestination(path,origin));
  }
});
