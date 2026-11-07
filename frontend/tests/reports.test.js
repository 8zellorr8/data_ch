import test from 'node:test';
import assert from 'node:assert/strict';
import {parseExecutionIds, qualityReportHTML, usageReportHTML} from '../reports.js';

const quality = {selected:1, compared:0, missing_or_mismatched:1, coverage:0, score_compared:0, score_null_excluded:0, mae:null, criteria:[], pairs:[{reference_id:'ref-1', ai_id:null, issue:'MISSING_AI', absolute_score_error:null}]};
const usage = {rows:[{execution:{id:'exec-1', conversation_id:'conv-1', status:'failed', usage:null, duration_ms:null}, tariff:null, known_cost:null, complete:false, limitations:['provider usage unavailable']}], known_cost_by_currency:{}, complete:false, known_duration_ms:0, duration_complete:false, known_calls:0, calls_complete:false, known_input_tokens:0, known_output_tokens:0, tokens_complete:false, missing_ids:[], pending_items:0};

test('unknown usage stays unknown, not a fabricated zero cost', () => {
  const html = usageReportHTML(usage);
  assert.match(html, /Неизвестный расход не равен нулю/);
  assert.match(html, /Нет данных/);
  assert.match(html, /Неизвестно/);
  assert.doesNotMatch(html, /0 USD|0 · модель не вызывалась/);
});
test('cancelled before start is an explicit known zero without invented currency', () => {
  const report = structuredClone(usage);
  Object.assign(report, {complete:true, calls_complete:true, tokens_complete:true, duration_complete:true});
  report.rows[0] = {execution:{id:'cancelled-before-start-1', conversation_id:'conv-1', status:'interrupted', provider_not_called:true, usage:null, duration_ms:0}, tariff:null, known_cost:'0', complete:true, limitations:[]};
  const html = usageReportHTML(report);
  assert.match(html, /0 · модель не вызывалась/);
  assert.match(html, /Отменено до запуска/);
  assert.doesNotMatch(html, /USD|KZT|RUB/);
});
test('money preserves decimal precision and currencies stay separate', () => {
  const report = structuredClone(usage);
  report.known_cost_by_currency = {USD:'0.000000000000000012345', KZT:'10.5'};
  const html = usageReportHTML(report);
  assert.match(html, /0,000000000000000012345 USD · 10,5 KZT/);
});
test('no comparable pairs keeps MAE absent and exposes exclusion reason', () => {
  const html = qualityReportHTML(quality);
  assert.match(html, /Точность модели по этому отчёту не измерена/);
  assert.match(html, /Нет AI-оценки/);
  assert.match(html, /MAE итогового балла<\/dt><dd>Нет данных/);
});
test('changed reference revision remains an explicit exclusion, zero MAE stays zero', () => {
  const report = structuredClone(quality);
  report.pairs[0].issue = 'REFERENCE_REVISION_CHANGED';
  assert.match(qualityReportHTML(report), /Ревизия ручного эталона изменилась/);
  report.mae = '0'; report.compared = 1; report.score_compared = 1; report.coverage = 1;
  assert.match(qualityReportHTML(report), /MAE итогового балла<\/dt><dd>0<\/dd>/);
});
test('server supplied labels and limitations are escaped', () => {
  const report = structuredClone(usage); report.rows[0].limitations = ['<script>alert(1)</script>'];
  assert.doesNotMatch(usageReportHTML(report), /<script>/);
  assert.match(usageReportHTML(report), /&lt;script&gt;/);
  assert.doesNotMatch(qualityReportHTML(quality, new Map([['ref-1','<img src=x>']])), /<img/);
});
test('execution IDs are validated and deduplicated before request', () => {
  assert.deepEqual(parseExecutionIds('one,two\none; three._:-1'), ['one','two','three._:-1']);
  assert.throws(() => parseExecutionIds(''), /Введите/);
  assert.throws(() => parseExecutionIds('id<script>'), /ID выполнения может/);
  assert.throws(() => parseExecutionIds(Array.from({length:1001}, (_, i) => `exec-${i}`).join('\n')), /1000/);
});
