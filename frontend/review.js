import { api as requestApi, getApiBase } from './api.js';
import {findOperation, prepareOperation, sendOperation, clearOperation} from './operations.js';
import { draftFingerprint, readDraft, writeDraft, removeDraft } from './drafts.js';
import { esc, toast, icon, dateText, scoreText, openModal } from './ui.js';

const roles = { client: 'Клиент', operator: 'Оператор', bot: 'Бот', system: 'Система' };
const evidenceNames = { present: 'Подтверждение', missing: 'Контекст отсутствующего действия', not_applicable: 'Не применимо' };
const encode = encodeURIComponent;
const E = (id) => `/api/v1/evaluations/${encode(id)}`;
const errorText = (error) => ({
  AI_NOT_CONFIGURED: 'Модель ещё не подключена. Вы можете провести ручную оценку — она сохранится с отдельной отметкой.',
  AI_UNAVAILABLE: 'Сервис модели сейчас недоступен. Проверьте историю перед повторным запуском.',
  AI_TIMEOUT: 'Модель не ответила вовремя. Проверьте историю перед повторным запуском.',
  AI_INVALID_RESULT: 'Ответ модели не прошёл проверку. Оценка не сохранена.',
  REVISION_CONFLICT: 'Оценку уже изменил другой участник. Обновите её, проверьте изменения и снова отправьте решение. Ваш черновик сохранён.',
  APPEAL_EXISTS: 'По этому критерию уже есть открытая апелляция. Обновите оценку, чтобы увидеть её.',
  APPEAL_CLOSED: 'Эту апелляцию уже рассмотрели. Обновите оценку, чтобы увидеть решение.',
  INVALID_EVIDENCE: 'Цитата должна точно совпадать с фрагментом выбранного сообщения.',
})[error.code] || error.message || 'Не удалось выполнить запрос. Попробуйте ещё раз.';
const valueLabel = (criterion, value) => value === 'na' ? 'N/A' : criterion.type === 'scale' ? `${value} / ${Math.max(...criterion.scale)}` : criterion.type === 'flag' ? (value === 'yes' ? 'Нарушение' : 'Не выявлено') : value === 'yes' ? 'Да' : 'Нет';
const valueColor = (criterion, value) => value === 'na' ? '' : criterion.type === 'flag' ? (value === 'yes' ? 'red' : 'green') : criterion.type === 'scale' ? (value === 0 ? 'red' : value === Math.max(...criterion.scale) ? 'green' : 'amber') : value === 'yes' ? 'green' : 'red';
const safeAudio = (value) => { try { const u = new URL(value); return ['http:', 'https:'].includes(u.protocol) && !u.username && !u.password ? u.href : ''; } catch { return ''; } };
const options = (criterion, selected = '') => `<option value="">Выберите значение</option>${(criterion.type === 'scale' ? criterion.scale : ['yes', 'no']).map(v => `<option value="${esc(v)}" ${String(v) === String(selected) ? 'selected' : ''}>${esc(valueLabel(criterion, v))}</option>`).join('')}${criterion.allow_na ? `<option value="na" ${selected === 'na' ? 'selected' : ''}>N/A — не применимо</option>` : ''}`;
const readValue = (criterion, raw) => criterion.type === 'scale' && raw !== 'na' ? Number(raw) : raw;
const isProblem = (criterion, answer) => {
  if (!answer || answer.value === 'na' || answer.needs_review) return false;
  if (criterion.type === 'flag') return answer.value === 'yes';
  if (criterion.type === 'binary') return answer.value === 'no';
  return typeof answer.value === 'number' && answer.value < Math.max(...criterion.scale);
};

export async function mountReview(container, { conversationId, evaluationId, checklistId, checklistVersion, newEvaluation = false, onSelectionChanged = () => {}, onEditChecklist = () => {}, onBack = () => {}, onChanged = () => {} } = {}) {
  let alive = true, conversation, checklists = [], checklist, evaluation = null, history = [], historyTotal = 0;
  let busy = false, pageError = '', loadingChecklist = false, loadingHistory = false, modalState = null, ticker = null;
  let aiConfigured = false, criterionFilter = 'all', evidenceReturn = null;
  const server = getApiBase();
  function ensureServer() {
    if (!alive || getApiBase() !== server) throw new Error('Адрес сервера изменился. Откройте диалог заново перед сохранением.');
  }
  const api = (path, options = {}) => { ensureServer(); return requestApi(path, {...options, base:server}); };
  const cleanup = () => { modalState?.flushDraft?.(); alive = false; clearInterval(ticker); if (modalState) modalState.modal.close(); container.removeEventListener('click', click); container.removeEventListener('change', change); window.removeEventListener('beforeunload', unload); };
  cleanup.canLeave = () => {
    if (busy || modalState?.saving) { toast('Дождитесь завершения текущего запроса.', 'error'); return false; }
    if (modalState?.flushDraft && modalState.flushDraft()) return true;
    return !modalState?.dirty || confirm('Уйти со страницы и потерять несохранённые изменения?');
  };
  const unload = (event) => { modalState?.flushDraft?.(); if (busy || modalState?.dirty || modalState?.saving) { event.preventDefault(); event.returnValue = ''; } };
  window.addEventListener('beforeunload', unload);
  container.innerHTML = '<div class="empty" role="status">Загружаем диалог и правила оценки…</div>';
  const initialPlaceholder = container.firstElementChild;
  container.addEventListener('click', click);
  container.addEventListener('change', change);
  try {
    const [conv, list, old, health] = await Promise.all([
      api(`/api/v1/conversations/${encode(conversationId)}`),
      api('/api/v1/checklists'),
      api(`/api/v1/conversations/${encode(conversationId)}/evaluations?limit=100`),
      api('/health').catch(() => ({ ai_configured: false })),
    ]);
    if (!alive || !container.contains(initialPlaceholder)) return cleanup;
    conversation = conv; checklists = list.items; history = old.items; historyTotal = old.total; aiConfigured = health.ai_configured === true;
    const initialId = newEvaluation ? null : evaluationId || history[0]?.id;
    const selectedChecklist = checklistId || checklists[0]?.checklist_id;
    const rulePath = selectedChecklist ? `/api/v1/checklists/${encode(selectedChecklist)}${checklistVersion ? `/versions/${Number(checklistVersion)}` : ''}` : null;
    const [rules, result] = await Promise.all([
      rulePath ? api(rulePath) : null,
      initialId ? api(E(initialId)) : null,
    ]);
    if (!alive || !container.contains(initialPlaceholder)) return cleanup;
    if (result && result.conversation_id !== conversationId) throw new Error('Оценка относится к другому диалогу.');
    checklist = rules; evaluation = result;
    if (checklist) onSelectionChanged({ checklistId: checklist.checklist_id, version: checklist.version });
    render();
  } catch (error) {
    if (alive && container.contains(initialPlaceholder)) container.innerHTML = `<div class="notice error" role="alert">${esc(errorText(error))}</div><button class="button secondary" data-action="back">${icon('arrow')} К диалогам</button>`;
  }
  return cleanup;

  function render() {
    if (!alive || !conversation || getApiBase() !== server) return;
    evidenceReturn = null;
    const source = evaluation?.conversation || conversation;
    const audio = safeAudio(source.audio_url);
    container.innerHTML = `<div class="review-page">
      <div class="review-breadcrumb"><button class="button ghost" data-action="back">${icon('arrow')} Диалоги</button><span>/</span><span>${esc(conversation.conversation_id)}</span></div>
      <div class="page-heading review-heading"><div><h1>${esc(conversation.conversation_id)}</h1><div class="review-subtitle"><span class="badge">${esc(conversation.language)}</span><span>${icon(conversation.channel === 'voice call' ? 'phone' : 'chat')} ${conversation.channel === 'voice call' ? 'Звонок' : 'Чат'}</span><span>${source.transcript.length} сообщений</span></div></div><button class="button secondary" data-action="refresh" ${busy || loadingHistory || loadingChecklist ? 'disabled' : ''}>${icon('refresh')} Обновить</button></div>
      ${pageError ? `<div class="notice ${pageError.includes('Модель ещё') ? '' : 'error'}" role="alert">${esc(pageError)}</div>` : ''}
      <section class="panel review-controls" aria-label="Новая оценка"><div class="review-control-title">Новая оценка<span class="muted">Выберите правила проверки</span></div><label class="field">Чек-лист<select data-select="checklist" ${busy || loadingHistory || loadingChecklist || !checklists.length ? 'disabled' : ''}>${checklists.map(c => `<option value="${esc(c.checklist_id)}" ${c.checklist_id === checklist?.checklist_id ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</select></label><label class="field review-version">Версия<select data-select="version" ${busy || loadingHistory || loadingChecklist || !checklist ? 'disabled' : ''}>${checklist ? Array.from({ length: checklists.find(c => c.checklist_id === checklist.checklist_id)?.version || checklist.version }, (_, i) => i + 1).reverse().map(v => `<option value="${v}" ${v === checklist.version ? 'selected' : ''}>v${v}</option>`).join('') : ''}</select></label><div class="review-control-buttons"><button class="button ghost" data-action="edit-checklist" ${busy || loadingHistory || loadingChecklist || !checklist ? 'disabled' : ''}>${icon('edit')} Изменить чек-лист</button><button class="button secondary" data-action="manual" ${busy || loadingHistory || loadingChecklist || !checklist ? 'disabled' : ''}>${icon('edit')} Оценить вручную</button><button class="button primary" data-action="ai" ${busy || loadingHistory || loadingChecklist || !checklist || !aiConfigured ? 'disabled' : ''}>${icon('shield')} ${busy ? 'Оцениваем…' : 'Оценить с AI'}</button></div>${!aiConfigured ? '<p class="review-ai-hint">AI не подключён. Доступна ручная оценка с цитатами.</p>' : ''}${busy ? '<div class="review-progress" role="status"><span class="review-spinner"></span><span data-progress>Модель анализирует диалог…</span><span class="muted">Лимит ожидания — 3 минуты</span></div>' : ''}</section>
      <div data-operation-banner>${operationBanner()}</div>
      <div data-manual-draft-banner>${manualDraftBanner()}</div>
      <div class="review-workspace"><section class="panel transcript-panel" aria-label="Транскрипт диалога"><div class="review-panel-heading"><h2>Диалог</h2><span class="muted">${evaluation ? 'Снимок на момент оценки' : 'Исходный текст'}</span></div>${audio ? `<div class="review-audio"><audio controls preload="none" src="${esc(audio)}" aria-label="Запись звонка"></audio><a href="${esc(audio)}" target="_blank" rel="noopener noreferrer">Открыть запись</a><p class="muted">Цитаты ведут к тексту. Позиция в аудио не синхронизирована.</p></div>` : source.channel === 'voice call' ? '<div class="notice">Аудиозапись не приложена. Доступен транскрипт звонка.</div>' : ''}<div class="transcript-scroll" tabindex="0" role="region" aria-label="Сообщения диалога"><div class="evidence-return"><button class="button secondary small" type="button" data-action="return-evidence" hidden>Вернуться к критерию ${icon('arrow')}</button></div>${source.transcript.map(m => `<article class="message message-${esc(m.speaker)}" id="message-${esc(m.id)}" data-message-id="${esc(m.id)}" tabindex="-1"><div class="message-meta"><strong>${esc(roles[m.speaker] || m.speaker)}</strong><span>${esc(m.timestamp)}</span><span class="message-id">${esc(m.id)}</span></div><p class="message-text">${esc(m.text)}</p></article>`).join('')}</div></section>
      <section class="panel evaluation-panel" aria-label="Оценка диалога"><div class="review-panel-heading"><h2>Результат проверки</h2><span class="badge">${historyTotal} в истории</span></div><div class="review-history"><label class="field">История оценок<select data-select="history" ${busy || loadingHistory || loadingChecklist ? 'disabled' : ''}><option value="" ${!evaluation ? 'selected' : ''}>Новая проверка</option>${evaluation && !history.some(h => h.id === evaluation.id) ? `<option value="${esc(evaluation.id)}" selected>${esc(dateText(evaluation.created_at))} · ${evaluation.origin === 'manual' ? 'Вручную' : 'AI'} · ${esc(scoreText(evaluation.score.total))}</option>` : ''}${history.map(h => `<option value="${esc(h.id)}" ${evaluation?.id === h.id ? 'selected' : ''}>${esc(dateText(h.created_at))} · ${h.origin === 'manual' ? 'Вручную' : 'AI'} · ${esc(scoreText(h.score))}</option>`).join('')}</select></label>${history.length < historyTotal ? '<button class="button ghost" data-action="more-history">Ранние оценки</button>' : ''}</div><div data-review-result>${evaluation ? resultHTML(evaluation) : `<div class="review-empty"><h3>${historyTotal ? 'Новая оценка диалога' : 'Диалог ещё не оценён'}</h3><p>${historyTotal ? 'Предыдущие результаты доступны в истории. Выберите правила и явно запустите новую оценку.' : 'Выберите чек-лист и запустите оценку. Здесь появятся результаты по критериям и цитаты из диалога.'}</p><div class="notice">Для самостоятельной проверки нажмите «Оценить вручную»</div></div>`}</div></section></div></div>`;
  }

  function resultHTML(result) {
    const allFlags = result.score.flags || [];
    const flags = allFlags.filter(id => !result.final.answers.find(a => a.criterion_id === id)?.needs_review);
    const uncertainFlags = allFlags.length - flags.length;
    const entries = result.checklist.criteria.map((criterion, index) => ({ criterion, index, answer: result.final.answers.find(a => a.criterion_id === criterion.id) }));
    const matches = (item, filter) => filter === 'all' || (filter === 'problems' && isProblem(item.criterion, item.answer)) || (filter === 'review' && item.answer?.needs_review) || (filter === 'na' && item.answer?.value === 'na');
    const visible = entries.filter(item => matches(item, criterionFilter));
    const expandedId = (visible.find(item => isProblem(item.criterion, item.answer)) || visible[0])?.criterion.id;
    const filters = [['all', 'Все'], ['problems', 'Проблемы'], ['review', 'Нужна проверка'], ['na', 'N/A']];
    const filterHTML = `<div class="criterion-filters" role="group" aria-label="Фильтр критериев">${filters.map(([key, label]) => `<button type="button" class="criterion-filter" data-action="filter-criteria" data-filter="${key}" aria-pressed="${criterionFilter === key}">${label}<span>${entries.filter(item => matches(item, key)).length}</span></button>`).join('')}</div><p class="criterion-filter-hint">Проблемы — замечания по оценке. N/A и ответы, требующие проверки, показаны отдельно.</p>`;
    return `<div class="review-result-meta"><span class="badge ${result.origin === 'manual' ? 'amber' : 'green'}">${result.origin === 'manual' ? 'Ручная оценка' : 'AI-оценка'}</span><span>${esc(result.checklist.name)} · v${result.checklist.version}</span><span class="muted">Редакция ${result.revision}</span></div><div class="review-score"><div><span class="muted">Итоговая оценка</span><strong>${esc(scoreText(result.score.total))}<small>${result.score.total === null ? '' : '/ 100'}</small></strong>${result.score.total === null ? '<span class="muted">Нет применимых весов</span>' : ''}</div><div class="review-original"><span class="muted">Первоначальная</span><strong>${esc(scoreText(result.original_score.total))}</strong><span class="muted">${result.reviews.length ? `Решений по апелляциям: ${result.reviews.length}` : 'Без изменений'}</span></div></div>${flags.length ? `<div class="notice error">${icon('alert')} Грубых нарушений по оценке: ${flags.length}. Флаги учитываются отдельно от балла.</div>` : ''}${uncertainFlags ? `<div class="notice">Требуют проверки флагов: ${uncertainFlags}. Эти ответы пока не подтверждают нарушение.</div>` : ''}<div class="review-sections">${result.score.sections.map(s => `<div><span>${esc(s.name)}</span><strong>${esc(scoreText(s.total))}</strong></div>`).join('')}</div>${filterHTML}<div class="criterion-results">${visible.map(({ criterion: c, index }) => {
      const answer = result.final.answers.find(a => a.criterion_id === c.id);
      const original = result.original.answers.find(a => a.criterion_id === c.id);
      const points = result.score.criteria.find(a => a.criterion_id === c.id);
      const appeals = (result.appeals || []).filter(a => a.criterion_id === c.id);
      const open = appeals.find(a => a.status === 'open');
      const displayValue = c.type === 'flag' && answer.needs_review && answer.value === 'yes' ? 'Возможное нарушение' : valueLabel(c, answer.value);
      return `<details class="criterion-result" ${c.id === expandedId ? 'open' : ''}><summary><span class="criterion-number">${String(index + 1).padStart(2, '0')}</span><span class="criterion-name">${esc(c.name)}${answer.needs_review ? '<small>Нужна ручная проверка</small>' : ''}</span><span class="badge ${answer.needs_review ? '' : valueColor(c, answer.value)}">${esc(displayValue)}</span><span class="criterion-chevron">${icon('chevron')}</span></summary><div class="criterion-result-body"><div class="criterion-weight"><span>${esc(c.section || 'Без раздела')}</span><span>${c.type === 'flag' ? 'Отдельный флаг' : `Вклад: ${scoreText(points.points)} из ${scoreText(points.effective_weight)} баллов`}</span></div>${original.value !== answer.value ? `<p class="review-change">После апелляции: ${esc(valueLabel(c, original.value))} → ${esc(valueLabel(c, answer.value))}</p>` : ''}<p class="review-explanation">${esc(answer.explanation)}</p>${answer.value === 'na' ? `<div class="notice">Причина N/A: ${esc(answer.na_reason)}</div>` : ''}${answer.needs_review ? `<div class="notice">${icon('alert')} ${esc(answer.limitation)}</div>` : ''}<div class="evidence-heading">${esc(evidenceNames[answer.evidence_kind] || answer.evidence_kind)}</div>${answer.evidence.map((proof, n) => `<button class="evidence-link" data-action="quote" data-criterion="${esc(c.id)}" data-proof="${n}"><span>«${esc(proof.quote)}»</span><small>${esc(proof.message_id)} · К сообщению ${icon('arrow')}</small></button>`).join('')}<details class="criterion-rule"><summary>Правило проверки</summary><p>${esc(c.rule)}</p>${conversation.channel === 'voice call' && c.call_rule ? `<p><strong>Для звонка:</strong> ${esc(c.call_rule)}</p>` : ''}${c.allow_na ? `<p><strong>N/A:</strong> ${esc(c.na_rule)}</p>` : ''}</details>${appeals.map(a => `<div class="appeal-entry"><div><span class="badge ${a.status === 'open' ? 'amber' : 'green'}">${a.status === 'open' ? 'Открытая апелляция' : 'Рассмотрена'}</span><span class="muted">${esc(a.author)} · ${esc(dateText(a.created_at))}</span></div><p>${esc(a.comment)}</p>${a.status === 'open' ? `<button class="button secondary" data-action="decide" data-appeal="${esc(a.id)}" ${busy || loadingHistory || loadingChecklist ? 'disabled' : ''}>Рассмотреть</button>` : `<p class="appeal-decision"><strong>${a.decision.previous_value === a.decision.value ? 'Оценка подтверждена' : 'Оценка изменена'}:</strong> ${esc(valueLabel(c, a.decision.previous_value))} → ${esc(valueLabel(c, a.decision.value))}<br>${esc(a.decision.reason)}<br><span class="muted">${esc(a.decision.reviewer)} · ${esc(dateText(a.decision.created_at))}</span></p>`}</div>`).join('')}<button class="button ghost appeal-create" data-action="appeal" data-criterion="${esc(c.id)}" ${open || busy || loadingHistory || loadingChecklist ? 'disabled' : ''}>${icon('edit')} ${open ? 'Апелляция на рассмотрении' : 'Оспорить оценку'}</button></div></details>`;
    }).join('')}${visible.length ? '' : '<div class="review-filter-empty" role="status">В этой группе критериев нет</div>'}</div><details class="review-audit"><summary>Сведения об оценке</summary><dl><dt>Создана</dt><dd>${esc(dateText(result.created_at))}</dd><dt>Источник</dt><dd>${result.origin === 'manual' ? `Ручная оценка${result.metadata?.author ? ` · ${esc(result.metadata.author)}` : ''}` : 'Модель AI'}</dd><dt>ID</dt><dd>${esc(result.id)}</dd><dt>SHA-256 входных данных</dt><dd>${esc(result.input_hash)}</dd></dl></details>`;
  }

  async function loadHistory() {
    const response = await api(`/api/v1/conversations/${encode(conversationId)}/evaluations?limit=100`);
    history = response.items; historyTotal = response.total;
  }
  async function refresh() {
    if (busy || loadingHistory || loadingChecklist) return;
    pageError = ''; loadingHistory = true; render();
    try {
      await loadHistory();
      const status = await api('/health'); aiConfigured = status.ai_configured === true;
      if (evaluation) evaluation = await api(E(evaluation.id));
    } catch (error) { pageError = errorText(error); }
    finally { loadingHistory = false; render(); }
  }
  async function click(event) {
    const button = event.target.closest('[data-action]');
    if (!button || button.disabled || !container.contains(button)) return;
    const action = button.dataset.action;
    if (action === 'back') return onBack();
    if (action === 'return-evidence') {
      if (evidenceReturn?.isConnected) { evidenceReturn.scrollIntoView({block:'center', behavior:'instant'}); evidenceReturn.focus({preventScroll:true}); }
      button.hidden = true;
      return;
    }
    if (action !== 'quote' && (busy || loadingHistory || loadingChecklist)) return;
    if (action === 'refresh') return refresh();
    if (action === 'recover-operation') return recoverOperation(button.dataset.kind);
    if (action === 'discard-operation') {
      const op = findOperation(operationScope(button.dataset.kind));
      if (op && confirm('Сначала проверьте историю и расход. Начатый запрос может ещё выполняться. Разрешить отдельную новую оценку?')) { clearOperation(op); render(); }
      return;
    }
    if (action === 'manual' || action === 'open-manual-draft') return manualForm();
    if (action === 'delete-manual-draft') { try { removeDraft(manualScope(checklist, conversation)); render(); toast('Черновик удалён'); } catch (error) { toast(errorText(error), 'error'); } return; }
    if (action === 'edit-checklist') return onEditChecklist({ conversationId, checklistId: checklist.checklist_id, checklistVersion: checklist.version });
    if (action === 'filter-criteria') { criterionFilter = button.dataset.filter; container.querySelector('[data-review-result]').innerHTML = resultHTML(evaluation); container.querySelector(`[data-filter="${criterionFilter}"]`)?.focus(); return; }
    if (action === 'ai') return evaluateAI();
    if (action === 'appeal') return appealForm(button.dataset.criterion);
    if (action === 'decide') return decisionForm(button.dataset.appeal);
    if (action === 'quote') {
      const answer = evaluation.final.answers.find(a => a.criterion_id === button.dataset.criterion);
      const proof = answer?.evidence[Number(button.dataset.proof)];
      if (!proof) return;
      const message = evaluation.conversation.transcript.find(m => m.id === proof.message_id);
      const target = [...container.querySelectorAll('[data-message-id]')].find(m => m.dataset.messageId === proof.message_id);
      if (!message || !target) return;
      evidenceReturn = button;
      container.querySelector('[data-action="return-evidence"]').hidden = false;
      container.querySelectorAll('.message').forEach(el => el.classList.remove('message-highlight'));
      const at = message.text.indexOf(proof.quote);
      target.querySelector('.message-text').innerHTML = at < 0 ? esc(message.text) : `${esc(message.text.slice(0, at))}<mark>${esc(proof.quote)}</mark>${esc(message.text.slice(at + proof.quote.length))}`;
      target.classList.add('message-highlight'); target.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' }); target.focus({ preventScroll: true });
    }
    if (action === 'more-history') {
      loadingHistory = true; render();
      try { const data = await api(`/api/v1/conversations/${encode(conversationId)}/evaluations?limit=100&offset=${history.length}`); history.push(...data.items); historyTotal = data.total; } catch (error) { toast(errorText(error), 'error'); }
      finally { loadingHistory = false; render(); }
    }
  }
  async function change(event) {
    const select = event.target.closest('[data-select]');
    if (!select || busy || loadingHistory || loadingChecklist) return;
    if (select.dataset.select === 'history') {
      const chosenId = select.value;
      loadingHistory = true; render();
      try { evaluation = chosenId ? await api(E(chosenId)) : null; criterionFilter = 'all'; pageError = ''; } catch (error) { pageError = errorText(error); }
      loadingHistory = false; render(); return;
    }
    const chosen = select.value, kind = select.dataset.select;
    loadingChecklist = true; render();
    try {
      checklist = await api(kind === 'checklist' ? `/api/v1/checklists/${encode(chosen)}` : `/api/v1/checklists/${encode(checklist.checklist_id)}/versions/${Number(chosen)}`);
      onSelectionChanged({ checklistId: checklist.checklist_id, version: checklist.version });
      pageError = '';
    } catch (error) { pageError = errorText(error); }
    loadingChecklist = false; render();
  }
  async function evaluateAI() {
    if (busy || loadingHistory || loadingChecklist || !checklist || !aiConfigured) return;
    busy = true; pageError = ''; render(); const started = Date.now();
    ticker = setInterval(() => { const el = container.querySelector('[data-progress]'); if (el) el.textContent = `Модель анализирует диалог · ${Math.floor((Date.now() - started) / 1000)} с`; }, 1000);
    try {
      ensureServer();
      const op = prepareOperation(operationScope('ai'), '/api/v1/evaluations', {conversation_id: conversationId, checklist_id: checklist.checklist_id, checklist_version: checklist.version});
      const receipt = await sendOperation(op);
      const result = await api(E(receipt.id), {base:server});
      clearOperation(op);
      recordResult(result); if (alive) { toast('Оценка сохранена'); onChanged(result); }
    } catch (error) { pageError = errorText(error); }
    finally { clearInterval(ticker); busy = false; render(); }
  }

  function operationScope(kind) { return `evaluation:${conversationId}:${kind}`; }
  function operationBanner() {
    try {
      return ['ai','manual'].map(kind => {
        const op = findOperation(operationScope(kind));
        if (!op) return '';
        return `<div class="notice operation-notice"><div><strong>${kind === 'manual' ? 'Ручная оценка' : 'AI-оценка'}: сохранён запрос</strong><p>${esc(op.error?.message || 'Результат ещё не подтверждён в интерфейсе. Можно восстановить его с прежним ключом, даже после перезагрузки.')} Правила: ${esc(op.body.checklist_id)} · v${esc(op.body.checklist_version)}</p>${op.error?.executionId ? `<p>Запуск для отчёта расходов: <code>${esc(op.error.executionId)}</code> · <a href="#reports?execution=${encode(op.error.executionId)}">Проверить расход</a></p>` : ''}</div><div class="toolbar"><button class="button secondary" data-action="recover-operation" data-kind="${kind}" ${busy ? 'disabled' : ''}>Восстановить результат</button><button class="button ghost" data-action="discard-operation" data-kind="${kind}" ${busy ? 'disabled' : ''}>Разрешить новую оценку</button></div></div>`;
      }).join('');
    } catch (error) { return `<div class="notice error" role="alert">${esc(error.message)}</div>`; }
  }
  async function recoverOperation(kind) {
    let op;
    try { op = findOperation(operationScope(kind)); } catch (error) { pageError = errorText(error); render(); return; }
    if (!op || busy) return;
    busy = true; pageError = ''; render();
    try {
      const receipt = op.resultId ? {id:op.resultId} : await sendOperation(op);
      const result = await api(E(receipt.id), {base:server});
      if (getApiBase() !== server) return;
      clearOperation(op); recordResult(result);
      if (kind === 'manual') removeDraft(manualScope(result.checklist, result.conversation));
      toast('Сохранённый результат восстановлен'); onChanged(result);
    } catch (error) { pageError = errorText(error); }
    finally { busy = false; render(); }
  }

  function recordResult(result) {
    evaluation = result; criterionFilter = 'all';
    if (!history.some(item => item.id === result.id)) historyTotal++;
    history = [{ id: result.id, origin: result.origin, revision: result.revision, created_at: result.created_at, score: result.score.total }, ...history.filter(item => item.id !== result.id)];
  }

  function createModal(title, html, wide = false) {
    const modal = openModal(title, html, { wide });
    modal.el.classList.add('review-modal');
    const state = { modal, dirty: false, saving: false }; modalState = state;
    modal.body.addEventListener('input', () => { state.dirty = true; });
    modal.body.addEventListener('change', () => { state.dirty = true; });
    const allowClose = () => !state.saving && (state.flushDraft?.() || !state.dirty || confirm('Закрыть форму и потерять несохранённые изменения?'));
    modal.el.addEventListener('cancel', event => { if (!allowClose()) event.preventDefault(); });
    modal.el.addEventListener('close', () => { state.flushDraft?.(); state.disposeDraft?.(); if (modalState === state) modalState = null; if (alive && state.flushDraft) { const banner = container.querySelector('[data-manual-draft-banner]'); if (banner) banner.innerHTML = manualDraftBanner(); } });
    const close = () => { state.dirty = false; modalState = null; modal.close(); };
    const fail = (message) => { const el = modal.body.querySelector('[data-form-error]'); el.textContent = message; el.hidden = false; el.focus(); };
    return { modal, state, close, fail };
  }
  function messageOptions(source, selected = '') {
    return `<option value="">Выберите сообщение</option>${source.transcript.map(m => `<option value="${esc(m.id)}" ${m.id === selected ? 'selected' : ''}>${esc(m.id)} · ${esc(roles[m.speaker] || m.speaker)} · ${esc(m.text.length > 95 ? m.text.slice(0, 95) + '…' : m.text)}</option>`).join('')}`;
  }
  function evidenceFields(source, initial = {}) {
    return `<div class="form-grid"><label class="field">Характер доказательства<select name="evidence_kind"><option value="present" ${initial.evidence_kind === 'present' ? 'selected' : ''}>Подтверждение</option><option value="missing" ${initial.evidence_kind === 'missing' ? 'selected' : ''}>Контекст отсутствующего действия</option><option value="not_applicable" ${initial.evidence_kind === 'not_applicable' ? 'selected' : ''}>Не применимо (N/A)</option></select></label><label class="field">Сообщение с доказательством<select name="message_id" required>${messageOptions(source, initial.evidence?.[0]?.message_id)}</select></label></div><label class="field">Точная цитата<textarea name="quote" rows="3" required maxlength="100000" placeholder="Выберите сообщение выше, затем оставьте нужный фрагмент">${esc(initial.evidence?.[0]?.quote || '')}</textarea><small class="muted">Цитата должна быть точным фрагментом исходного сообщения. Для отсутствующего действия укажите контекст, где оно ожидалось.</small></label>`;
  }
  function bindAnswerFields(root, source) {
    root.addEventListener('change', event => {
      const group = event.target.closest('[data-answer-group]');
      if (!group) return;
      if (event.target.name === 'message_id') {
        const message = source.transcript.find(m => m.id === event.target.value);
        const quote = group.querySelector('[name="quote"]'); quote.value = message?.text || ''; quote.setCustomValidity('');
      }
      if (event.target.name === 'value') {
        const na = event.target.value === 'na';
        const kind = group.querySelector('[name="evidence_kind"]');
        kind.value = na ? 'not_applicable' : kind.value === 'not_applicable' ? 'present' : kind.value;
        const reason = group.querySelector('[name="na_reason"]');
        if (reason) { reason.required = na; reason.closest('.field').hidden = !na; }
      }
      if (event.target.name === 'needs_review') {
        const field = group.querySelector('[name="limitation"]');
        field.required = event.target.checked; field.closest('.field').hidden = !event.target.checked;
      }
      const value = group.querySelector('[name="value"]');
      const status = group.querySelector('[data-answer-status]');
      if (status) status.textContent = value?.value ? 'Значение выбрано' : 'Не заполнено';
    });
    root.addEventListener('input', event => event.target.setCustomValidity?.(''));
    root.addEventListener('change', event => event.target.setCustomValidity?.(''));
  }
  function validateEvidence(group, source) {
    const quote = group.querySelector('[name="quote"]');
    const messageId = group.querySelector('[name="message_id"]').value;
    const message = source.transcript.find(m => m.id === messageId);
    if (!quote.value.trim() || !message?.text.includes(quote.value)) {
      quote.setCustomValidity('Укажите точный фрагмент выбранного сообщения без изменения текста.'); quote.reportValidity(); return null;
    }
    return [{ message_id: messageId, quote: quote.value }];
  }
  function revealInvalid(form) {
    // A fieldset also matches :invalid but cannot receive validation focus.
    const invalid = form.querySelector('input:invalid, select:invalid, textarea:invalid');
    if (invalid) { invalid.closest('details')?.setAttribute('open', ''); invalid.focus(); invalid.reportValidity(); return true; }
    return false;
  }
  function setSaving(form, state, value) {
    state.saving = value; form.querySelectorAll('button[type="submit"], [data-reload]').forEach(b => { b.disabled = value; });
    form.querySelector('button[type="submit"]')?.setAttribute('aria-busy', String(value));
  }
  function manualScope(rules, source, latestVersion = checklists.find(item => item.checklist_id === rules.checklist_id)?.version || rules.version, latest = history[0]) {
    return {
      server, kind: 'manual', entity: `${conversationId}:${rules.checklist_id}`, version: rules.version,
      fingerprint: draftFingerprint({ rules, source, latestVersion, latest: latest ? { id: latest.id, revision: latest.revision } : null }),
    };
  }

  function manualDraftBanner() {
    if (!checklist || !conversation) return '';
    let saved;
    try { saved = readDraft(manualScope(checklist, conversation)); }
    catch (error) { return `<div class="review-draft-banner notice"><p>${esc(error.message)}</p><button class="button ghost" data-action="delete-manual-draft">Удалить повреждённый черновик</button></div>`; }
    if (!saved) return '';
    return `<div class="review-draft-banner"><div><strong>Черновик ручной оценки · v${esc(saved.version)}</strong><p>${saved.compatible ? `Сохранён ${esc(dateText(saved.updatedAt))} в этой вкладке` : 'Версия правил или данные оценки изменились. Автоматическое восстановление недоступно.'}</p></div><div class="toolbar"><button class="button secondary" data-action="open-manual-draft">Открыть черновик</button><button class="button ghost" data-action="delete-manual-draft">Удалить</button></div></div>`;
  }

  async function loadManualContext(selected) {
    const [source, rules, latestRules, recent] = await Promise.all([
      api(`/api/v1/conversations/${encode(conversationId)}`),
      api(`/api/v1/checklists/${encode(selected.checklist_id)}/versions/${selected.version}`),
      api(`/api/v1/checklists/${encode(selected.checklist_id)}`),
      api(`/api/v1/conversations/${encode(conversationId)}/evaluations?limit=1`),
    ]);
    return { source, rules, latestRules, recent, scope: manualScope(rules, source, latestRules.version, recent.items[0]) };
  }

  function attachManualDraft(form, state, fail, context) {
    const { scope, rules } = context;
    const fields = ['value', 'explanation', 'na_reason', 'evidence_kind', 'message_id', 'quote', 'needs_review', 'limitation'];
    const fieldset = form.querySelector('[data-draft-fields]');
    const notice = form.querySelector('[data-draft-notice]');
    const status = form.querySelector('[data-draft-status]');
    let pending = null, timer = null, finished = false, stale = false, storageBlocked = false, withoutDraft = false, hasSavedDraft = false;
    try { pending = readDraft(scope); } catch (error) { storageBlocked = true; fail(error.message); }

    const collect = () => ({
      author: form.elements.author.value,
      answers: rules.criteria.map((criterion, index) => {
        const group = form.querySelector(`[data-answer-group="${index}"]`);
        return Object.fromEntries([['criterion_id', criterion.id], ...fields.map(name => {
          const input = group.querySelector(`[name="${name}"]`);
          return [name, name === 'needs_review' ? input.checked : input.value];
        })]);
      }),
    });
    const restoreFields = data => {
      form.elements.author.value = typeof data.author === 'string' ? data.author : '';
      rules.criteria.forEach((criterion, index) => {
        const answer = Array.isArray(data.answers) ? data.answers.find(item => item.criterion_id === criterion.id) : null;
        if (!answer) return;
        const group = form.querySelector(`[data-answer-group="${index}"]`);
        fields.forEach(name => {
          const input = group.querySelector(`[name="${name}"]`);
          if (name === 'needs_review') input.checked = answer[name] === true;
          else input.value = typeof answer[name] === 'string' || typeof answer[name] === 'number' ? String(answer[name]) : '';
        });
        group.querySelector('[name="value"]').dispatchEvent(new Event('change', { bubbles: true }));
        group.querySelector('[name="needs_review"]').dispatchEvent(new Event('change', { bubbles: true }));
      });
    };
    const showPending = () => {
      fieldset.disabled = !!pending || storageBlocked || stale;
      if (!pending && !storageBlocked && !stale) { notice.innerHTML = ''; return; }
      const compatible = pending?.compatible && !stale;
      notice.innerHTML = `<div class="review-draft-choice notice"><p>${storageBlocked ? 'Черновик недоступен для чтения. Можно удалить его или заполнить форму без сохранения черновика.' : compatible ? `Найден черновик от ${esc(dateText(pending.updatedAt))}. Восстановить заполненные поля?` : 'Черновик относится к другой версии правил или состояния оценки. Он сохранён отдельно и не применён к актуальным данным.'}</p><div class="toolbar">${compatible ? '<button class="button secondary" type="button" data-draft-restore>Восстановить</button>' : ''}<button class="button ghost" type="button" data-draft-delete>${stale ? 'Удалить черновик' : 'Удалить и начать заново'}</button>${storageBlocked ? '<button class="button secondary" type="button" data-draft-without>Продолжить без черновика</button>' : ''}</div>${pending && !compatible ? `<details><summary>Посмотреть сохранённые поля</summary><pre class="draft-preview">${esc(JSON.stringify(pending.data, null, 2))}</pre></details>` : ''}</div>`;
      notice.querySelector('[data-draft-without]')?.addEventListener('click', () => {
        withoutDraft = true; storageBlocked = false; showPending();
        status.textContent = 'Черновик недоступен. Сохраните оценку перед закрытием формы'; form.elements.author.focus();
      });
      notice.querySelector('[data-draft-restore]')?.addEventListener('click', async () => {
        const button = notice.querySelector('[data-draft-restore]'); button.disabled = true;
        try {
          const fresh = await loadManualContext(rules);
          if (!state.modal.el.isConnected || !alive) return;
          if (fresh.scope.fingerprint !== scope.fingerprint || String(fresh.scope.version) !== String(scope.version)) {
            pending.compatible = false; stale = true; showPending(); fail('Данные на сервере изменились. Сохранённые поля доступны для просмотра; откройте новую форму после проверки актуальной оценки.'); return;
          }
          const data = pending.data;
          restoreFields(data); pending = null; showPending(); state.dirty = true; state.flushDraft();
          form.elements.author.focus(); toast('Черновик восстановлен');
        } catch (error) { fail(errorText(error)); }
        finally { if (button.isConnected) button.disabled = false; }
      });
      notice.querySelector('[data-draft-delete]')?.addEventListener('click', () => {
        try {
          removeDraft(scope); pending = null; storageBlocked = false;
          if (stale) { finished = true; state.dirty = false; state.modal.close(); toast('Черновик удалён. Откройте ручную оценку заново, чтобы загрузить свежие данные.'); return; }
          showPending(); status.textContent = 'Изменения сохраняются в этой вкладке'; form.elements.author.focus();
        } catch (error) { fail(error.message); }
      });
    };
    state.flushDraft = () => {
      clearTimeout(timer); timer = null;
      if (finished || pending) return true;
      if (stale) return hasSavedDraft;
      if (storageBlocked || withoutDraft) return !state.dirty;
      if (!state.dirty) return true;
      try { writeDraft(scope, collect()); hasSavedDraft = true; status.textContent = 'Черновик сохранён в этой вкладке'; return true; }
      catch (error) { status.textContent = 'Черновик не сохранён'; fail(error.message); return false; }
    };
    const schedule = () => {
      if (pending || stale || finished || storageBlocked || withoutDraft) return;
      status.textContent = 'Сохраняем черновик…'; clearTimeout(timer);
      timer = setTimeout(() => state.flushDraft(), 400);
    };
    form.addEventListener('input', schedule); form.addEventListener('change', schedule);
    state.disposeDraft = () => { clearTimeout(timer); form.removeEventListener('input', schedule); form.removeEventListener('change', schedule); };
    showPending();
    return {
      blocked: () => !!pending || storageBlocked || stale,
      async ensureFresh() {
        const saved = state.flushDraft();
        const fresh = await loadManualContext(rules);
        if (fresh.scope.fingerprint === scope.fingerprint && String(fresh.scope.version) === String(scope.version)) return true;
        stale = true;
        try { pending = readDraft(scope); if (pending) pending.compatible = false; } catch {}
        showPending(); fail(`Правила, диалог или последняя оценка изменились на сервере. ${saved ? 'Черновик сохранён' : 'Поля оставлены в форме'}, отправка остановлена. Проверьте актуальные данные и откройте новую форму.`); return false;
      },
      saved() {
        finished = true; clearTimeout(timer); state.dirty = false;
        if (!withoutDraft) { try { removeDraft(scope); } catch (error) { toast(error.message, 'error'); } }
      },
    };
  }

  async function manualForm() {
    if (!checklist || busy || loadingHistory || loadingChecklist) return;
    let context;
    loadingChecklist = true; render();
    try { context = await loadManualContext(checklist); }
    catch (error) { pageError = errorText(error); }
    finally { loadingChecklist = false; }
    if (!alive) return;
    if (!context) { render(); return; }
    const selected = context.rules, source = context.source;
    conversation = source; checklist = selected;
    checklists = checklists.map(item => item.checklist_id === selected.checklist_id ? { ...item, version: context.latestRules.version, name: context.latestRules.name } : item);
    historyTotal = context.recent.total;
    if (context.recent.items[0]) history = [context.recent.items[0], ...history.filter(item => item.id !== context.recent.items[0].id)];
    render();
    const { modal, state, close, fail } = createModal('Ручная оценка', `<form class="manual-evaluation-form" novalidate><div data-draft-notice></div><div class="notice error" data-form-error role="alert" tabindex="-1" hidden></div><fieldset data-draft-fields><p class="muted">${esc(selected.name)} · v${selected.version}. Укажите ответы и цитаты по всем критериям (${selected.criteria.length}).</p><div class="notice">Ручная оценка сохранится в истории с именем автора</div><label class="field">Автор оценки<input name="author" maxlength="100" required autocomplete="name" placeholder="Ваше имя"></label>${selected.global_rules ? `<details class="criterion-rule"><summary>Общие правила</summary><p>${esc(selected.global_rules)}</p></details>` : ''}<div class="manual-criteria">${selected.criteria.map((c, i) => `<details class="manual-criterion" data-answer-group="${i}" ${i === 0 ? 'open' : ''}><summary><span class="criterion-number">${String(i + 1).padStart(2, '0')}</span><strong>${esc(c.name)}</strong><small class="muted" data-answer-status>Не заполнено</small><span class="manual-chevron" aria-hidden="true">${icon('chevron')}</span></summary><div class="manual-criterion-body"><p class="manual-rule">${esc(c.rule)}</p>${source.channel === 'voice call' && c.call_rule ? `<p class="manual-rule"><strong>Для звонка:</strong> ${esc(c.call_rule)}</p>` : ''}${c.allow_na ? `<p class="muted">N/A: ${esc(c.na_rule)}</p>` : ''}<label class="field">${c.type === 'flag' ? 'Выявлено нарушение?' : 'Значение критерия'}<select name="value" required>${options(c)}</select></label><label class="field">Объяснение оценки<textarea name="explanation" required maxlength="20000" rows="2"></textarea></label><label class="field" hidden>Почему критерий не применим<textarea name="na_reason" maxlength="5000" rows="2"></textarea></label>${evidenceFields(source)}<label class="review-checkbox"><input name="needs_review" type="checkbox"> Нужна дополнительная проверка</label><label class="field" hidden>Что не удалось проверить<textarea name="limitation" maxlength="5000" rows="2"></textarea></label></div></details>`).join('')}</div><div class="review-form-footer"><span class="muted">Для каждого ответа нужны пояснение и цитата</span><button class="button primary" type="submit">${icon('check')} Сохранить оценку</button></div></fieldset><p class="manual-draft-status" data-draft-status role="status">Изменения сохраняются в этой вкладке</p></form>`, true);
    const form = modal.body.querySelector('form'); bindAnswerFields(form, source);
    const draft = attachManualDraft(form, state, fail, context);
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (state.saving || draft.blocked() || revealInvalid(form)) return;
      const author = form.elements.author.value.trim();
      if (!author) return fail('Укажите имя автора оценки.');
      const answers = [];
      for (let i = 0; i < selected.criteria.length; i++) {
        const criterion = selected.criteria[i], group = form.querySelector(`[data-answer-group="${i}"]`);
        const get = name => group.querySelector(`[name="${name}"]`);
        group.setAttribute('open', '');
        const evidence = validateEvidence(group, source); if (!evidence) return;
        const value = readValue(criterion, get('value').value), kind = get('evidence_kind').value;
        if ((value === 'na') !== (kind === 'not_applicable')) return fail(`«${criterion.name}»: N/A и характер доказательства «Не применимо» должны быть выбраны вместе.`);
        if (!get('explanation').value.trim() || (value === 'na' && !get('na_reason').value.trim()) || (get('needs_review').checked && !get('limitation').value.trim())) return fail(`«${criterion.name}»: пояснения не должны состоять только из пробелов.`);
        answers.push({ criterion_id: criterion.id, value, explanation: get('explanation').value.trim(), evidence_kind: kind, evidence, na_reason: value === 'na' ? get('na_reason').value.trim() : '', needs_review: get('needs_review').checked, limitation: get('needs_review').checked ? get('limitation').value.trim() : '' });
      }
      setSaving(form, state, true); form.querySelector('[data-draft-fields]').disabled = true;
      try {
        if (!await draft.ensureFresh()) return;
        ensureServer();
        const op = prepareOperation(operationScope('manual'), '/api/v1/evaluations/manual', {conversation_id: conversationId, checklist_id: selected.checklist_id, checklist_version: selected.version, author, result:{answers}});
        const receipt = await sendOperation(op);
        const result = await api(E(receipt.id), {base:server});
        clearOperation(op);
        draft.saved(); recordResult(result); close(); render(); toast('Ручная оценка сохранена'); onChanged(result);
      } catch (error) { fail(errorText(error) + ' Сохранённый запрос можно восстановить после закрытия формы.'); render(); } finally { setSaving(form, state, false); form.querySelector('[data-draft-fields]').disabled = draft.blocked(); }
    });
  }
  function appealForm(criterionId) {
    const current = evaluation, criterion = current.checklist.criteria.find(c => c.id === criterionId);
    if (!criterion) return;
    const { modal, state, close, fail } = createModal('Оспорить оценку', `<form novalidate><p><strong>${esc(criterion.name)}</strong></p><p class="muted">Опишите, почему результат нужно пересмотреть. Решение и новая цитата сохранятся отдельно от исходной оценки.</p><label class="field">Ваше имя<input name="author" maxlength="100" required autocomplete="name"></label><label class="field">Причина апелляции<textarea name="comment" maxlength="5000" rows="4" required></textarea></label><div class="notice error" data-form-error role="alert" tabindex="-1" hidden></div><button class="button ghost" type="button" data-reload hidden>Обновить оценку</button><div class="review-form-footer"><button type="submit" class="button primary">Отправить на рассмотрение</button></div></form>`);
    const form = modal.body.querySelector('form');
    form.querySelector('[data-reload]').onclick = async () => { try { evaluation = await api(E(current.id)); render(); toast('Оценка обновлена. Черновик сохранён.'); } catch (error) { fail(errorText(error)); } };
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (state.saving || revealInvalid(form)) return;
      const author = form.elements.author.value.trim(), comment = form.elements.comment.value.trim();
      if (!author || !comment) return fail('Заполните имя и причину апелляции.');
      setSaving(form, state, true);
      try { const created = await api(`${E(current.id)}/appeals`, { method: 'POST', body: { criterion_id: criterionId, author, comment } }); evaluation = { ...current, appeals: [...(current.appeals || []), created] }; close(); render(); toast('Апелляция открыта'); onChanged(evaluation); }
      catch (error) { fail(errorText(error)); if (error.code === 'APPEAL_EXISTS') form.querySelector('[data-reload]').hidden = false; }
      finally { setSaving(form, state, false); }
    });
  }
  function decisionForm(appealId) {
    let current = evaluation;
    const appeal = current.appeals.find(a => a.id === appealId), criterion = current.checklist.criteria.find(c => c.id === appeal?.criterion_id);
    if (!appeal || !criterion) return;
    const answer = current.final.answers.find(a => a.criterion_id === criterion.id);
    const { modal, state, close, fail } = createModal('Решение по апелляции', `<form data-answer-group="decision" novalidate><p><strong>${esc(criterion.name)}</strong></p><blockquote class="appeal-comment">${esc(appeal.comment)}<footer>${esc(appeal.author)}</footer></blockquote><p class="muted">Исходное значение: ${esc(valueLabel(criterion, answer.value))}. Укажите итоговое значение и доказательство, даже если подтверждаете оценку.</p><label class="field">Ревьюер<input name="reviewer" maxlength="100" required autocomplete="name"></label><label class="field">Итоговое значение<select name="value" required>${options(criterion, answer.value)}</select></label><label class="field">Обоснование решения<textarea name="reason" maxlength="5000" rows="3" required></textarea></label>${evidenceFields(current.conversation, answer)}<div class="notice error" data-form-error role="alert" tabindex="-1" hidden></div><button class="button secondary" type="button" data-reload hidden>Обновить оценку, сохранив черновик</button><div class="review-form-footer"><span class="muted" data-revision>Редакция оценки: ${current.revision}</span><button type="submit" class="button primary">${icon('check')} Сохранить решение</button></div></form>`, true);
    const form = modal.body.querySelector('form'); bindAnswerFields(form, current.conversation);
    let closed = false;
    form.querySelector('[data-reload]').onclick = async () => {
      try {
        current = await api(E(current.id)); evaluation = current; render();
        closed = current.appeals.find(a => a.id === appealId)?.status !== 'open';
        form.querySelector('[data-revision]').textContent = `Редакция оценки: ${current.revision}`;
        form.querySelector('button[type="submit"]').disabled = closed;
        const latest = current.final.answers.find(a => a.criterion_id === criterion.id);
        fail(closed ? 'Апелляция уже рассмотрена. Черновик оставлен для сравнения; повторное решение недоступно.' : `Оценка обновлена. Текущее значение: ${valueLabel(criterion, latest.value)}. Проверьте черновик перед повторной отправкой.`);
      } catch (error) { fail(errorText(error)); }
    };
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (state.saving || closed || revealInvalid(form)) return;
      const evidence = validateEvidence(form, current.conversation); if (!evidence) return;
      const value = readValue(criterion, form.elements.value.value), kind = form.elements.evidence_kind.value;
      const reason = form.elements.reason.value.trim(), reviewer = form.elements.reviewer.value.trim();
      if (!reason || !reviewer) return fail('Укажите ревьюера и содержательное обоснование.');
      if ((value === 'na') !== (kind === 'not_applicable')) return fail('N/A и характер доказательства «Не применимо» должны быть выбраны вместе.');
      setSaving(form, state, true);
      try { const result = await api(`/api/v1/appeals/${encode(appealId)}/decision`, { method: 'POST', body: { expected_revision: current.revision, value, reason, reviewer, evidence_kind: kind, evidence } }); recordResult(result); close(); render(); toast('Решение сохранено, итог пересчитан'); onChanged(evaluation); }
      catch (error) { fail(errorText(error)); if (['REVISION_CONFLICT', 'APPEAL_CLOSED'].includes(error.code)) form.querySelector('[data-reload]').hidden = false; }
      finally { setSaving(form, state, false); if (closed) form.querySelector('button[type="submit"]').disabled = true; }
    });
  }
}
