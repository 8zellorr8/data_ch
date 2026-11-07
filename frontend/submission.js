import { api, getApiBase } from './api.js';
import { esc, icon, dateText, scoreText, downloadJson } from './ui.js';

const expectedCount = split => split === 'dev' ? 65 : 10;
const terminal = new Set(['succeeded', 'failed', 'interrupted']);
export function buildSubmissionRequest({ split, teamName, modelMode, modelNames, source, batch, conversations, evaluations = [], selections = {} }) {
  if (!['dev', 'prod'].includes(split)) throw new Error('Выберите DEV или PROD.');
  const team_name = teamName.trim();
  if (!team_name || team_name.length > 200) throw new Error('Введите название команды: до 200 символов.');
  if (!['local', 'external_api', 'hybrid'].includes(modelMode)) throw new Error('Выберите способ запуска модели.');
  const names = [...new Set((Array.isArray(modelNames) ? modelNames : modelNames.split(/\r?\n/)).map(name => name.trim()).filter(Boolean))];
  if (!names.length || names.length > 20 || names.some(name => name.length > 200)) throw new Error('Укажите от 1 до 20 реально использованных моделей, каждую с новой строки (до 200 символов).');
  if (conversations.length !== expectedCount(split)) throw new Error(`Для ${split.toUpperCase()} требуется ${expectedCount(split)} диалогов. В выбранном наборе: ${conversations.length}.`);
  if (new Set(conversations.map(c => c.conversation_id)).size !== conversations.length) throw new Error('В наборе повторяются внешние ID диалогов. Исправьте набор перед сдачей.');
  const body = { split, team_name, model_mode: modelMode, model_names: names };
  let selectedEvaluations;
  if (source === 'batch') {
    if (!batch) throw new Error('Выберите завершённый пакет и дождитесь его загрузки.');
    if (!batch.completed || batch.items.some(item => !terminal.has(item.status))) throw new Error('Пакет ещё выполняется. Дождитесь завершения или отмените ожидающие элементы в разделе «Пакеты».');
    const ids = new Set(conversations.map(c => c.id));
    if (batch.items.length !== ids.size || new Set(batch.items.map(item => item.conversation_id)).size !== ids.size || batch.items.some(item => !ids.has(item.conversation_id))) throw new Error('Пакет должен содержать ровно все диалоги выбранного набора.');
    body.batch_id = batch.id;
    const chosen = new Set(batch.items.map(item => item.evaluation_id).filter(Boolean));
    selectedEvaluations = evaluations.filter(evaluation => chosen.has(evaluation.id));
  } else if (source === 'items') {
    selectedEvaluations = [];
    body.items = conversations.map(conversation => {
      const choice = selections[conversation.id];
      if (choice?.kind === 'error') {
        const error = (choice.error || '').trim();
        if (!error || error.length > 1000) throw new Error(`Укажите причину отсутствия результата для ${conversation.conversation_id}: до 1000 символов.`);
        return { conversation_id: conversation.conversation_id, error };
      }
      const evaluation = evaluations.find(item => item.id === choice?.evaluationId && item.conversation_id === conversation.id && item.origin === 'ai');
      if (!evaluation) throw new Error(`Выберите AI-оценку или укажите ошибку для ${conversation.conversation_id}.`);
      selectedEvaluations.push(evaluation);
      return { conversation_id: conversation.conversation_id, evaluation_id: evaluation.id };
    });
  } else throw new Error('Выберите источник результатов.');
  const missing = [...new Set(selectedEvaluations.map(e => e.metadata?.model).filter(name => name && !names.includes(name)))];
  if (missing.length) throw new Error('В списке моделей отсутствуют названия из выбранных оценок: ' + missing.join(', ') + '.');
  return body;
}

export async function mountSubmission(host, { datasets = [], datasetId, batchId: initialBatchId = '', onDatasetChange = () => {} } = {}) {
  const server = getApiBase(), abort = new AbortController();
  let alive = true, busy = false, snapshot = null, batches = [], batchesTotal = 0, batch = null, batchToken = 0;
  let batchLoading = false, listLoading = false;
  const values = { split: 'dev', teamName: '', modelMode: '', modelNames: '', source: 'batch', batchId: '' };
  const selections = {};
  const valid = () => alive && host.isConnected && server === getApiBase();
  const cleanup = () => { alive = false; abort.abort(); host.removeEventListener('click', click); host.removeEventListener('change', change); host.removeEventListener('input', input); host.removeEventListener('submit', submit); };
  cleanup.canLeave = () => !busy;
  async function request(path, options = {}) {
    if (!valid()) throw new Error('Подключение к серверу изменилось. Откройте экран заново.');
    return api(path, { ...options, base: server, signal: abort.signal });
  }
  host.addEventListener('click', click); host.addEventListener('change', change); host.addEventListener('input', input); host.addEventListener('submit', submit);
  await load();
  return cleanup;

  async function load() {
    host.innerHTML = '<div class="loading" role="status">Загружаем набор для сдачи…</div>';
    if (!datasetId) { render(); return; }
    try {
      // A coherent snapshot supplies explicit evaluation choices without fetching every history.
      const result = await request('/api/v1/datasets/' + encodeURIComponent(datasetId) + '/export');
      if (!valid()) return;
      snapshot = result; render();
      await loadBatches();
      if (valid() && initialBatchId) {
        values.batchId = initialBatchId;
        const select = host.querySelector('#submission-batch');
        if (![...select.options].some(option => option.value === initialBatchId)) {
          const option = document.createElement('option'); option.value = initialBatchId; option.textContent = 'Выбранный пакет · ' + initialBatchId.slice(0, 8); select.append(option);
        }
        select.value = initialBatchId;
        await selectBatch(initialBatchId);
      }
    } catch (error) {
      if (valid()) host.innerHTML = '<div class="page-heading"><div><h1>Сдача результатов</h1></div></div><div class="notice error" role="alert">' + esc(error.message) + '</div><button class="button secondary" data-submission-action="retry">Повторить</button>';
    }
  }
  function render() {
    if (!valid()) return;
    host.innerHTML = '<div class="submission-page"><div class="page-heading"><div><h1>Сдача результатов</h1><p>JSON 1.0 для организаторов хакатона</p></div><a class="button secondary" href="#dialogs">К диалогам</a></div>' +
      (!snapshot ? '<div class="panel empty"><h2>Сначала загрузите набор</h2><p>Для сдачи нужны исходные диалоги и результаты их проверок</p><a class="button primary" href="#dialogs">Загрузить диалоги</a></div>' :
      '<div class="dataset-toolbar"><label class="dataset-field">Набор данных<select data-submission-field="dataset" aria-label="Набор для сдачи">' + datasets.map(dataset => '<option value="' + esc(dataset.id) + '"' + (dataset.id === datasetId ? ' selected' : '') + '>' + esc(dataset.name) + ' · ' + dataset.count + '</option>').join('') + '</select></label><span class="muted">Группа: ' + esc(snapshot.dataset.group_id) + '</span></div>' +
      '<p class="notice">Файл содержит исходные AI-оценки до апелляций. Ручные оценки доступны в архиве данных. Совместимость с официальными восемью критериями проверит сервер.</p>' +
      '<form class="submission-form stack" novalidate><section class="panel stack" aria-labelledby="submission-details"><h2 id="submission-details">1. Команда и модели</h2><div class="form-grid">' +
      field('split', 'Этап сдачи', '<select id="submission-split" data-submission-field="split"><option value="dev">DEV · 65 диалогов</option><option value="prod">PROD · 10 чатов</option></select>') +
      field('teamName', 'Название команды', '<input id="submission-teamName" data-submission-field="teamName" maxlength="200" required autocomplete="organization">') +
      field('modelMode', 'Способ запуска модели', '<select id="submission-modelMode" data-submission-field="modelMode" required><option value="">Выберите способ</option><option value="external_api">Внешний API</option><option value="local">Локальная модель</option><option value="hybrid">Гибридный</option></select>') +
      field('modelNames', 'Использованные модели', '<textarea id="submission-modelNames" data-submission-field="modelNames" required rows="3" aria-describedby="submission-model-help"></textarea><small id="submission-model-help">Точные названия, каждое с новой строки. Включите модели реальных неудачных вызовов</small>') +
      '</div><div data-submission-models class="submission-model-hint"></div></section>' +
      '<section class="panel stack" aria-labelledby="submission-source"><h2 id="submission-source">2. Источник результатов</h2><fieldset class="submission-modes"><legend class="sr-only">Способ выбора результатов</legend><label><input type="radio" name="submission-source" value="batch" checked> Завершённый пакет</label><label><input type="radio" name="submission-source" value="items">Выбрать по диалогам</label></fieldset>' +
      '<div data-submission-batch-pane class="stack"><div class="field"><label for="submission-batch">Пакет на все диалоги набора</label><select id="submission-batch" data-submission-field="batchId"><option value="">Загружаем пакеты…</option></select></div><p class="muted small-text">Список завершённых пакетов этого сервера. После выбора проверяется принадлежность набору</p><div class="toolbar"><button class="button secondary small" type="button" data-submission-action="refresh-batches">' + icon('refresh') + 'Обновить пакеты</button><button class="button secondary small" type="button" data-submission-action="more-batches" hidden>Загрузить ещё</button><a href="#batches">Открыть пакеты</a></div><div data-submission-batch-info aria-live="polite"></div></div>' +
      '<div data-submission-items-pane hidden><p class="muted">Выберите конкретную исходную AI-оценку для каждого внешнего ID. Если результата нет, укажите короткую причину без ключей и персональных данных</p>' + selectionRows() + '</div></section>' +
      '<section class="panel stack submission-download" aria-label="Скачать файл"><h2>3. Проверьте и скачайте</h2><div data-submission-coverage role="status"></div><p data-submission-next class="submission-next"></p><p class="muted">Сохраните файл как <code>results.json</code>, затем добавьте его в <code data-submission-destination>results/dev/results.json</code> и отправьте в репозиторий команды</p><div id="submission-validation-error" data-submission-error class="form-error" role="alert" tabindex="-1"></div><button class="button primary" type="submit" data-submission-download>' + icon('download') + 'Скачать results.json</button><div data-submission-success aria-live="polite"></div></section></form>') + '</div>';
    if (!snapshot) return;
    for (const key of ['split', 'teamName', 'modelMode', 'modelNames']) host.querySelector('[data-submission-field="' + key + '"]').value = values[key];
    updateCoverage(); updateModels();
  }
  function field(id, label, control) { return '<div class="field"><label for="submission-' + id + '">' + label + '</label>' + control + '</div>'; }
  function selectionRows() {
    return '<div class="submission-rows">' + snapshot.conversations.map((conversation, index) => {
      const evaluations = snapshot.evaluations.filter(e => e.conversation_id === conversation.id && e.origin === 'ai');
      return '<div class="submission-row" data-submission-conversation="' + esc(conversation.id) + '"><div><a href="#review/' + encodeURIComponent(conversation.id) + '">' + esc(conversation.conversation_id) + '</a><div class="muted small-text">' + esc(conversation.language) + ' · ' + (conversation.channel === 'voice call' ? 'Звонок' : 'Чат') + '</div></div><div class="field"><label for="submission-eval-' + index + '">Исходный результат</label><select id="submission-eval-' + index + '" data-submission-choice="' + esc(conversation.id) + '"><option value="">Выберите оценку или ошибку</option>' + evaluations.map(e => '<option value="' + esc(e.id) + '">' + esc(dateText(e.created_at)) + ' · ' + esc(e.metadata?.model || 'Модель не указана') + ' · ' + esc(e.checklist.name) + ' v' + esc(e.checklist.version) + ' · исходный балл ' + esc(scoreText(e.original_score.total)) + ' · ' + esc(e.id.slice(0, 8)) + '</option>').join('') + '<option value="__error">Нет результата — указать ошибку</option></select>' + (!evaluations.length ? '<small>AI-оценок этого диалога пока нет</small>' : '') + '<a data-submission-eval-link hidden>Открыть выбранную оценку</a></div><div class="field" data-submission-error-field hidden><label for="submission-failure-' + index + '">Причина ошибки</label><input id="submission-failure-' + index + '" data-submission-failure="' + esc(conversation.id) + '" maxlength="1000" placeholder="Например, AI_TIMEOUT"></div></div>';
    }).join('') + '</div>';
  }
  async function loadBatches(append = false) {
    if (listLoading || !valid()) return;
    listLoading = true;
    const previous = values.batchId;
    const more = host.querySelector('[data-submission-action="more-batches"]');
    if (more) more.disabled = true;
    try {
      const result = await request('/api/v1/evaluation-batches?limit=100&offset=' + (append ? batches.length : 0));
      if (!valid()) return;
      batches = append ? [...batches, ...result.items.filter(item => !batches.some(old => old.id === item.id))] : result.items;
      batchesTotal = result.total;
      const select = host.querySelector('#submission-batch');
      select.innerHTML = '<option value="">Выберите завершённый пакет</option>' + batches.filter(item => item.completed).map(item => '<option value="' + esc(item.id) + '">' + esc(dateText(item.created_at)) + ' · ' + item.total + ' диалогов · ' + esc(item.checklist_id) + ' v' + esc(item.checklist_version) + ' · ' + esc(item.id.slice(0, 8)) + '</option>').join('');
      if (previous && ![...select.options].some(option => option.value === previous)) {
        const option = document.createElement('option'); option.value = previous; option.textContent = 'Выбранный пакет · ' + previous.slice(0, 8); select.append(option);
      }
      select.value = previous;
      if (!previous && !batches.some(item => item.completed)) host.querySelector('[data-submission-batch-info]').innerHTML = '<div class="notice">Завершённых пакетов на загруженных страницах пока нет. Можно выбрать результаты по диалогам' + (batches.length < batchesTotal ? ' или загрузить следующую страницу пакетов' : '') + '</div>';
      more.hidden = batches.length >= batchesTotal;
      more.textContent = 'Загрузить ещё (' + batches.length + ' из ' + batchesTotal + ')';
      updateCoverage();
    } catch (error) { if (valid()) host.querySelector('[data-submission-batch-info]').innerHTML = '<div class="notice error" role="alert">' + esc(error.message) + '</div>'; }
    finally { listLoading = false; if (valid()) more.disabled = false; }
  }
  async function selectBatch(id) {
    const token = ++batchToken; batch = null; batchLoading = Boolean(id); updateCoverage(); updateModels();
    const info = host.querySelector('[data-submission-batch-info]');
    info.innerHTML = id ? '<div role="status">Проверяем состав пакета…</div>' : '';
    if (!id) return;
    try {
      const result = await request('/api/v1/evaluation-batches/' + encodeURIComponent(id));
      if (!valid() || token !== batchToken) return;
      batch = result;
      const ids = new Set(snapshot.conversations.map(c => c.id));
      const completeSet = result.items.length === ids.size && new Set(result.items.map(item => item.conversation_id)).size === ids.size && result.items.every(item => ids.has(item.conversation_id));
      const successes = result.items.filter(item => item.status === 'succeeded').length;
      const failures = result.items.filter(item => ['failed', 'interrupted'].includes(item.status)).length;
      info.innerHTML = '<div class="notice' + (!completeSet || !result.completed ? ' amber' : '') + '"><strong>' + (completeSet && result.completed ? 'Пакет завершён и содержит весь набор' : !result.completed ? 'Пакет ещё выполняется' : 'Состав пакета не совпадает с выбранным набором') + '</strong><p>Успешно: ' + successes + ' · С ошибкой или прервано: ' + failures + '. Ошибки сохраняются в файле отдельными результатами.</p><a href="#batches/' + encodeURIComponent(result.id) + '">Просмотреть пакет</a></div>';
      // Refresh metadata once: the batch may have completed after the initial snapshot.
      const fresh = await request('/api/v1/datasets/' + encodeURIComponent(datasetId) + '/export');
      if (!valid() || token !== batchToken) return;
      snapshot.evaluations = fresh.evaluations;
      refreshEvaluationChoices(); updateModels();
    } catch (error) { if (valid() && token === batchToken) { batch = null; info.innerHTML = '<div class="notice error" role="alert">' + esc(error.message) + '</div>'; } }
    finally { if (valid() && token === batchToken) { batchLoading = false; updateCoverage(); } }
  }
  function refreshEvaluationChoices() {
    // Only replace options: keep explicit choices, error text and focused inputs.
    // New evaluations can appear while a batch finishes after the initial load.
    for (const conversation of snapshot.conversations) {
      const select = [...host.querySelectorAll('[data-submission-choice]')].find(control => control.dataset.submissionChoice === conversation.id);
      if (!select) continue;
      const evaluations = snapshot.evaluations.filter(e => e.conversation_id === conversation.id && e.origin === 'ai');
      select.innerHTML = '<option value="">Выберите оценку или ошибку</option>' + evaluations.map(e => '<option value="' + esc(e.id) + '">' + esc(dateText(e.created_at)) + ' · ' + esc(e.metadata?.model || 'Модель не указана') + ' · ' + esc(e.checklist.name) + ' v' + esc(e.checklist.version) + ' · исходный балл ' + esc(scoreText(e.original_score.total)) + ' · ' + esc(e.id.slice(0, 8)) + '</option>').join('') + '<option value="__error">Нет результата — указать ошибку</option>';
      const choice = selections[conversation.id];
      select.value = choice?.kind === 'error' ? '__error' : choice?.evaluationId || '';
      const hint = select.parentElement.querySelector('small');
      if (hint && evaluations.length) hint.remove();
    }
    updateCoverage();
  }
  function updateModels() {
    if (!snapshot || !valid()) return;
    const ids = new Set(values.source === 'batch' ? (batch?.items || []).map(item => item.evaluation_id) : Object.values(selections).map(choice => choice.evaluationId));
    const names = [...new Set(snapshot.evaluations.filter(e => ids.has(e.id) && e.origin === 'ai').map(e => e.metadata?.model).filter(Boolean))];
    host.querySelector('[data-submission-models]').innerHTML = names.length ? '<span class="muted">В выбранных оценках сохранены модели:</span> ' + names.map(name => '<code>' + esc(name) + '</code>').join(' ') + '<p class="muted small-text">Укажите эти названия в поле выше. Модели неудачных вызовов добавьте отдельно</p>' : '<span class="muted">Названия моделей можно сверить после выбора сохранённых AI-оценок. Не указывайте модель, которую команда не запускала</span>';
  }
  function updateCoverage() {
    if (!snapshot || !valid()) return;
    const count = snapshot.conversations.length, expected = expectedCount(values.split);
    const selected = snapshot.conversations.filter(c => selections[c.id]?.kind === 'evaluation' || (selections[c.id]?.kind === 'error' && selections[c.id].error.trim())).length;
    host.querySelector('[data-submission-destination]').textContent = 'results/' + values.split + '/results.json';
    host.querySelector('[data-submission-coverage]').innerHTML = '<strong>' + esc(values.split.toUpperCase()) + ': ' + count + ' из ' + expected + ' диалогов</strong>' + (values.source === 'items' ? '<span>Выбрано исходов: ' + selected + ' из ' + count + '</span>' : '') + (count !== expected ? '<p class="form-error">Выберите другой набор или измените этап сдачи</p>' : '');
    const next = host.querySelector('[data-submission-next]');
    next.textContent = count !== expected ? `Для ${values.split.toUpperCase()} нужен набор из ${expected} диалогов` : batchLoading ? 'Проверяем состав пакета…' : values.source === 'batch' && !batch ? 'Выберите завершённый пакет в шаге 2' : values.source === 'items' && selected !== count ? `Выберите оценку или укажите ошибку ещё для ${count-selected} диалогов` : 'Состав выбран. Сервер проверит исходные оценки перед скачиванием';
    host.querySelector('[data-submission-download]').disabled = busy || batchLoading;
  }
  function change(event) {
    if (!valid() || busy) return;
    const control = event.target, fieldName = control.dataset.submissionField;
    if (fieldName === 'dataset') { onDatasetChange(control.value); return; }
    if (fieldName) { control.removeAttribute('aria-invalid'); control.setCustomValidity(''); values[fieldName] = control.value; if (fieldName === 'batchId') selectBatch(control.value); updateCoverage(); }
    if (control.name === 'submission-source') {
      values.source = control.value;
      host.querySelector('[data-submission-batch-pane]').hidden = values.source !== 'batch';
      host.querySelector('[data-submission-items-pane]').hidden = values.source !== 'items';
      updateCoverage(); updateModels();
    }
    const id = control.dataset.submissionChoice;
    if (id) {
      control.removeAttribute('aria-invalid');
      const row = control.closest('[data-submission-conversation]');
      const isError = control.value === '__error';
      selections[id] = isError ? { kind: 'error', error: row.querySelector('[data-submission-failure]').value } : { kind: 'evaluation', evaluationId: control.value };
      if (!control.value) delete selections[id];
      row.querySelector('[data-submission-error-field]').hidden = !isError;
      const link = row.querySelector('[data-submission-eval-link]');
      link.hidden = isError || !control.value;
      if (!link.hidden) link.href = '#review/' + encodeURIComponent(id) + '/' + encodeURIComponent(control.value);
      updateCoverage(); updateModels();
    }
  }
  function input(event) {
    if (!valid() || busy) return;
    const fieldName = event.target.dataset.submissionField;
    if (fieldName && fieldName !== 'dataset') { values[fieldName] = event.target.value; event.target.setCustomValidity(''); event.target.removeAttribute('aria-invalid'); }
    const id = event.target.dataset.submissionFailure;
    if (id) { selections[id] = { kind: 'error', error: event.target.value }; updateCoverage(); }
    const success = host.querySelector('[data-submission-success]'); if (success) success.innerHTML = '';
  }
  function click(event) {
    const action = event.target.closest('[data-submission-action]')?.dataset.submissionAction;
    if (!action || busy) return;
    if (action === 'retry') load();
    if (action === 'more-batches') loadBatches(true);
    if (action === 'refresh-batches') loadBatches(false);
  }
  async function submit(event) {
    if (!event.target.matches('.submission-form')) return;
    event.preventDefault();
    if (!valid() || busy || batchLoading) return;
    const form = event.target, errorHost = host.querySelector('[data-submission-error]');
    errorHost.textContent = ''; host.querySelector('[data-submission-success]').innerHTML = '';
    const requiredControl = !values.teamName.trim() ? host.querySelector('#submission-teamName') : !values.modelMode ? host.querySelector('#submission-modelMode') : !values.modelNames.trim() ? host.querySelector('#submission-modelNames') : null;
    if (requiredControl) { requiredControl.setAttribute('aria-invalid','true'); requiredControl.setCustomValidity('Заполните это поле перед скачиванием результатов.'); requiredControl.reportValidity(); requiredControl.focus(); return; }
    let body;
    try { body = buildSubmissionRequest({ ...values, batch, conversations: snapshot.conversations, evaluations: snapshot.evaluations, selections }); }
    catch (error) {
      errorHost.textContent = error.message;
      let invalid = snapshot.conversations.length !== expectedCount(values.split) ? host.querySelector('#submission-split') : values.source==='batch' && !batch ? host.querySelector('#submission-batch') : null;
      if (!invalid && values.source==='items') {
        const missing = snapshot.conversations.find(c => !selections[c.id]?.evaluationId && !(selections[c.id]?.kind==='error' && selections[c.id].error.trim()));
        if (missing) invalid=[...host.querySelectorAll('[data-submission-choice],[data-submission-failure]')].find(el => (selections[missing.id]?.kind==='error' ? el.dataset.submissionFailure : el.dataset.submissionChoice)===missing.id);
      }
      if (invalid) { invalid.setAttribute('aria-invalid','true'); invalid.setAttribute('aria-describedby','submission-validation-error'); invalid.focus(); } else errorHost.focus();
      return;
    }
    if (!form.reportValidity()) return;
    busy = true; form.setAttribute('aria-busy', 'true');
    const controls = [...host.querySelectorAll('input,select,textarea,button')];
    const previousDisabled = controls.map(control => control.disabled);
    controls.forEach(control => control.disabled = true);
    const button = host.querySelector('[data-submission-download]'); button.textContent = 'Проверяем и формируем файл…';
    try {
      const result = await request('/api/v1/datasets/' + encodeURIComponent(datasetId) + '/submission', { method: 'POST', body });
      if (!valid()) return;
      downloadJson(result, 'results.json');
      const errors = result.results.filter(item => item.status === 'error').length;
      host.querySelector('[data-submission-success]').innerHTML = '<div class="notice"><strong>Файл сформирован</strong><p>Результатов: ' + result.results.length + ' · Ошибок: ' + errors + '. Путь для сдачи: <code>results/' + values.split + '/results.json</code>. Отправка в Git выполняется командой отдельно.</p></div>';
    } catch (error) { if (valid()) { errorHost.textContent = (error.code === 'INVALID_SUBMISSION' ? 'Сервер отклонил файл: ' : '') + error.message; errorHost.focus(); } }
    finally {
      busy = false;
      if (valid()) { form.removeAttribute('aria-busy'); controls.forEach((control, index) => control.disabled = previousDisabled[index]); button.innerHTML = icon('download') + 'Скачать results.json'; updateCoverage(); }
    }
  }
}
