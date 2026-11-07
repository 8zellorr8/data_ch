import { api, getApiBase } from './api.js';
import { esc, icon, openModal, dateText } from './ui.js';
import { prepareOperation, sendOperation, findOperation, clearOperation } from './operations.js';

const endpoint = '/api/v1/evaluation-batches';
const scope = 'batch:create';
const encode = encodeURIComponent;
const terminal = new Set(['succeeded', 'failed', 'interrupted']);
const labels = { pending: 'В очереди', running: 'Оценивается', succeeded: 'Готово', failed: 'Ошибка', interrupted: 'Прервано' };
const reasons = {
  BATCH_CANCELLED_BEFORE_START: 'Отменено до запуска',
  AI_NOT_CONFIGURED: 'Модель не подключена', AI_INVALID_RESULT: 'Ответ модели не прошёл проверку',
  AI_TIMEOUT: 'Истекло время ожидания модели', AI_UNAVAILABLE: 'Модель недоступна',
  EVALUATION_INTERRUPTED: 'Обработка прервана на сервере', WORKER_INTERRUPTED: 'Обработчик остановлен',
};

export function batchCounts(batch) {
  const items = batch?.items || [];
  const count = status => items.filter(item => item.status === status).length;
  return { total: items.length, pending: count('pending'), running: count('running'), succeeded: count('succeeded'),
    failed: count('failed'), interrupted: count('interrupted'), finished: items.filter(item => terminal.has(item.status)).length };
}
export function batchItemLabel(item) {
  return item.status === 'interrupted' && item.error_code === 'BATCH_CANCELLED_BEFORE_START' ? 'Отменено до запуска' : labels[item.status] || item.status;
}

// Keep old, unkeyed browser requests visible during migration. They cannot be
// safely replayed using a newly generated server idempotency key.
export function getLegacyBatchTasks() {
  try {
    const raw = sessionStorage.getItem('ketnavr-batch-v1:' + encode(getApiBase()));
    if (!raw) return [];
    const saved = JSON.parse(raw);
    if (saved.schema !== 1 || !Array.isArray(saved.tasks)) throw new Error();
    return saved.tasks.filter(item => ['running', 'uncertain'].includes(item.status) && !item.reviewed_at);
  } catch { throw new Error('Не удалось прочитать журнал предыдущей версии. Проверьте историю диалогов перед новой проверкой.'); }
}

/** The browser only monitors jobs; leaving the view never cancels server work. */
export function createBatchController({ request = api, getBase = getApiBase, operations = {prepareOperation, sendOperation, findOperation, clearOperation},
  onChange = () => {}, pollMs = 2500, schedule = setTimeout, unschedule = clearTimeout } = {}) {
  const base = getBase();
  let disposed = false, generation = 0, timer = null;
  let state = { record: null, busy: false, creating: false, cancelling: false, paused: false, error: '', warning: '' };
  const getState = () => structuredClone({ ...state, counts: batchCounts(state.record) });
  const notify = () => { if (!disposed) { try { onChange(getState()); } catch { /* Presentation does not change server outcome. */ } } };
  const ensureServer = () => { if (getBase() !== base) throw new Error('Адрес сервера изменился. Вернитесь к исходному серверу, чтобы открыть пакет.'); };
  const clearTimer = () => { if (timer !== null) unschedule(timer); timer = null; };
  function nextPoll() {
    clearTimer();
    if (!disposed && !state.paused && state.record && !state.record.completed && getBase() === base) timer = schedule(() => { timer = null; refresh().catch(() => {}); }, pollMs);
  }
  async function refresh(id = state.record?.id) {
    if (!id || disposed || state.creating || state.cancelling) return getState();
    ensureServer(); clearTimer();
    const current = ++generation;
    state.busy = true; state.error = ''; notify();
    try {
      const record = await request(endpoint + '/' + encode(id), {base});
      if (disposed || current !== generation || getBase() !== base) return getState();
      if (record?.id !== id || !Array.isArray(record.items)) throw new Error('Сервер вернул неожиданный пакет. Обновите страницу.');
      state.record = record;
    } catch (error) {
      if (!disposed && current === generation) state.error = error.message;
      throw error;
    } finally {
      if (!disposed && current === generation) { state.busy = false; notify(); nextPoll(); }
    }
    return getState();
  }
  async function submit(operation) {
    ensureServer();
    if (state.creating || state.record) throw new Error('Пакет уже создаётся или создан.');
    state.creating = true; state.error = ''; notify();
    try {
      const record = await operations.sendOperation(operation);
      if (!record?.id || !Array.isArray(record.items)) throw new Error('Не удалось подтвердить создание пакета. Повторите сохранённый запрос с тем же ключом.');
      state.record = record;
      try { operations.clearOperation(operation); }
      catch { state.warning = 'Пакет создан, но локальный журнал не очищен. Повтор сохранённого запроса вернёт тот же пакет.'; }
    } catch (error) { state.error = error.message; throw error; }
    finally { state.creating = false; notify(); }
    // Replayed POST returns its original pending snapshot. Always read current status.
    if (!disposed && getBase() === base) await refresh().catch(() => {});
    return getState();
  }
  async function start({ items, checklistId, checklistVersion }) {
    if (disposed) throw new Error('Экран уже закрыт');
    if (state.creating || state.record) throw new Error('Пакет уже создаётся или создан.');
    ensureServer();
    const ids = (items || []).map(item => item.id);
    if (!ids.length || ids.length > 100 || ids.some(id => typeof id !== 'string' || !id) || new Set(ids).size !== ids.length) throw new Error('Выберите от 1 до 100 разных диалогов.');
    const version = Number(checklistVersion);
    if (!checklistId || !Number.isInteger(version) || version < 1) throw new Error('Выберите чек-лист и его версию.');
    const body = {conversation_ids: [...ids], checklist_id: checklistId, checklist_version: version};
    return submit(operations.prepareOperation(scope, endpoint, body));
  }
  async function retryCreation() {
    ensureServer();
    const op = operations.findOperation(scope);
    if (!op) throw new Error('Сохранённый запрос не найден.');
    return submit(op);
  }
  async function cancel() {
    ensureServer();
    if (!state.record || state.record.completed || state.cancelling || disposed) return getState();
    clearTimer(); const current = ++generation;
    state.cancelling = true; state.busy = false; state.error = ''; notify();
    try {
      const record = await request(endpoint + '/' + encode(state.record.id) + '/cancel', {method: 'POST', base});
      if (disposed || current !== generation || getBase() !== base) return getState();
      if (record?.id !== state.record.id || !Array.isArray(record.items)) throw new Error('Не удалось подтвердить отмену. Обновите пакет.');
      state.record = record;
    } catch (error) { if (!disposed && current === generation) state.error = error.message; throw error; }
    finally { if (!disposed && current === generation) { state.cancelling = false; notify(); nextPoll(); } }
    return getState();
  }
  function dispose() { disposed = true; generation++; clearTimer(); }
  function pause() { state.paused = true; clearTimer(); notify(); }
  async function resume() { state.paused = false; return refresh(); }
  return {start, retryCreation, refresh, cancel, pause, resume, dispose, getState};
}

export function openBatchDialog({items = [], aiConfigured = false, onClosed = () => {}} = {}) {
  const selection = items.map(({id, conversation_id}) => ({id, conversation_id}));
  const modal = openModal('Проверить выбранные диалоги', '<p role="status">Загружаем чек-листы…</p>', {wide: true});
  const base = getApiBase();
  let closed = false, loading = true, error = '', lists = [], chosen = '', version = 1, creating = false;
  const controller = createBatchController();
  const canLeave = () => !creating;
  const close = () => { if (canLeave()) modal.close(); };
  modal.el.addEventListener('cancel', event => { if (!canLeave()) event.preventDefault(); });
  modal.el.addEventListener('close', () => { if (closed) return; closed = true; controller.dispose(); onClosed(controller.getState()); }, {once: true});
  modal.body.addEventListener('change', event => {
    if (event.target.name === 'batch-checklist') { chosen = event.target.value; version = lists.find(item => item.checklist_id === chosen)?.version || 1; render(); }
    if (event.target.name === 'batch-version') version = Number(event.target.value);
  });
  modal.body.addEventListener('click', async event => {
    const button = event.target.closest('[data-batch-action]');
    if (!button || button.disabled) return;
    if (button.dataset.batchAction === 'close') return close();
    if (button.dataset.batchAction !== 'start' || creating) return;
    creating = true; error = ''; render();
    try {
      if (base !== getApiBase()) throw new Error('Адрес сервера изменился. Откройте форму заново.');
      if (getLegacyBatchTasks().some(task => selection.some(item => item.id === task.id))) throw new Error('Для выбранных диалогов остались старые запросы с неизвестным исходом. Сначала откройте их историю в разделе «Пакеты».');
      const state = await controller.start({items: selection, checklistId: chosen, checklistVersion: version});
      creating = false; close(); location.hash = '#batches/' + encode(state.record.id);
    } catch (cause) { error = cause.message; }
    finally { creating = false; render(); }
  });
  (async () => {
    try { lists = (await api('/api/v1/checklists', {base})).items; chosen = lists[0]?.checklist_id || ''; version = lists[0]?.version || 1; }
    catch (cause) { error = cause.message; }
    finally { loading = false; render(); }
  })();
  function render() {
    if (closed) return;
    const focused = document.activeElement?.name;
    let pending;
    try { pending = findOperation(scope); } catch (cause) { error = cause.message; }
    const maxVersion = lists.find(item => item.checklist_id === chosen)?.version || 1;
    modal.el.querySelector('[data-modal-close]').disabled = creating;
    modal.body.innerHTML = `<div class="batch-dialog" aria-busy="${loading || creating}"><p>Выбрано диалогов: <strong>${selection.length}</strong>. Пакет продолжит работу на сервере после закрытия страницы.</p>
      <div class="form-grid"><label class="field">Чек-лист<select name="batch-checklist" ${loading || creating || !lists.length ? 'disabled' : ''}>${lists.map(item => `<option value="${esc(item.checklist_id)}" ${item.checklist_id === chosen ? 'selected' : ''}>${esc(item.name)}</option>`).join('')}</select></label>
      <label class="field">Версия<select name="batch-version" ${loading || creating || !chosen ? 'disabled' : ''}>${Array.from({length: maxVersion}, (_, i) => maxVersion - i).map(n => `<option value="${n}" ${n === version ? 'selected' : ''}>v${n}</option>`).join('')}</select></label></div>
      ${!aiConfigured ? '<div class="notice">Модель не подключена. Ручная оценка доступна в карточке диалога.</div>' : ''}
      ${pending ? '<div class="notice amber">Есть сохранённый запрос создания пакета. <a href="#batches" data-recover-batch>Проверить и восстановить</a></div>' : ''}
      ${error ? `<div class="notice error" role="alert">${esc(error)}</div>` : ''}
      <ol class="batch-task-list" aria-label="Выбранные диалоги">${selection.map(item => `<li><span class="batch-task-id">${esc(item.conversation_id || item.id)}</span></li>`).join('')}</ol>
      <div class="batch-footer"><button class="button secondary" data-batch-action="close" ${creating ? 'disabled' : ''}>Закрыть</button><button class="button primary" data-batch-action="start" ${loading || creating || !aiConfigured || !chosen || !selection.length || selection.length > 100 || pending ? 'disabled' : ''}>${icon('shield')}${creating ? 'Создаём пакет…' : 'Создать пакет'}</button></div></div>`;
    modal.body.querySelector('[data-recover-batch]')?.addEventListener('click', () => close());
    if (focused) modal.body.querySelector(`[name="${focused}"]`)?.focus();
  }
  return {el: modal.el, close, canLeave, controller};
}

function badge(item) {
  return `<span class="badge ${item.status === 'succeeded' ? 'green' : item.status === 'failed' ? 'red' : item.status === 'interrupted' ? 'amber' : ''}">${esc(batchItemLabel(item))}</span>`;
}
function countsHTML(counts) {
  return `<div class="batch-counts" role="status"><span>Завершено<strong>${counts.finished} / ${counts.total}</strong></span><span>Готово<strong>${counts.succeeded}</strong></span><span>Ошибка<strong>${counts.failed}</strong></span><span>Прервано / отменено<strong>${counts.interrupted}</strong></span><span>В очереди<strong>${counts.pending}</strong></span><span>В работе<strong>${counts.running}</strong></span></div>`;
}

export async function mountBatches(host, {batchId} = {}) {
  const base = getApiBase();
  let alive = true, page = 0, list = null, listBusy = false, error = '', timer = null, serial = 0;
  let recovery = null, recoveryBusy = false, recoveryController = null, releaseModal = null;
  let paused = false, lastListView = '', lastDetailView = '';
  const controller = batchId ? createBatchController({onChange: renderDetail}) : null;
  const active = () => alive && host.isConnected && getApiBase() === base;
  const clearTimer = () => { if (timer !== null) clearTimeout(timer); timer = null; };
  const cleanup = () => { alive = false; serial++; clearTimer(); controller?.dispose(); recoveryController?.dispose(); releaseModal?.close(); host.removeEventListener('click', click); };
  cleanup.canLeave = () => !recoveryBusy;
  host.addEventListener('click', click);
  if (batchId) { renderDetail(controller.getState()); await controller.refresh(batchId).catch(() => {}); }
  else await loadList();
  return cleanup;

  async function loadList() {
    if (!active()) return;
    clearTimer(); const token = ++serial; listBusy = true; error = ''; renderList();
    try {
      const result = await api(endpoint + '?limit=20&offset=' + page * 20, {base});
      if (!active() || token !== serial) return;
      list = result;
      // New batches shift offset pages; a manual refresh always starts at page 1.
    } catch (cause) { if (active() && token === serial) error = cause.message; }
    finally {
      if (active() && token === serial) {
        listBusy = false; renderList();
        if (!paused && list?.items.some(item => !item.completed)) timer = setTimeout(loadList, 2500);
      }
    }
  }
  function recoveryHTML() {
    let operation = null;
    try { operation = findOperation(scope); } catch (cause) { return `<div class="notice error" role="alert">${esc(cause.message)}</div>`; }
    let old = [], legacyError = '';
    try { old = getLegacyBatchTasks(); } catch (cause) { legacyError = cause.message; }
    return (operation ? `<section class="panel batch-recovery"><h2>Восстановить создание пакета</h2><p>Сохранён запрос: ${operation.body.conversation_ids?.length || 0} диалогов · ${esc(operation.body.checklist_id)} · v${esc(operation.body.checklist_version)}. Повтор использует прежний ключ и возвращает тот же пакет, если сервер уже его создал.</p>${operation.error?.message ? `<p class="muted">${esc(operation.error.message)}</p>` : ''}${recovery ? `<div role="alert" class="notice error">${esc(recovery)}</div>` : ''}<div class="toolbar"><button class="button secondary" data-batch-view="recover" ${recoveryBusy ? 'disabled' : ''}>${recoveryBusy ? 'Проверяем сохранённый запрос…' : 'Восстановить пакет'}</button><button class="button ghost" data-batch-view="release" ${recoveryBusy ? 'disabled' : ''}>Подготовить новый запрос</button></div></section>` : '') +
      (old.length ? `<section class="panel batch-recovery"><h2>Запросы из предыдущей версии</h2><p>Их исход неизвестен. Они отправлялись без ключа повторов; проверьте историю до новой оценки.</p><ul>${old.map(item => `<li><a href="#review/${encode(item.id)}">${esc(item.conversation_id || item.id)}</a> · ${esc(item.checklistId)} · v${esc(item.checklistVersion)}</li>`).join('')}</ul><button class="button secondary" data-batch-view="release-legacy">История проверена</button></section>` : '') +
      (legacyError ? `<div class="notice error" role="alert">${esc(legacyError)}</div>` : '');
  }
  function releaseRequest(legacy = false) {
    const op = legacy ? null : findOperation(scope);
    if (!legacy && !op) return;
    const modal = openModal('Подготовить новый запрос', `<form class="stack"><p>Сначала проверьте список пакетов и историю диалогов. Новый запрос получит новый ключ и может повторно вызвать модель. Уже созданный пакет продолжит работу на сервере.</p><label><input type="checkbox" required> Я проверил историю и хочу разрешить новую проверку</label><div class="form-error" role="alert"></div><button class="button primary" type="submit">Разрешить новый запрос</button></form>`);
    releaseModal = modal;
    modal.el.addEventListener('close', () => { if (releaseModal === modal) releaseModal = null; }, {once: true});
    modal.body.querySelector('form').onsubmit = event => {
      event.preventDefault();
      try {
        if (!active()) throw new Error('Экран или адрес сервера изменился. Откройте список пакетов заново.');
        if (legacy) {
          const key = 'ketnavr-batch-v1:' + encode(base);
          const saved = JSON.parse(sessionStorage.getItem(key));
          if (saved?.schema !== 1 || !Array.isArray(saved.tasks)) throw new Error('Не удалось прочитать старый журнал');
          saved.tasks.forEach(item => { if (['running', 'uncertain'].includes(item.status)) item.reviewed_at = new Date().toISOString(); });
          sessionStorage.setItem(key, JSON.stringify(saved));
        } else {
          const current = findOperation(scope);
          if (current?.key !== op.key) throw new Error('Сохранённый запрос изменился. Обновите список пакетов.');
          clearOperation(op);
        }
        modal.close(); recovery = ''; renderList();
      } catch (cause) { modal.body.querySelector('.form-error').textContent = cause.message; }
    };
  }
  function renderList() {
    if (!active()) return;
    const currentView=JSON.stringify([list,error,recovery,recoveryBusy,paused,page,recoveryHTML()]);
    if (currentView===lastListView) { const button=host.querySelector('[data-batch-view="refresh"]'); if(button)button.disabled=listBusy||recoveryBusy; return; }
    lastListView=currentView;
    const focused = document.activeElement?.dataset.batchView;
    const focusedLink = host.contains(document.activeElement) ? document.activeElement?.getAttribute('href') : null;
    host.innerHTML = `<div class="page-heading"><div><h1>Пакеты проверок</h1><p>Общая очередь команды на выбранном сервере</p></div><div class="heading-actions"><button class="button secondary" data-batch-view="refresh" ${listBusy || recoveryBusy ? 'disabled' : ''}>${icon('refresh')}Обновить</button><a class="button primary" href="#dialogs">Выбрать диалоги</a></div></div>${recoveryHTML()}${list?.items.some(item=>!item.completed) || paused ? `<div class="batch-update-controls"><button type="button" class="button secondary small" data-batch-view="pause">${paused ? 'Продолжить обновление' : 'Приостановить обновление'}</button><span>${paused ? 'Прогресс на экране приостановлен. Проверка на сервере продолжается.' : 'Прогресс обновляется автоматически каждые 2,5 с'}</span></div>` : ''}
      ${error ? `<div class="notice error" role="alert">${esc(error)}</div>` : ''}
      ${!list ? `<div class="panel" role="status">${listBusy ? 'Загружаем пакеты…' : 'Пакеты не загружены. Повторите запрос.'}</div>` : !list.items.length ? '<div class="panel empty"><h2>На этой странице пакетов нет</h2><p>Выберите диалоги и создайте пакет проверки.</p></div>' : `<div class="table-panel"><p class="batch-scroll-hint">Таблица шире экрана · прокрутите вправо, чтобы увидеть результаты</p><div class="table-wrap" tabindex="0" role="region" aria-label="Пакеты проверок и результаты"><table class="batch-table"><thead><tr><th>Пакет</th><th>Правила</th><th>Состояние</th><th>Результаты</th></tr></thead><tbody>${list.items.map(item => `<tr><td><a href="#batches/${encode(item.id)}">${esc(dateText(item.created_at))}</a><small class="batch-record-id">${esc(item.id)}</small></td><td>${esc(item.checklist_id)}<div class="muted">Версия ${item.checklist_version}</div></td><td><span class="badge ${item.completed && !item.failed && !item.interrupted ? 'green' : ''}">${item.completed ? 'Завершён' : 'В работе'}</span><div class="muted">${item.pending} в очереди · ${item.running} в работе</div></td><td><strong>${item.succeeded} / ${item.total} готово</strong><div class="muted">${item.failed} ошибок · ${item.interrupted} прервано / отменено</div></td></tr>`).join('')}</tbody></table></div></div>`}
      ${list ? `<div class="table-foot"><span role="status">Пакетов: ${list.total} · страница ${page + 1}</span><div class="pagination"><button class="button secondary small" data-batch-view="prev" ${!page || listBusy || recoveryBusy ? 'disabled' : ''}>Назад</button><button class="button secondary small" data-batch-view="next" ${(page + 1) * 20 >= list.total || listBusy || recoveryBusy ? 'disabled' : ''}>Далее</button></div></div>` : ''}`;
    if (focused) host.querySelector(`[data-batch-view="${focused}"]`)?.focus({preventScroll: true});
    else if (focusedLink) [...host.querySelectorAll('a[href]')].find(link => link.getAttribute('href') === focusedLink)?.focus({preventScroll: true});
  }
  function renderDetail(state) {
    if (!active()) return;
    const currentView=JSON.stringify([state.record,state.error,state.warning,state.cancelling,state.paused]);
    if(currentView===lastDetailView) { const button=host.querySelector('[data-batch-view="refresh"]');if(button)button.disabled=state.busy||state.cancelling; return; }
    lastDetailView=currentView;
    const focused = document.activeElement?.dataset.batchView;
    const focusedLink = host.contains(document.activeElement) ? document.activeElement?.getAttribute('href') : null;
    const record = state.record, counts = state.counts;
    host.innerHTML = `<div class="page-heading"><div><a href="#batches" class="back-link">← Все пакеты</a><h1>Проверка диалогов</h1><p>${record ? `${esc(record.checklist_id)} · версия ${record.checklist_version} · ${esc(dateText(record.created_at))}` : 'Загружаем пакет'}</p></div><div class="heading-actions"><button class="button secondary" data-batch-view="refresh" ${state.busy || state.cancelling ? 'disabled' : ''}>${icon('refresh')}Обновить</button>${record && !record.completed ? `<button class="button secondary" data-batch-view="cancel" ${!counts.pending || state.cancelling ? 'disabled' : ''}>${state.cancelling ? 'Отменяем ожидающие…' : 'Отменить ожидающие'}</button>` : ''}</div></div>
      ${state.error ? `<div class="notice error" role="alert">${esc(state.error)}</div>` : ''}${state.warning ? `<div class="notice amber">${esc(state.warning)}</div>` : ''}
      ${record ? `<section class="panel batch-detail"><div class="section-heading"><h2>${record.completed ? 'Пакет завершён' : 'Проверка выполняется на сервере'}</h2><span class="badge">${record.completed ? 'Все элементы обработаны' : state.paused ? 'Обновление приостановлено' : 'Обновление каждые 2,5 с'}</span></div><p class="muted">${record.completed ? 'Проверьте ошибки и прерванные элементы. Завершение пакета не означает, что все диалоги оценены.' : 'Страницу можно закрыть. Отмена действует только на ожидающие диалоги; текущий вызов модели продолжится.'}</p>${!record.completed ? `<div class="batch-update-controls"><button type="button" class="button secondary small" data-batch-view="pause">${state.paused ? 'Продолжить обновление' : 'Приостановить обновление'}</button>${state.paused ? '<span>Прогресс на экране приостановлен. Проверка на сервере продолжается.</span>' : ''}</div>` : ''}${countsHTML(counts)}<progress class="batch-progress" value="${counts.finished}" max="${counts.total || 1}" aria-label="Обработано ${counts.finished} из ${counts.total}"></progress>
      <ol class="batch-task-list batch-results" aria-label="Результаты пакета">${record.items.map((item, i) => `<li><div class="batch-task-id"><a href="#review/${encode(item.conversation_id)}${item.evaluation_id ? '/' + encode(item.evaluation_id) : ''}">Диалог ${i + 1}</a><small>${esc(item.conversation_id)}</small>${item.error_code ? `<small>${esc(reasons[item.error_code] || 'Ошибка обработки')} · ${esc(item.error_code)}</small>` : ''}</div><div class="batch-task-status">${badge(item)}${item.evaluation_id ? `<a class="button secondary small" href="#review/${encode(item.conversation_id)}/${encode(item.evaluation_id)}">Открыть оценку</a>` : ''}</div></li>`).join('')}</ol><p class="batch-record-id">ID пакета: ${esc(record.id)}</p><div class="toolbar"><a class="button secondary" href="#reports?batch=${encode(record.id)}">Расход пакета</a>${record.completed ? `<a class="button secondary" href="#submission?batch=${encode(record.id)}">Подготовить сдачу</a>` : ''}</div></section>` : '<div class="panel" role="status">Нет данных пакета</div>'}`;
    if (focused) host.querySelector(`[data-batch-view="${focused}"]`)?.focus({preventScroll: true});
    else if (focusedLink) [...host.querySelectorAll('a[href]')].find(link => link.getAttribute('href') === focusedLink)?.focus({preventScroll: true});
  }
  async function click(event) {
    const button = event.target.closest('[data-batch-view]');
    if (!button || button.disabled || !active()) return;
    const action = button.dataset.batchView;
    if (action === 'pause') {
      if(batchId) { if(controller.getState().paused) await controller.resume().catch(()=>{}); else controller.pause(); }
      else { paused=!paused; clearTimer(); if(paused)renderList();else await loadList(); }
      return;
    }
    if (action === 'release' || action === 'release-legacy') { releaseRequest(action === 'release-legacy'); return; }
    if (action === 'recover' && !recoveryBusy) {
      recoveryBusy = true; recovery = ''; clearTimer(); renderList();
      recoveryController = createBatchController();
      try { const value = await recoveryController.retryCreation(); if (active()) location.hash = '#batches/' + encode(value.record.id); }
      catch (cause) { recovery = cause.message; }
      finally { recoveryBusy = false; recoveryController.dispose(); if (active()) await loadList(); }
      return;
    }
    if (batchId) {
      if (action === 'refresh') await controller.refresh(batchId).catch(() => {});
      if (action === 'cancel') await controller.cancel().catch(() => {});
    } else {
      if (action === 'refresh') page = 0;
      if (action === 'prev') page = Math.max(0, page - 1);
      if (action === 'next') page++;
      await loadList();
    }
  }
}
