import test from 'node:test';
import assert from 'node:assert/strict';
import {api, retryAfterDeadline} from '../api.js';

test('Retry-After becomes an absolute deadline for seconds and HTTP dates', () => {
  const now = Date.UTC(2026, 10, 8, 12);
  assert.equal(retryAfterDeadline('60', now), now + 60000);
  assert.equal(retryAfterDeadline(' 0 ', now), now);
  assert.equal(retryAfterDeadline('Sun, 08 Nov 2026 12:30:00 GMT', now), now + 1800000);
  assert.equal(retryAfterDeadline('Sun, 08 Nov 2026 11:30:00 GMT', now), now - 1800000);
  for (const value of [null, undefined, '', ' ', '-1', '1.5', 'NaN', 'Infinity', '1e3', '12abc', '2026-11-08', '999999999999999999999', 'Wed, 32 Nov 2026 12:30:00 GMT']) {
    assert.equal(retryAfterDeadline(value, now), null, String(value));
  }
});

test('a model cooldown keeps error metadata without scheduling another request', async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  const start = Date.now();
  globalThis.fetch = async () => {
    calls++;
    return new Response(JSON.stringify({error:{code:'AI_UNAVAILABLE', message:'provider refused'}}), {
      status:502, headers:{'Content-Type':'application/json', 'Retry-After':'60', 'X-Request-ID':'request-1', 'X-Execution-ID':'execution-1'},
    });
  };
  try {
    await assert.rejects(api('/api/v1/evaluations', {base:'https://example.invalid', method:'POST', body:{conversation_id:'c'}}), error => {
      assert.equal(error.code, 'AI_UNAVAILABLE');
      assert.equal(error.status, 502);
      assert.equal(error.requestId, 'request-1');
      assert.equal(error.executionId, 'execution-1');
      assert.ok(error.retryUntil >= start + 60000 && error.retryUntil <= Date.now() + 60000);
      assert.match(error.message, /паузу/);
      return true;
    });
    assert.equal(calls, 1);
  } finally { globalThis.fetch = originalFetch; }
});

test('unavailable AI without a valid wait header remains an ordinary failure', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({error:{code:'AI_UNAVAILABLE'}}), {status:502, headers:{'Retry-After':'garbage'}});
  try {
    await assert.rejects(api('/test', {base:'https://example.invalid'}), error => {
      assert.equal(error.retryUntil, null);
      assert.equal(error.message, 'Модель недоступна. Оценка не сохранена.');
      return true;
    });
  } finally { globalThis.fetch = originalFetch; }
});
