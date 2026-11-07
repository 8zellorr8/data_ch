import {api, getApiBase} from './api.js';
import {esc, icon, dateText, scoreText, downloadJson} from './ui.js';

const encode = encodeURIComponent;
const percent = value => value == null ? '—' : new Intl.NumberFormat('ru-RU', {style:'percent', maximumFractionDigits:2}).format(value);
const decimal = value => value == null ? 'Неизвестно' : String(value).replace('.', ',');
const issueNames = {
  MISSING_REFERENCE:'Ручной эталон не найден', MISSING_AI:'Нет AI-оценки', WRONG_ORIGIN:'Неверный источник оценки',
  REFERENCE_REVISION_CHANGED:'Ревизия ручного эталона изменилась — перечитайте оценки и выберите её заново',
  INPUT_MISMATCH:'Не совпадают диалог, версия правил или снимок входных данных',
};
const statusNames = {pending:'Ожидает', running:'Выполняется', succeeded:'Готово', failed:'Ошибка', interrupted:'Прервано'};
const limitationNames = {USAGE_UNKNOWN:'Модель не передала данные о расходе', ADAPTER_USAGE_INCOMPLETE:'Модель не подтвердила учёт всех обращений', TARIFF_UNKNOWN:'Для модели не задан тариф', TOKENS_UNKNOWN:'Число токенов неизвестно', EXECUTION_INCOMPLETE:'Выполнение ещё не завершено'};
const metric = (label, value, note = '') => `<div class="report-metric"><dt>${esc(label)}</dt><dd>${esc(value)}</dd>${note ? `<small>${esc(note)}</small>` : ''}</div>`;
const reportTable = (head, rows) => `<div class="report-table-block"><p class="report-scroll-hint">Таблица шире экрана · прокрутите вправо, чтобы увидеть все столбцы</p><div class="table-wrap report-table-wrap" tabindex="0" role="region" aria-label="${esc(head.join(', '))}"><table class="report-table"><thead><tr>${head.map(h => `<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div></div>`;

export function parseExecutionIds(text) {
  const ids = [...new Set(text.split(/[\s,;]+/).filter(Boolean))];
  if (!ids.length) throw new Error('Введите хотя бы один ID выполнения из X-Execution-ID.');
  if (ids.length > 1000) throw new Error('За один запрос можно выбрать не более 1000 выполнений.');
  if (ids.some(id => !/^[A-Za-z0-9._:-]{1,128}$/.test(id))) throw new Error('ID выполнения может содержать латинские буквы, цифры, точку, дефис, двоеточие и подчёркивание.');
  return ids;
}

export function qualityReportHTML(report, labels = new Map(), criterionLabels = new Map()) {
  const label = id => labels.get(id) || id;
  return `<div class="report-result-heading"><div><h3>Результат сравнения</h3><p class="muted">Внутренние метрики команды · не официальный рейтинг хакатона</p></div><button type="button" class="button secondary" data-report-download="quality">${icon('download')}Скачать отчёт</button></div>
    <dl class="report-metrics">${metric('Покрытие', percent(report.coverage), `${report.compared} из ${report.selected} выбранных пар`)}${metric('MAE итогового балла', report.mae == null ? 'Нет данных' : decimal(report.mae), `Сравнено баллов: ${report.score_compared}`)}${metric('Исключено пар', scoreText(report.missing_or_mismatched), `Нет итогового балла (null): ${report.score_null_excluded}`)}</dl>
    ${report.compared === 0 ? '<p class="notice amber">Нет сопоставимых пар. Точность модели по этому отчёту не измерена.</p>' : ''}
    <p class="report-help">MAE — средняя абсолютная разница итоговых баллов. Сравнивается исходная AI-оценка с итоговой ручной оценкой выбранной ревизии. Пропуски не считаются нулевой ошибкой.</p>
    <h3>Выбранные пары</h3>${reportTable(['Ручной эталон','AI-оценка','Результат','Разница баллов'], report.pairs.map(p => `<tr><td>${esc(label(p.reference_id))}</td><td>${p.ai_id ? esc(label(p.ai_id)) : 'Не выбрана'}${p.model ? `<div class="row-meta">${esc(p.model)} · ${esc(p.prompt_version || 'Версия промпта не указана')}</div>` : ''}</td><td>${p.issue ? `<span class="report-issue">${esc(issueNames[p.issue] || p.issue)}</span><div class="row-meta">${esc(p.issue)}</div>` : 'Сравнено'}</td><td>${p.absolute_score_error == null ? '—' : esc(decimal(p.absolute_score_error))}</td></tr>`).join(''))}
    ${report.criteria.length ? `<h3 class="report-subheading">По критериям и версиям правил</h3>${reportTable(['Критерий','Совпадения','Точность','Ошибки N/A','Флаги'], report.criteria.map(c => `<tr><td>${esc(criterionLabels.get(JSON.stringify([c.checklist_id,c.checklist_version,c.criterion_id])) || c.criterion_id)}<div class="row-meta">${esc(c.checklist_id)} · v${esc(c.checklist_version)}</div></td><td>${esc(c.matches)} / ${esc(c.compared)}</td><td>${esc(percent(c.accuracy))}</td><td>Лишние: ${esc(c.na_false_positive)}<br>Пропущены: ${esc(c.na_false_negative)}</td><td>${c.kind === 'flag' ? `Сравнено: ${esc(c.flag_compared)}<br>Ложные: ${esc(c.flag_false_positive)} · Пропущены: ${esc(c.flag_false_negative)}<br>N/A исключены: ${esc(c.flag_na_excluded)}` : 'Не флаг'}</td></tr>`).join(''))}` : ''}`;
}

export function usageReportHTML(report) {
  const costEntries = Object.entries(report.known_cost_by_currency);
  const cost = costEntries.length ? costEntries.map(([currency, value]) => `${decimal(value)} ${currency}`).join(' · ') : report.complete && report.rows.every(row => row.execution.provider_not_called) && report.rows.length ? '0 · модель не вызывалась' : 'Нет данных';
  const known = (value, complete) => value === 0 && !complete ? 'Нет данных' : scoreText(value);
  const rows = report.rows.map(row => {
    const run = row.execution, usage = run.usage, noCall = run.provider_not_called === true;
    const calls = noCall ? '0' : usage?.calls == null ? 'Неизвестно' : scoreText(usage.calls);
    const tokens = noCall ? '0 / 0' : `${usage?.input_tokens == null ? '—' : scoreText(usage.input_tokens)} / ${usage?.output_tokens == null ? '—' : scoreText(usage.output_tokens)}`;
    return `<tr><td>${esc(usage?.model || (noCall ? 'Без обращения к модели' : 'Модель не указана'))}<div class="row-meta">${esc(run.id)}</div>${run.evaluation_id ? `<a href="#review/${encode(run.conversation_id)}/${encode(run.evaluation_id)}">Открыть оценку</a>` : ''}</td><td>${esc(noCall ? 'Отменено до запуска' : statusNames[run.status] || run.status)}${run.error_code ? `<div class="row-meta">${esc(run.error_code)}</div>` : ''}</td><td>${esc(calls)}</td><td>${esc(tokens)}</td><td>${row.known_cost == null ? 'Неизвестно' : `${esc(decimal(row.known_cost))}${row.tariff ? ` ${esc(row.tariff.currency)}` : ''}`}<div class="row-meta">${row.complete ? 'Полный учёт' : 'Неполный учёт'}</div></td><td>${run.duration_ms == null ? 'Неизвестно' : `${esc(scoreText(run.duration_ms))} мс`}</td></tr>`;
  }).join('');
  const limitations = report.rows.filter(row => row.limitations.length).map(row => `<li><code>${esc(row.execution.id)}</code>: ${row.limitations.map(value => esc(limitationNames[value] || value)).join('; ')}</li>`).join('');
  const tariffs = [...new Map(report.rows.filter(row => row.tariff).map(row => [JSON.stringify(row.tariff), row.tariff])).values()];
  return `<div class="report-result-heading"><div><h3>Расход выбранных запусков</h3><p class="muted">${report.complete ? 'Учёт полный' : 'Учёт неполный — показана только известная часть'}</p></div><button type="button" class="button secondary" data-report-download="usage">${icon('download')}Скачать отчёт</button></div>
    ${!report.complete ? '<p class="notice amber">Неизвестный расход не равен нулю. Итог может быть выше известной части; полнота зависит от данных модели и тарифа.</p>' : ''}
    <dl class="report-metrics">${metric('Известная стоимость', cost, report.complete ? 'По применённым тарифам' : 'Неполный учёт')}${metric('Вызовы модели', known(report.known_calls, report.calls_complete), report.calls_complete ? 'Все вызовы учтены' : `Учтено вызовов: ${report.known_calls}; число может быть больше`)}${metric('Входные / выходные токены', `${known(report.known_input_tokens, report.tokens_complete)} / ${known(report.known_output_tokens, report.tokens_complete)}`, report.tokens_complete ? 'Все токены учтены' : 'Токены учтены не полностью')}${metric('Известная длительность', `${known(report.known_duration_ms, report.duration_complete)}${report.known_duration_ms === 0 && !report.duration_complete ? '' : ' мс'}`, report.duration_complete ? 'Длительность учтена полностью' : 'Длительность учтена не полностью')}</dl>
    ${report.pending_items ? `<p class="notice">Ещё не завершено элементов: ${esc(report.pending_items)}. После завершения обновите отчёт.</p>` : ''}
    ${report.missing_ids.length ? `<div class="notice amber"><p>Не найдены записи: ${report.missing_ids.length}</p><ul>${report.missing_ids.map(id => `<li><code>${esc(id)}</code></li>`).join('')}</ul></div>` : ''}
    ${rows ? reportTable(['Выполнение','Статус','Вызовы','Токены: вход / выход','Стоимость','Длительность'], rows) : '<p class="notice">Нет записей о расходе для выбранных ID</p>'}
    ${limitations ? `<details class="report-details" open><summary>Ограничения учёта</summary><ul>${limitations}</ul></details>` : ''}
    ${tariffs.length ? `<details class="report-details"><summary>Применённые тарифы</summary><p class="report-help">Это расчёт по выбранному тарифу, не исторический счёт провайдера. Разные валюты не складываются.</p>${reportTable(['Модель','Тариф','Вход','Выход','Единица'], tariffs.map(t => `<tr><td>${esc(t.model)}</td><td>${esc(t.version)}<div class="row-meta">С ${esc(t.effective_date)}</div></td><td>${esc(decimal(t.input_price))} ${esc(t.currency)}</td><td>${esc(decimal(t.output_price))} ${esc(t.currency)}</td><td>${esc(scoreText(t.token_unit))} токенов</td></tr>`).join(''))}</details>` : ''}`;
}

export async function mountReports(host, {datasets = [], datasetId = '', batchId = '', executionId = '', onDatasetChange = () => {}} = {}) {
  const server = getApiBase();
  let alive = true, evaluations = [], pairs = [], batches = [], batchTotal = 0, batchOffset = 0, batchBusy = false;
  let qualityReport = null, usageReport = null, qualityBusy = false, usageBusy = false, snapshotBusy = false;
  let criterionLabels = new Map();
  const active = () => alive && host.isConnected && getApiBase() === server;
  const q = selector => host.querySelector(selector);
  const cleanup = () => { alive = false; host.removeEventListener('click', click); };
  const evaluationLabel = e => `${e.externalId} · ${dateText(e.created_at)} · v${e.checklistVersion} · ${e.id.slice(0,8)}`;
  const errorAt = (selector, error) => { if (active()) { q(selector).textContent = error?.message || String(error); q(selector).focus(); } };
  host.innerHTML = `<div class="reports-page"><div class="page-heading"><div><h1>Отчёты</h1><p>Сравнение с ручной разметкой и учёт расходов модели</p></div><button type="button" class="button secondary" data-report-action="refresh">${icon('refresh')}Обновить оценки</button></div>
    ${datasets.length ? `<div class="dataset-toolbar"><div class="dataset-field"><label for="reports-dataset">Набор для выбора оценок</label><select id="reports-dataset">${datasets.map(d => `<option value="${esc(d.id)}" ${d.id === datasetId ? 'selected' : ''}>${esc(d.name)} · ${esc(d.count)}</option>`).join('')}</select></div></div>` : '<p class="notice">Загрузите диалоги для сравнения оценок. Расход можно запросить по пакету или ID выполнения.</p>'}
    <div class="report-tabs" role="group" aria-label="Тип отчёта"><button type="button" class="button" data-report-tab="quality" aria-pressed="true">Качество</button><button type="button" class="button" data-report-tab="usage" aria-pressed="false">Расход</button></div>
    <div id="report-load-error" class="form-error" role="alert" tabindex="-1"></div>
    <section data-report-panel="quality"><div class="panel report-config"><h2>Внутренние метрики команды</h2><p class="report-help">Сравните AI с ручной проверкой того же диалога. Для каждой пары выберите эталон, его ревизию и результат AI.</p>
      <h3 class="report-step">1. Выберите оценки для сравнения</h3><form id="quality-pair-form" class="stack"><div class="report-pair-fields"><div class="field"><label for="report-reference">Ручной эталон</label><select id="report-reference" required disabled><option value="">Загружаем оценки…</option></select><div id="reference-detail" class="report-selection-detail"></div></div><div class="field"><label for="report-revision">Ревизия эталона</label><select id="report-revision" required disabled><option value="">Сначала выберите эталон</option></select></div><div class="field"><label for="report-ai">AI-оценка того же диалога</label><select id="report-ai" required disabled><option value="">Сначала выберите эталон</option></select><div id="ai-detail" class="report-selection-detail"></div></div></div><div class="toolbar"><button type="submit" class="button secondary" id="report-add-pair" disabled>${icon('plus')}Добавить пару</button><span class="small-text muted">Без AI можно явно учесть пропуск</span></div></form>
      <div class="report-comparison"><h3 class="report-step">2. Проверьте пары и рассчитайте качество</h3><div id="quality-pairs" role="status"></div><div id="quality-error" class="form-error" role="alert" tabindex="-1"></div><button type="button" class="button primary" data-report-action="quality" disabled>Рассчитать качество</button></div>
    </div><div id="quality-result" class="report-result" aria-live="polite"></div></section>
    <section data-report-panel="usage" hidden><div class="panel report-config"><h2>Расход модели</h2><p class="report-help">Выбор пакета учитывает успешные и неудачные обращения. Выбор оценок покрывает только связанные с ними успешные запуски.</p>
      <form id="usage-form" class="stack"><div class="field"><label for="usage-mode">Что учитывать</label><select id="usage-mode"><option value="batch">Весь пакет</option><option value="evaluations">Выбранные AI-оценки</option><option value="executions">ID выполнений, включая ошибки</option></select></div>
      <div data-usage-fields="batch" class="stack"><div class="field"><label for="usage-batch">Пакет проверок на этом сервере</label><select id="usage-batch"><option value="">Загружаем пакеты…</option></select><small>Пакеты всего сервера, включая другие наборы</small><div id="usage-batch-detail" class="report-selection-detail"></div></div><button type="button" class="button secondary" data-report-action="more-batches" hidden>Загрузить ещё пакеты</button><div id="batch-load-error" class="form-error" role="alert" tabindex="-1"></div></div>
      <fieldset data-usage-fields="evaluations" class="report-evaluation-choices" hidden><legend>AI-оценки выбранного набора</legend><div id="usage-evaluations">Загружаем оценки…</div></fieldset>
      <div data-usage-fields="executions" class="field" hidden><label for="usage-executions">ID выполнений</label><textarea id="usage-executions" rows="4" spellcheck="false" placeholder="Один ID на строку"></textarea><small>Из X-Execution-ID ответа backend. Можно вставить несколько ID через пробел или запятую.</small></div>
      <div id="usage-error" class="form-error" role="alert" tabindex="-1"></div><div><button type="submit" class="button primary" id="usage-submit">Рассчитать расход</button></div></form>
    </div><div id="usage-result" class="report-result" aria-live="polite"></div></section></div>`;
  host.addEventListener('click', click);
  q('#reports-dataset')?.addEventListener('change', event => onDatasetChange(event.target.value));
  q('#report-reference').onchange = referenceChanged;
  q('#report-ai').onchange = () => showSelection('#ai-detail',q('#report-ai').value);
  q('#quality-pair-form').onsubmit = event => {
    event.preventDefault(); if (!active() || qualityBusy) return;
    const reference = evaluations.find(e => e.id === q('#report-reference').value);
    const revision = Number(q('#report-revision').value), ai = q('#report-ai').value;
    if (!reference || !revision || !ai) return errorAt('#quality-error', 'Выберите эталон, его ревизию и AI-оценку либо явный пропуск.');
    if (pairs.some(p => p.reference_id === reference.id)) return errorAt('#quality-error', 'Этот эталон уже добавлен. Для сравнения другой модели удалите прежнюю пару.');
    if (pairs.length >= 1000) return errorAt('#quality-error', 'В одном отчёте допустимо не более 1000 пар.');
    pairs.push({reference_id:reference.id, reference_revision:revision, ai_id:ai === '__missing__' ? null : ai});
    qualityReport = null; q('#quality-result').innerHTML = ''; q('#quality-error').textContent = ''; renderPairs();
  };
  q('#usage-batch').onchange = () => {const id=q('#usage-batch').value;q('#usage-batch-detail').innerHTML=id ? `<a href="#batches/${encode(id)}">Открыть состав и результаты пакета</a><details><summary>ID пакета</summary><code>${esc(id)}</code></details>` : '';};
  q('#usage-mode').onchange = () => { host.querySelectorAll('[data-usage-fields]').forEach(el => { el.hidden = el.dataset.usageFields !== q('#usage-mode').value; }); usageReport = null; q('#usage-result').innerHTML = ''; q('#usage-error').textContent = ''; };
  q('#usage-form').addEventListener('change', () => { if (!usageBusy) { usageReport = null; q('#usage-result').innerHTML = ''; } });
  if (batchId || executionId) {
    q('#usage-mode').value = executionId ? 'executions' : 'batch'; q('#usage-mode').onchange();
    if (executionId) q('#usage-executions').value = executionId;
    if (batchId) q('#usage-batch').innerHTML = `<option value="${esc(batchId)}" selected>Пакет ${esc(batchId.slice(0,8))}</option>`;
    host.querySelectorAll('[data-report-panel]').forEach(el => { el.hidden = el.dataset.reportPanel !== 'usage'; });
    host.querySelectorAll('[data-report-tab]').forEach(el => el.setAttribute('aria-pressed', String(el.dataset.reportTab === 'usage')));
  }
  q('#usage-form').onsubmit = async event => {
    event.preventDefault(); if (!active() || usageBusy) return;
    let body;
    try {
      const mode = q('#usage-mode').value;
      if (mode === 'batch') { if (!q('#usage-batch').value) throw new Error('Выберите пакет проверок.'); body = {batch_id:q('#usage-batch').value}; }
      else if (mode === 'evaluations') { const ids = [...host.querySelectorAll('[name="usage-evaluation"]:checked')].map(el => el.value); if (!ids.length) throw new Error('Выберите хотя бы одну AI-оценку.'); if (ids.length > 1000) throw new Error('Можно выбрать не более 1000 оценок.'); body = {evaluation_ids:ids}; }
      else body = {execution_ids:parseExecutionIds(q('#usage-executions').value)};
    } catch (error) { errorAt('#usage-error', error); return; }
    usageBusy = true; q('#usage-error').textContent = ''; q('#usage-result').innerHTML = '<p class="notice" role="status">Считаем расход…</p>'; toggleUsage(true); q('[data-report-action="refresh"]').disabled = true;
    try { const report = await api('/api/v1/reports/usage', {method:'POST', body, base:server}); if (!active()) return; usageReport = report; q('#usage-result').innerHTML = usageReportHTML(report); }
    catch (error) { if (active()) { usageReport = null; q('#usage-result').innerHTML = ''; errorAt('#usage-error', error); } }
    finally { usageBusy = false; if (active()) { toggleUsage(false); q('[data-report-action="refresh"]').disabled = snapshotBusy || qualityBusy; } }
  };
  await Promise.allSettled([loadEvaluations(), loadBatches()]);
  return cleanup;

  function toggleUsage(disabled) { q('#usage-form').querySelectorAll('input,select,textarea,button').forEach(el => { el.disabled = disabled; }); q('#usage-submit').textContent = disabled ? 'Считаем…' : 'Рассчитать расход'; }
  function showSelection(selector,id) {
    const value=evaluations.find(e=>e.id===id);
    q(selector).innerHTML=value ? `<a href="#review/${encode(value.conversation_id)}/${encode(value.id)}">Открыть оценку ${esc(value.externalId)}</a><span>${esc(value.checklistName)} · v${esc(value.checklistVersion)}${value.model ? ' · '+esc(value.model) : ''}</span><details><summary>ID оценки</summary><code>${esc(value.id)}</code></details>` : id==='__missing__' ? '<span>Пара попадёт в пропуски, а не в нулевую ошибку</span>' : '';
  }
  function referenceChanged() {
    const reference = evaluations.find(e => e.id === q('#report-reference').value);
    showSelection('#reference-detail',reference?.id); showSelection('#ai-detail',null);
    q('#report-revision').disabled = !reference;
    q('#report-revision').innerHTML = '<option value="">Выберите ревизию</option>' + (reference ? `<option value="${reference.revision}">${reference.revision} · текущая</option>` : '');
    q('#report-ai').disabled = !reference;
    q('#report-ai').innerHTML = '<option value="">Выберите AI-оценку или пропуск</option>' + (reference ? '<option value="__missing__">Нет AI-оценки — учесть пропуск</option>' + evaluations.filter(e => e.origin === 'ai' && e.conversation_id === reference.conversation_id).map(e => `<option value="${esc(e.id)}">${esc(evaluationLabel(e))}</option>`).join('') : '');
  }
  function renderPairs() {
    const labels = new Map(evaluations.map(e => [e.id, evaluationLabel(e)]));
    q('#quality-pairs').innerHTML = pairs.length ? `<h3>Выбрано пар: ${pairs.length}</h3><ol class="report-pairs">${pairs.map((p, index) => `<li><div><strong>${esc(labels.get(p.reference_id) || p.reference_id)}</strong><span>Ревизия ${esc(p.reference_revision)} → ${p.ai_id ? esc(labels.get(p.ai_id) || p.ai_id) : 'Пропуск AI'}</span></div><button type="button" class="button small secondary" data-remove-pair="${index}" ${qualityBusy ? 'disabled' : ''} aria-label="Удалить пару ${index + 1}">Удалить</button></li>`).join('')}</ol>` : '<p class="report-help">После выбора оценок нажмите «Добавить пару». Можно сравнить несколько диалогов за один расчёт.</p>';
    q('[data-report-action="quality"]').disabled = !pairs.length || qualityBusy;
  }
  async function loadEvaluations() {
    if (snapshotBusy || !active()) return;
    snapshotBusy = true; q('[data-report-action="refresh"]').disabled = true; q('#report-load-error').textContent = '';
    try {
      const snapshot = datasetId ? await api(`/api/v1/datasets/${encode(datasetId)}/export`, {base:server}) : {evaluations:[]};
      if (!active()) return;
      // Keep only selection metadata; report forms do not retain dialogue text or evidence.
      evaluations = snapshot.evaluations.map(e => ({id:e.id, origin:e.origin, revision:e.revision, model:e.metadata?.model, conversation_id:e.conversation_id, externalId:e.conversation.conversation_id, checklistName:e.checklist.name, checklistVersion:e.checklist.version, created_at:e.created_at}));
      criterionLabels = new Map(snapshot.evaluations.flatMap(e => (e.checklist.criteria || []).map(c => [JSON.stringify([e.checklist.checklist_id,e.checklist.version,c.id]),c.name])));
      const references = evaluations.filter(e => e.origin === 'manual');
      q('#report-reference').innerHTML = `<option value="">${references.length ? 'Выберите ручную оценку' : 'Ручных оценок пока нет'}</option>` + references.map(e => `<option value="${esc(e.id)}">${esc(evaluationLabel(e))}</option>`).join('');
      q('#report-reference').disabled = !references.length; q('#report-add-pair').disabled = !references.length || qualityBusy; referenceChanged();
      const ai = evaluations.filter(e => e.origin === 'ai');
      q('#usage-evaluations').innerHTML = ai.length ? ai.map(e => `<label class="report-choice"><input type="checkbox" name="usage-evaluation" value="${esc(e.id)}"><span>${esc(evaluationLabel(e))}</span></label>`).join('') : '<p class="report-help">AI-оценок в этом наборе пока нет. Для неудачного запуска используйте его ID выполнения.</p>';
      if (usageBusy) toggleUsage(true);
      renderPairs();
    } catch (error) { if (active()) { errorAt('#report-load-error', error); q('#report-reference').innerHTML = '<option value="">Оценки не загружены</option>'; } }
    finally { snapshotBusy = false; if (active()) q('[data-report-action="refresh"]').disabled = qualityBusy || usageBusy; }
  }
  async function loadBatches() {
    if (batchBusy || !active()) return; batchBusy = true;
    q('[data-report-action="more-batches"]').disabled = true; q('#batch-load-error').textContent = '';
    try {
      const result = await api(`/api/v1/evaluation-batches?limit=100&offset=${batchOffset}`, {base:server});
      if (!active()) return;
      const selected = q('#usage-batch').value;
      batches = [...new Map([...batches, ...result.items].map(b => [b.id, b])).values()]; batchOffset += result.items.length; batchTotal = result.total;
      q('#usage-batch').innerHTML = `<option value="">${batches.length ? 'Выберите пакет' : 'Пакетов пока нет'}</option>` + batches.map(b => `<option value="${esc(b.id)}">${esc(dateText(b.created_at))} · ${esc(b.checklist_id)} v${esc(b.checklist_version)} · ${esc(b.total)} диалогов · ${b.completed ? 'Завершён' : 'В работе'} · ${esc(b.id.slice(0,8))}</option>`).join('');
      if (batchId && !batches.some(b => b.id === batchId)) q('#usage-batch').insertAdjacentHTML('beforeend', `<option value="${esc(batchId)}">Пакет ${esc(batchId.slice(0,8))}</option>`);
      if (batches.some(b => b.id === selected) || selected === batchId) q('#usage-batch').value = selected;
      q('#usage-batch').onchange();
      q('[data-report-action="more-batches"]').hidden = batchOffset >= batchTotal || !result.items.length;
    } catch (error) { if (active()) { errorAt('#batch-load-error', error); q('[data-report-action="more-batches"]').hidden = false; q('[data-report-action="more-batches"]').textContent = 'Повторить загрузку пакетов'; } }
    finally { batchBusy = false; if (active()) q('[data-report-action="more-batches"]').disabled = usageBusy; }
  }
  async function click(event) {
    const button = event.target.closest('button'); if (!button || button.disabled || !active()) return;
    if (button.dataset.reportTab) { host.querySelectorAll('[data-report-panel]').forEach(el => { el.hidden = el.dataset.reportPanel !== button.dataset.reportTab; }); host.querySelectorAll('[data-report-tab]').forEach(el => el.setAttribute('aria-pressed', String(el === button))); return; }
    if (button.dataset.reportDownload === 'quality' && qualityReport) { downloadJson(qualityReport, 'quality-report.json'); return; }
    if (button.dataset.reportDownload === 'usage' && usageReport) { downloadJson(usageReport, 'usage-report.json'); return; }
    if (button.hasAttribute('data-remove-pair') && !qualityBusy) { pairs.splice(Number(button.dataset.removePair), 1); qualityReport = null; q('#quality-result').innerHTML = ''; renderPairs(); return; }
    if (button.dataset.reportAction === 'refresh' && !qualityBusy && !usageBusy) { usageReport = null; q('#usage-result').innerHTML = ''; await loadEvaluations(); return; }
    if (button.dataset.reportAction === 'more-batches') { await loadBatches(); return; }
    if (button.dataset.reportAction !== 'quality' || qualityBusy || !pairs.length) return;
    qualityBusy = true; q('#quality-error').textContent = ''; q('#quality-result').innerHTML = '<p class="notice" role="status">Сравниваем оценки…</p>'; q('#quality-pair-form').querySelectorAll('select,button').forEach(el => { el.disabled = true; }); q('[data-report-action="refresh"]').disabled = true; renderPairs();
    try {
      const report = await api('/api/v1/reports/quality', {method:'POST', body:{pairs:pairs.map(p => ({...p}))}, base:server});
      if (!active()) return; qualityReport = report; q('#quality-result').innerHTML = qualityReportHTML(report, new Map(evaluations.map(e => [e.id, evaluationLabel(e)])), criterionLabels);
    } catch (error) { if (active()) { qualityReport = null; q('#quality-result').innerHTML = ''; errorAt('#quality-error', error); } }
    finally { qualityBusy = false; if (active()) { q('#report-reference').disabled = !evaluations.some(e => e.origin === 'manual'); q('#report-add-pair').disabled = q('#report-reference').disabled; q('#report-revision').disabled = !q('#report-reference').value; q('#report-ai').disabled = !q('#report-reference').value; q('[data-report-action="refresh"]').disabled = snapshotBusy || usageBusy; renderPairs(); } }
  }
}
