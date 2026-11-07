import test from 'node:test';
import assert from 'node:assert/strict';
import { datasetRows, filterRows } from '../dialogs-model.js';

const conversation = i => ({id: String(i), conversation_id: 'QA-' + String(i).padStart(3, '0'), language: 'RU', channel: 'agent chat', transcript: [{text: 'Do not retain this transcript'}]});
const evaluation = (id, score, options = {}) => ({
  id: 'e' + id, conversation_id: String(id), created_at: '2026-01-01T00:00:00Z', origin: 'manual',
  checklist: {checklist_id: 'rules', version: 1},
  score: {total: score, flags: options.flag ? ['flag'] : []},
  final: {answers: [{criterion_id: 'flag', value: options.flag ? 'yes' : 'no', needs_review: !!options.review}]},
});
test('filters and score sorting cover the full dataset before pagination; zero is not missing', () => {
  const rows = datasetRows({conversations: Array.from({length: 25}, (_, i) => conversation(i + 1)), evaluations: [evaluation(24, 0), evaluation(25, 100), evaluation(23, null)]});
  assert.equal(filterRows(rows, {sort: 'asc'})[0].id, '24');
  assert.equal(filterRows(rows, {sort: 'desc'})[0].id, '25');
  assert.equal(filterRows(rows, {status: 'unrated'}).length, 22);
  assert.equal(rows.find(row => row.id === '23').evaluation.score, null);
  assert.ok(rows.every(row => !('transcript' in row)));
});
test('uncertain flagged answers are review items, not confirmed coarse violations', () => {
  const rows = datasetRows({conversations: [conversation(1), conversation(2)], evaluations: [evaluation(1, 100, {flag: true, review: true}), evaluation(2, 100, {flag: true})]});
  assert.deepEqual(filterRows(rows, {status: 'review'}).map(row => row.id), ['1']);
  assert.deepEqual(filterRows(rows, {status: 'violation'}).map(row => row.id), ['2']);
});
test('latest evaluation only; final result after appeal is used', () => {
  const old = evaluation(1, 0, {flag: true});
  const latest = {...evaluation(1, 95), id: 'new', created_at: '2026-01-02T00:00:00Z', original_score: {total: 0}};
  const rows = datasetRows({conversations: [conversation(1)], evaluations: [latest, old]});
  assert.equal(rows[0].evaluation.score, 95);
  assert.equal(rows[0].evaluation.hasViolation, false);
});
