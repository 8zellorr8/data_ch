import test from 'node:test';
import assert from 'node:assert/strict';
import { buildSubmissionRequest } from '../submission.js';

const conversations = Array.from({ length: 10 }, (_, n) => ({ id: 'internal-' + n, conversation_id: 'external-' + n }));
const evaluations = conversations.map((conversation, n) => ({ id: 'eval-' + n, conversation_id: conversation.id, origin: 'ai', metadata: { model: 'actual-model' } }));
const selections = Object.fromEntries(conversations.map((conversation, n) => [conversation.id, { kind: 'evaluation', evaluationId: 'eval-' + n }]));
const params = () => ({ split: 'prod', teamName: 'Our team', modelMode: 'external_api', modelNames: 'actual-model', source: 'items', conversations, evaluations, selections });

test('submission uses external dialogue IDs, explicitly selected evaluations and original mode only', () => {
  const body = buildSubmissionRequest(params());
  assert.deepEqual(body.items[0], { conversation_id: 'external-0', evaluation_id: 'eval-0' });
  assert.equal(body.batch_id, undefined);
  assert.deepEqual(body.model_names, ['actual-model']);
});
test('explicit errors are preserved and do not become zero-score results', () => {
  const body = buildSubmissionRequest({ ...params(), selections: { ...selections, 'internal-0': { kind: 'error', error: ' AI_TIMEOUT ' } } });
  assert.deepEqual(body.items[0], { conversation_id: 'external-0', error: 'AI_TIMEOUT' });
});
test('missing/manual/foreign evaluations cannot be submitted', () => {
  assert.throws(() => buildSubmissionRequest({ ...params(), selections: {} }), /Выберите AI/);
  assert.throws(() => buildSubmissionRequest({ ...params(), evaluations: evaluations.map(e => ({ ...e, origin: 'manual' })) }), /Выберите AI/);
  assert.throws(() => buildSubmissionRequest({ ...params(), selections: { ...selections, 'internal-0': { evaluationId: 'eval-1' } } }), /Выберите AI/);
});
test('count, external IDs and actual model metadata must match', () => {
  assert.throws(() => buildSubmissionRequest({ ...params(), split: 'dev' }), /65/);
  assert.throws(() => buildSubmissionRequest({ ...params(), conversations: conversations.map(c => ({ ...c, conversation_id: 'duplicate' })) }), /повторяются/);
  assert.throws(() => buildSubmissionRequest({ ...params(), modelNames: 'invented' }), /actual-model/);
});
test('only a completed full-dataset batch is accepted, including terminal failures', () => {
  const batch = { id: 'batch', completed: true, items: conversations.map(c => ({ conversation_id: c.id, status: 'failed', error_code: 'AI_TIMEOUT' })) };
  const body = buildSubmissionRequest({ ...params(), source: 'batch', batch });
  assert.equal(body.batch_id, 'batch'); assert.equal(body.items, undefined);
  assert.throws(() => buildSubmissionRequest({ ...params(), source: 'batch', batch: { ...batch, completed: false } }), /выполняется/);
  assert.throws(() => buildSubmissionRequest({ ...params(), source: 'batch', batch: { ...batch, items: batch.items.map(item => ({ ...item, status: 'pending' })) } }), /выполняется/);
  assert.throws(() => buildSubmissionRequest({ ...params(), source: 'batch', batch: { ...batch, items: batch.items.slice(1) } }), /ровно все/);
  assert.throws(() => buildSubmissionRequest({ ...params(), source: 'batch', batch: { ...batch, items: batch.items.map(item => ({ ...item, conversation_id: 'internal-0' })) } }), /ровно все/);
});
