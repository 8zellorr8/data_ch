import test from 'node:test';
import assert from 'node:assert/strict';
import { draftFingerprint, readDraft, writeDraft, removeDraft } from '../drafts.js';

const data = new Map();
globalThis.sessionStorage = {getItem: key => data.get(key) ?? null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key)};
const scope = {server: 'http://localhost:8001', kind: 'manual', entity: 'dialog:rules', version: 1, fingerprint: draftFingerprint({revision: 1, rule: 'initial'})};

test('draft survives a new reader, is bound to server/entity/version/revision and can be removed', () => {
  data.clear();
  writeDraft(scope, {author: 'QA', answers: [{value: 'no'}]});
  assert.equal(readDraft({...scope}).data.author, 'QA');
  assert.equal(readDraft(scope).compatible, true);
  assert.equal(readDraft({...scope, version: 2}).compatible, false);
  assert.equal(readDraft({...scope, fingerprint: draftFingerprint({revision: 2, rule: 'initial'})}).compatible, false);
  assert.equal(readDraft({...scope, entity: 'other:rules'}), null);
  assert.equal(readDraft({...scope, server: 'http://localhost:8002'}), null);
  removeDraft(scope);
  assert.equal(readDraft(scope), null);
});
test('fingerprint is stable for key order without persisting source contents', () => {
  assert.equal(draftFingerprint({a: 1, b: 2}), draftFingerprint({b: 2, a: 1}));
  assert.notEqual(draftFingerprint({a: 1, b: 2}), draftFingerprint({a: 2, b: 1}));
  data.clear(); writeDraft(scope, {author: 'QA'});
  assert.ok(![...data.values()][0].includes('initial'));
});
test('storage failures are visible rather than reported as successful persistence', () => {
  const previous = globalThis.sessionStorage;
  globalThis.sessionStorage = {setItem() { throw new Error('Quota'); }, getItem() { throw new Error('Disabled'); }, removeItem() { throw new Error('Disabled'); }};
  try {
    assert.throws(() => writeDraft(scope, {author: 'QA'}), /Не удалось сохранить/);
    assert.throws(() => readDraft(scope), /не разрешает читать/);
    assert.throws(() => removeDraft(scope), /не разрешает удалить/);
  } finally { globalThis.sessionStorage = previous; }
});
