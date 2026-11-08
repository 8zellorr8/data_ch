import test from 'node:test';
import assert from 'node:assert/strict';
import {createBatchController, batchCounts, batchItemLabel, batchRetrySummary, canResumeBatchItem, getLegacyBatchTasks} from '../batch.js';
import {createOperationStore} from '../operations.js';

const base = 'https://example.invalid';
const endpoint = '/api/v1/evaluation-batches';
const chosen = {items:[{id:'a'}, {id:'b'}, {id:'c'}], checklistId:'rules', checklistVersion:3};
function record(statuses = ['pending', 'pending', 'pending']) {
  return {id:'batch-1', created_at:'2026-11-07T00:00:00Z', checklist_id:'rules', checklist_version:3,
    completed:statuses.every(status => !['pending', 'running'].includes(status)),
    items:statuses.map((status, i) => ({id:'item-' + i, conversation_id:['a','b','c'][i], status,
      evaluation_id:status === 'succeeded' ? 'evaluation-' + i : null, error_code:status === 'failed' ? 'AI_INVALID_RESULT' : null}))};
}
function storage() {
  const data = new Map();
  return {getItem:key => data.get(key) || null, setItem:(key,value) => data.set(key,value), removeItem:key => data.delete(key)};
}
function scheduler() {
  const timers = new Map(); let next = 0;
  return {schedule:fn => {timers.set(++next,fn); return next;}, unschedule:id => timers.delete(id), size:() => timers.size,
    async tick() { const fn = timers.values().next().value; timers.clear(); if (fn) await fn(); await new Promise(resolve => setImmediate(resolve)); }};
}
function setup(request, options = {}) {
  const schedule = scheduler();
  const operations = createOperationStore({storage:options.storage || storage(), getBase:options.getBase || (() => base), request, uuid:() => 'stable-key'});
  const controller = createBatchController({request, getBase:options.getBase || (() => base), operations, ...schedule, ...options});
  return {controller, schedule, operations};
}
function deferred() { let resolve; const promise = new Promise(r => {resolve = r;}); return {promise,resolve}; }

test('creates one pinned server batch and then reads current status', async () => {
  const calls = [];
  const {controller,schedule} = setup(async (path,options = {}) => {
    calls.push({path,options}); return options.method === 'POST' ? record() : record(['succeeded','failed','interrupted']);
  });
  const state = await controller.start(chosen);
  assert.equal(calls.length,2);
  assert.equal(calls[0].path,endpoint);
  assert.deepEqual(calls[0].options.body,{conversation_ids:['a','b','c'],checklist_id:'rules',checklist_version:3});
  assert.equal(calls[0].options.headers['Idempotency-Key'],'stable-key');
  assert.equal(calls[1].path,endpoint + '/batch-1');
  assert.equal(state.record.completed,true); assert.equal(state.counts.succeeded,1); assert.equal(state.counts.failed,1);
  assert.equal(state.counts.interrupted,1); assert.equal(schedule.size(),0);
  await assert.rejects(controller.start(chosen),/уже создаётся или создан/);
});

test('polls active work and stops when all items are terminal, including failures', async () => {
  let reads = 0;
  const {controller,schedule} = setup(async () => ++reads === 1 ? record(['running','pending','pending']) : record(['succeeded','failed','interrupted']));
  await controller.refresh('batch-1'); assert.equal(schedule.size(),1);
  await schedule.tick();
  assert.equal(reads,2); assert.equal(schedule.size(),0); assert.equal(controller.getState().counts.finished,3);
});

test('lost creation response is recovered after reload with same key and body even if AI is unavailable', async () => {
  const saved = storage(), posts = []; let lose = true;
  const request = async (path,options = {}) => {
    if (options.method === 'POST') {
      posts.push(options);
      if (lose) {lose = false; throw Object.assign(new Error('connection lost'),{code:'NETWORK_ERROR'});}
      return record(); // Idempotent replay is the old snapshot, not the current one.
    }
    assert.equal(path,endpoint + '/batch-1'); return record(['succeeded','succeeded','failed']);
  };
  const first = setup(request,{storage:saved});
  await assert.rejects(first.controller.start(chosen),/connection lost/);
  first.controller.dispose();
  const second = setup(request,{storage:saved});
  const state = await second.controller.retryCreation();
  assert.equal(posts.length,2);
  assert.equal(posts[0].headers['Idempotency-Key'],posts[1].headers['Idempotency-Key']);
  assert.deepEqual(posts[0].body,posts[1].body);
  assert.equal(state.counts.succeeded,2); assert.equal(state.counts.failed,1);
  assert.equal(second.operations.findOperation('batch:create'),null);
});

test('cancellation is a server request and continues observing the already running item', async () => {
  const calls = [];
  const {controller,schedule} = setup(async (path,options = {}) => {
    calls.push({path,options});
    if (options.method === 'POST') return record(['running','interrupted','interrupted']);
    return record(['running','pending','pending']);
  });
  await controller.refresh('batch-1');
  const state = await controller.cancel();
  assert.equal(calls[1].path,endpoint + '/batch-1/cancel'); assert.equal(calls[1].options.method,'POST');
  assert.equal(calls[1].options.body,undefined);
  assert.equal(state.record.completed,false); assert.equal(state.counts.running,1); assert.equal(state.counts.interrupted,2);
  assert.equal(schedule.size(),1); controller.dispose();
});

test('a slow pre-cancellation poll cannot overwrite the newer cancellation response', async () => {
  const stale = deferred(); let reads = 0;
  const {controller} = setup(async (path,options = {}) => {
    if (options.method === 'POST') return record(['running','interrupted','interrupted']);
    if (++reads === 1) return record(['running','pending','pending']);
    return stale.promise;
  });
  await controller.refresh('batch-1');
  const reading = controller.refresh();
  await controller.cancel(); stale.resolve(record(['running','pending','pending'])); await reading;
  assert.equal(controller.getState().counts.interrupted,2); controller.dispose();
});

test('leaving the view stops polling and never sends cancellation', async () => {
  const calls = [];
  const {controller,schedule} = setup(async (path,options = {}) => {calls.push(options.method || 'GET');return record();});
  await controller.refresh('batch-1'); assert.equal(schedule.size(),1);
  controller.dispose(); assert.equal(schedule.size(),0); await schedule.tick();
  assert.deepEqual(calls,['GET']);
});

test('pausing monitoring stops reads and resuming observes completed server work without cancelling it', async () => {
  const calls = []; let completed = false;
  const {controller,schedule} = setup(async (path,options = {}) => {
    calls.push({path,method:options.method || 'GET'});
    return record(completed ? ['succeeded','failed','succeeded'] : ['running','pending','pending']);
  });
  await controller.refresh('batch-1');
  controller.pause();
  assert.equal(controller.getState().paused,true); assert.equal(schedule.size(),0);
  completed = true; await schedule.tick();
  assert.equal(calls.length,1); assert.equal(controller.getState().record.completed,false);
  const state = await controller.resume();
  assert.equal(state.paused,false); assert.equal(state.record.completed,true); assert.equal(schedule.size(),0);
  assert.deepEqual(calls,[{path:endpoint+'/batch-1',method:'GET'},{path:endpoint+'/batch-1',method:'GET'}]);
  controller.dispose();
});

test('a poll already in flight cannot restart automatic reads after pausing', async () => {
  const pending = deferred(); let reads = 0;
  const {controller,schedule} = setup(async () => ++reads === 1 ? record() : pending.promise);
  await controller.refresh('batch-1');
  const reading = controller.refresh(); controller.pause();
  pending.resolve(record(['succeeded','running','pending'])); await reading;
  assert.equal(controller.getState().counts.succeeded,1);
  assert.equal(controller.getState().paused,true); assert.equal(schedule.size(),0);
  await schedule.tick(); assert.equal(reads,2);
  controller.dispose();
});

test('stale server response is discarded after changing API base', async () => {
  let current = base; const pending = deferred();
  const {controller,schedule} = setup(() => pending.promise,{getBase:() => current});
  const reading = controller.refresh('batch-1'); current = 'https://another.invalid'; pending.resolve(record()); await reading;
  assert.equal(controller.getState().record,null); assert.equal(schedule.size(),0);
  await assert.rejects(controller.start(chosen),/Адрес сервера изменился/);
});

test('validation and unavailable storage prevent any POST', async () => {
  let posts = 0;
  const {controller} = setup(async () => {posts++;return record();},{storage:{getItem:() => null,setItem:() => {throw new Error('quota');}}});
  await assert.rejects(controller.start({...chosen,items:Array.from({length:101},(_,i) => ({id:'id'+i}))}),/от 1 до 100/);
  await assert.rejects(controller.start({...chosen,items:[{id:'a'},{id:'a'}]}),/разных диалогов/);
  await assert.rejects(controller.start(chosen),/Не удалось сохранить ключ/);
  assert.equal(posts,0);
});

test('failed polling preserves last progress and schedules a read-only retry', async () => {
  let calls = 0;
  const {controller,schedule} = setup(async () => {if (++calls === 1) return record(['succeeded','running','pending']); throw new Error('network');});
  await controller.refresh('batch-1');
  await assert.rejects(controller.refresh(),/network/);
  assert.equal(controller.getState().counts.succeeded,1); assert.equal(schedule.size(),1);
  controller.dispose();
});

test('cancelled-before-start is distinct from processing failure and counts as terminal', () => {
  const item = {status:'interrupted',error_code:'BATCH_CANCELLED_BEFORE_START'};
  assert.equal(batchItemLabel(item),'Отменено до запуска');
  assert.equal(batchItemLabel({status:'interrupted',error_code:'WORKER_INTERRUPTED'}),'Прервано');
  const counts = batchCounts({items:[item,{status:'failed'},{status:'pending'}]});
  assert.equal(counts.finished,2); assert.equal(counts.failed,1); assert.equal(counts.interrupted,1);
});

test('old browser journal keeps running and uncertain tasks visible until explicit review', () => {
  const originalSession = globalThis.sessionStorage, originalLocal = globalThis.localStorage;
  const store = storage();
  globalThis.sessionStorage = store; globalThis.localStorage = {getItem:() => base};
  try {
    const key = 'ketnavr-batch-v1:' + encodeURIComponent(base);
    store.setItem(key,JSON.stringify({schema:1,tasks:[{id:'a',status:'running'},{id:'b',status:'uncertain'},{id:'c',status:'success'},{id:'d',status:'uncertain',reviewed_at:'2026-11-07'}]}));
    assert.deepEqual(getLegacyBatchTasks().map(item => item.id),['a','b']);
    store.setItem(key,'{}');
    assert.throws(() => getLegacyBatchTasks(),/Не удалось прочитать журнал/);
  } finally { globalThis.sessionStorage = originalSession; globalThis.localStorage = originalLocal; }
});

function limitedRecord(status = 'failed') {
  const value = record(['succeeded', status, 'interrupted']);
  Object.assign(value.items[1], {error_code:'AI_QUOTA_EXHAUSTED', retry_at:2000000000, rate_limit_retries:3});
  value.items[2].error_code = 'BATCH_CANCELLED_BEFORE_START';
  return value;
}

test('limit waits, quota exhaustion and cancellation have distinct states', () => {
  assert.equal(batchItemLabel({status:'pending',error_code:'AI_RATE_LIMITED'}),'Ожидание лимита');
  assert.equal(batchItemLabel({status:'pending',error_code:'AI_QUOTA_EXHAUSTED'}),'Ожидание квоты');
  assert.equal(batchItemLabel({status:'failed',error_code:'AI_QUOTA_EXHAUSTED',evaluation_id:null}),'Квота исчерпана');
  assert.equal(batchItemLabel({status:'interrupted',error_code:'RESTORE_REQUIRES_REVIEW'}),'Требует проверки');
  assert.equal(batchItemLabel({status:'failed',error_code:'AI_INVALID_RESULT'}),'Ошибка');
  assert.deepEqual(batchRetrySummary(limitedRecord()),{waiting:0,resumable:1});
  assert.deepEqual(batchRetrySummary(limitedRecord('pending')),{waiting:1,resumable:0});
});

test('resume eligibility never includes existing evaluations, ordinary errors, interrupted or pending work', () => {
  for (const status of ['pending','running','succeeded','interrupted']) assert.equal(canResumeBatchItem({status,error_code:'AI_RATE_LIMITED'}),false);
  for (const error_code of ['AI_UNAVAILABLE','AI_INVALID_RESULT','AI_TIMEOUT','BATCH_CANCELLED_BEFORE_START']) assert.equal(canResumeBatchItem({status:'failed',error_code}),false);
  assert.equal(canResumeBatchItem({status:'failed',error_code:'AI_RATE_LIMITED',evaluation_id:'existing'}),false);
  assert.equal(canResumeBatchItem({status:'failed',error_code:'AI_RATE_LIMITED',evaluation_id:null}),true);
  assert.equal(canResumeBatchItem({status:'failed',error_code:'AI_QUOTA_EXHAUSTED'}),true);
});

test('manual continuation uses the existing batch and preserves successes, cancellation and cooldown', async () => {
  const calls = [];
  const {controller,schedule} = setup(async (path,options = {}) => {
    calls.push({path,options});
    return options.method === 'POST' ? limitedRecord('pending') : limitedRecord();
  });
  await controller.refresh('batch-1'); controller.pause();
  const state = await controller.resumeBatch();
  assert.deepEqual(calls.map(call => [call.path,call.options.method || 'GET']),[[endpoint+'/batch-1','GET'],[endpoint+'/batch-1/resume','POST']]);
  assert.equal(calls[1].options.body,undefined);
  assert.equal(state.record.items[0].evaluation_id,'evaluation-0');
  assert.equal(state.record.items[2].status,'interrupted');
  assert.equal(state.record.items[1].retry_at,2000000000);
  assert.equal(state.paused,false); assert.equal(schedule.size(),1);
  controller.dispose();
});

test('continuation is available while other batch items are running', async () => {
  const value = limitedRecord(); value.items[2].status = 'running'; value.completed = false;
  let posts = 0;
  const {controller} = setup(async (path,options = {}) => {if(options.method === 'POST') posts++; return value;});
  await controller.refresh('batch-1'); await controller.resumeBatch();
  assert.equal(posts,1); controller.dispose();
});

test('pending quota waits and unrelated failures never trigger resume POST', async () => {
  let posts = 0, current = limitedRecord('pending');
  const {controller} = setup(async (path,options = {}) => {if(options.method === 'POST') posts++;return current;});
  await controller.refresh('batch-1'); await controller.resumeBatch();
  current = record(['failed','interrupted','succeeded']);
  await controller.refresh(); await controller.resumeBatch();
  assert.equal(posts,0); controller.dispose();
});

test('double click and conflicting cancel cannot issue extra writes while resuming', async () => {
  const pending = deferred(); let posts = 0;
  const {controller} = setup(async (path,options = {}) => {
    if (options.method === 'POST') {posts++;return pending.promise;}
    return limitedRecord();
  });
  await controller.refresh('batch-1');
  const resuming = controller.resumeBatch();
  assert.equal(controller.getState().resuming,true);
  await controller.resumeBatch(); await controller.cancel(); await controller.refresh();
  assert.equal(posts,1);
  pending.resolve(limitedRecord('pending')); await resuming;
  assert.equal(controller.getState().resuming,false); controller.dispose();
});

test('slow poll cannot overwrite a newer resume response', async () => {
  const stale = deferred(); let reads = 0;
  const {controller} = setup(async (path,options = {}) => {
    if (options.method === 'POST') return limitedRecord('pending');
    return ++reads === 1 ? limitedRecord() : stale.promise;
  });
  await controller.refresh('batch-1');
  const reading = controller.refresh(); await controller.resumeBatch();
  stale.resolve(limitedRecord()); await reading;
  assert.equal(controller.getState().record.items[1].status,'pending'); controller.dispose();
});

test('lost resume response reconciles with GET and never automatically grants a second retry budget', async () => {
  const calls = []; let reads = 0;
  const {controller,schedule} = setup(async (path,options = {}) => {
    calls.push({path,method:options.method || 'GET'});
    if (options.method === 'POST') throw Object.assign(Error('connection lost'),{code:'NETWORK_ERROR'});
    return ++reads === 1 ? limitedRecord() : limitedRecord('pending');
  });
  await controller.refresh('batch-1');
  await assert.rejects(controller.resumeBatch(),/connection lost/);
  assert.equal(controller.getState().record.items[1].status,'pending');
  assert.equal(controller.getState().resumeNeedsRefresh,false);
  await schedule.tick();
  assert.equal(calls.filter(call => call.method === 'POST').length,1);
  assert.equal(calls[2].method,'GET'); controller.dispose();
});

test('unresolved resume outcome blocks more writes until a successful state read', async () => {
  let reads = 0, posts = 0, offline = true;
  const {controller} = setup(async (path,options = {}) => {
    if (options.method === 'POST') {posts++;throw Error('lost');}
    if (++reads > 1 && offline) throw Error('offline');
    return limitedRecord();
  });
  await controller.refresh('batch-1'); await assert.rejects(controller.resumeBatch());
  assert.equal(controller.getState().resumeNeedsRefresh,true);
  await controller.resumeBatch(); assert.equal(posts,1);
  offline = false; await controller.refresh();
  assert.equal(controller.getState().resumeNeedsRefresh,false);
  assert.equal(controller.getState().warning,''); controller.dispose();
});
