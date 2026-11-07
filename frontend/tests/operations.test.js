import test from 'node:test';
import assert from 'node:assert/strict';
import {createOperationStore} from '../operations.js';
import {defaultApiBase} from '../api.js';
const memory = () => { const m = new Map(); return {getItem:k=>m.get(k)||null,setItem:(k,v)=>m.set(k,v),removeItem:k=>m.delete(k),key:i=>[...m.keys()][i],get length(){return m.size;}}; };
const body = {conversation_id:'c', checklist_id:'rules', checklist_version:1};
test('lost response survives reload with the same durable key and exact body', async () => {
  const storage=memory(), calls=[]; let fail=true;
  const request=async (path,options)=>{assert.equal(storage.length,1);calls.push(options);if(fail)throw Object.assign(new Error('lost'),{code:'NETWORK_ERROR'});return {id:'saved'};};
  const config={storage,getBase:()=> 'https://api.example',request,uuid:()=> 'key-1'};
  const first=createOperationStore(config), op=first.prepareOperation('evaluation:c:ai','/api/v1/evaluations',body);
  body.checklist_version=2;
  await assert.rejects(first.sendOperation(op),/lost/);
  fail=false;
  const second=createOperationStore(config), recovered=second.findOperation(op.scope);
  assert.equal((await second.sendOperation(recovered)).id,'saved');
  assert.equal(calls[0].headers['Idempotency-Key'],calls[1].headers['Idempotency-Key']);
  assert.deepEqual(calls[0].body,calls[1].body);assert.equal(calls[1].body.checklist_version,1);
  assert.equal(calls[1].timeoutMs,185000);
  assert.equal(second.findOperation(op.scope).resultId,'saved');
  second.clearOperation(op);assert.equal(second.findOperation(op.scope),null);
});
test('changed payload is blocked until user deliberately clears old operation', () => {
  const s=createOperationStore({storage:memory(),getBase:()=> 'base',uuid:()=> 'key'});
  s.prepareOperation('scope','/path',{a:1,b:2});
  assert.equal(s.prepareOperation('scope','/path',{b:2,a:1}).key,'key');
  assert.throws(()=>s.prepareOperation('scope','/path',{a:2}),{code:'OPERATION_PENDING'});
});
test('unavailable storage prevents POST; changing servers never replays to another server', async () => {
  let posts=0, base='first';
  const request=async()=>{posts++;return{id:'x'};};
  const bad=createOperationStore({storage:{getItem:()=>null,setItem:()=>{throw Error();}},getBase:()=>base,request,uuid:()=> 'k'});
  assert.throws(()=>bad.prepareOperation('s','/p',{a:1}),{code:'OPERATION_STORAGE_ERROR'});
  const s=createOperationStore({storage:memory(),getBase:()=>base,request,uuid:()=> 'k'}), op=s.prepareOperation('s','/p',{a:1});
  base='second';await assert.rejects(s.sendOperation(op),{code:'SERVER_CHANGED'});assert.equal(posts,0);assert.equal(s.findOperation('s'),null);
});
test('failed call keeps execution ID for usage reporting', async () => {
  const s=createOperationStore({storage:memory(),getBase:()=> 'base',uuid:()=> 'k',request:async()=>{throw Object.assign(Error('failed'),{code:'AI_TIMEOUT',executionId:'execution-1'});}});
  const op=s.prepareOperation('s','/p',{a:1});await assert.rejects(s.sendOperation(op));assert.equal(s.findOperation('s').error.executionId,'execution-1');
});
test('hosted and local proxy pages use their own origin', () => {
  assert.equal(defaultApiBase({protocol:'https:',hostname:'demo.example',origin:'https://demo.example'}),'https://demo.example');
  assert.equal(defaultApiBase({protocol:'http:',hostname:'127.0.0.1',origin:'http://127.0.0.1:5173'}),'http://127.0.0.1:5173');
});
