import { api } from './api.js';
import { esc, icon, scoreText, toast } from './ui.js';
import { datasetRows, filterRows } from './dialogs-model.js';
import { openBatchDialog, getLegacyBatchTasks } from './batch.js';
import { findOperation } from './operations.js';

const PAGE_SIZE = 15;
export async function mountDialogs(container, options) {
  const { datasets, datasetId, onDatasetChange, onImport, onExport, onDemo, onRefresh, aiConfigured } = options;
  let alive = true, page = 0, batch = null, opening = false;
  let rows = [], filtered = [], visible = [];
  const selectedIds = new Set();
  const filters = {search: '', language: '', channel: '', status: '', sort: ''};
  const sizeObserver = new ResizeObserver(updateScrollHint);
  const wideFilters = matchMedia('(min-width: 1200px)');
  let compactFiltersOpen = false;
  container.classList.add('dialogs-page');
  const dataset = datasets.find(item => item.id === datasetId);
  const cleanup = () => {
    alive = false;
    sizeObserver.disconnect();
    wideFilters.removeEventListener('change', syncFilterLayout);
    if (batch?.canLeave()) batch.close();
    container.removeEventListener('click', click);
    container.removeEventListener('change', change);
    container.removeEventListener('input', input);
  };
  cleanup.canLeave = () => {
    if (opening || (batch && !batch.canLeave())) { toast('Дождитесь ответа на создание пакета. Проверка продолжится на сервере.', 'error'); return false; }
    return true;
  };
  container.innerHTML = '<div class="loading" role="status">Загружаем диалоги и последние результаты…</div>';
  if (dataset) {
    // One existing API call covers every page. Discard transcript/history snapshots
    // after building row summaries; do not cache dialogue contents in storage.
    try { rows = datasetRows(await api('/api/v1/datasets/' + encodeURIComponent(dataset.id) + '/export')); }
    catch (error) { cleanup(); throw error; }
  }
  if (!alive || !container.isConnected) return cleanup;
  render();
  wideFilters.addEventListener('change', syncFilterLayout);
  container.addEventListener('click', click);
  container.addEventListener('change', change);
  container.addEventListener('input', input);
  return cleanup;

  function render() {
    const chats = rows.filter(row => row.channel === 'agent chat').length;
    const rated = rows.filter(row => row.evaluation).length;
    const pendingReview = rows.filter(row => row.evaluation?.needsReview).length;
    const selection = '<div class="dataset-field"><label for="dataset">Набор данных</label><select id="dataset">' +
      datasets.map(item => '<option value="' + esc(item.id) + '"' + (item.id === datasetId ? ' selected' : '') + '>' + esc(item.name) + ' · ' + item.count + '</option>').join('') + '</select></div>';
    container.innerHTML =
      '<div class="page-heading"><div><h1>Диалоги</h1><p>Оценки по чек-листам и история проверок</p></div>' + (dataset ? '<div class="heading-actions"><button class="button secondary" id="export">' + icon('download') + 'Скачать архив</button><button class="button primary" id="import">' + icon('plus') + 'Загрузить диалоги</button></div>' : '') + '</div>' +
      (dataset ?
      '<div class="stats-strip"><div class="stat"><div class="stat-head">Всего диалогов</div><div class="stat-value">' + rows.length + '</div><div class="stat-caption">' + chats + ' чатов · ' + (rows.length - chats) + ' звонков</div></div><div class="stat"><div class="stat-head">Оценено</div><div class="stat-value">' + rated + '</div><div class="stat-caption">Последняя оценка каждого диалога</div></div><div class="stat"><div class="stat-head">Нужна проверка</div><div class="stat-value">' + pendingReview + '</div><div class="stat-caption">Есть неопределённые ответы</div></div><div class="stat"><div class="stat-head">Без оценки</div><div class="stat-value">' + (rows.length - rated) + '</div></div></div>' +
      '<div class="dataset-toolbar">' + selection + '<span class="dataset-group muted">Группа: ' + esc(dataset.group_id) + '</span><button class="button ghost" id="refresh-list" aria-label="Обновить диалоги">' + icon('refresh') + 'Обновить</button></div>' +
      '<div class="table-panel"><div class="dialog-filters"><div class="dialog-filter-main"><label class="field dialog-search"><span>ID диалога</span><span class="search-field">' + icon('search') + '<input id="search" type="search" placeholder="Например, DEV-001" autocomplete="off"></span></label>' +
      '<label class="field dialog-result-filter"><span>Результат</span><select id="result-status"><option value="">Все результаты</option><option value="unrated">Без оценки</option><option value="review">Нужна проверка</option><option value="violation">Есть грубое нарушение</option></select></label></div>' +
      '<details class="dialog-filter-options"' + (wideFilters.matches ? ' open' : '') + '><summary>Язык, канал и порядок<span data-extra-filter-count class="muted"></span></summary><div class="dialog-filter-fields">' +
      '<label class="field"><span>Язык</span><select id="language"><option value="">Все языки</option><option>RU</option><option>KZ</option><option>EN</option></select></label>' +
      '<label class="field"><span>Канал</span><select id="channel"><option value="">Все каналы</option><option value="agent chat">Чаты</option><option value="voice call">Звонки</option></select></label>' +
      '<label class="field"><span>Порядок</span><select id="score-sort"><option value="">Порядок набора</option><option value="asc">Сначала низкий балл</option><option value="desc">Сначала высокий балл</option></select></label></div></details>' +
      '<div class="dialog-filter-context"><span class="muted">По последней оценке во всём наборе</span><button class="button small ghost" data-dialog-action="reset" hidden>Сбросить фильтры</button></div></div>' +
      '<p class="sr-only" data-dialog-status role="status" aria-live="polite"></p><div id="selection-bar"></div><div id="table-content"></div></div>' :
      '<div class="table-panel empty"><h2>Диалогов пока нет</h2><p>Загрузите JSON с диалогами или откройте учебный пример</p><button class="button primary" id="empty-import">' + icon('upload') + 'Загрузить JSON</button> <button class="button secondary" id="demo">Открыть пример</button></div>') +
      '<div class="ai-note">' + icon('shield') + '<div><strong>' + (aiConfigured ? 'Модель подключена.' : 'AI не подключён.') + '</strong> ' + (aiConfigured ? 'Выберите диалоги для общей проверки или откройте карточку.' : 'Пакетная проверка станет доступна после подключения модели. В карточке доступна ручная оценка.') + '</div></div>';
    if (dataset) {
      table();
      container.querySelector('.dialog-filter-options').addEventListener('toggle', event => {
        if (!wideFilters.matches) compactFiltersOpen = event.currentTarget.open;
      });
    }
  }
  function syncFilterLayout() {
    const details = container.querySelector('.dialog-filter-options');
    if (!details) return;
    const hadFocus = details.contains(document.activeElement);
    details.open = wideFilters.matches || compactFiltersOpen;
    if (hadFocus && !details.open) details.querySelector('summary').focus({preventScroll: true});
  }
  function statusHTML(result) {
    if (!result) return '<span class="badge">Без оценки</span>';
    return '<span class="score-inline"><span class="score-number">' + scoreText(result.score) + '</span><span class="badge">' + (result.origin === 'manual' ? 'Вручную' : 'AI') + '</span></span><div class="row-meta">' +
      esc(result.checklistName || 'Чек-лист') + ' · v' + esc(result.checklistVersion || '') + '</div><div class="result-tags">' +
      (result.needsReview ? '<span class="badge amber">Нужна проверка</span>' : '') +
      (result.hasViolation ? '<span class="badge red">Грубое нарушение</span>' : '') + '</div>';
  }
  function table() {
    sizeObserver.disconnect();
    filtered = filterRows(rows, filters);
    page = Math.min(page, Math.max(0, Math.ceil(filtered.length / PAGE_SIZE) - 1));
    visible = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
    const host = container.querySelector('#table-content');
    host.innerHTML = visible.length ? '<p id="dialog-scroll-hint" class="dialog-scroll-hint" hidden>Все столбцы — прокрутите таблицу вбок. С клавиатуры используйте стрелки ← →</p><div class="table-wrap dialog-table-scroll" role="region" aria-label="Диалоги и последние оценки" aria-describedby="dialog-scroll-hint" tabindex="0"><table class="dialog-table"><thead><tr><th scope="col" class="selection-cell"><label class="row-select"><input type="checkbox" id="select-page" aria-label="Выбрать текущую страницу"></label></th><th scope="col">Диалог</th><th scope="col">Канал</th><th scope="col">Язык</th><th scope="col">Сообщения</th><th scope="col">Последняя оценка</th><th scope="col"><span class="muted">Открыть</span></th></tr></thead><tbody>' +
      visible.map(row => '<tr' + (selectedIds.has(row.id) ? ' class="is-selected"' : '') + '><td class="selection-cell"><label class="row-select"><input type="checkbox" data-select-dialog="' + esc(row.id) + '" aria-label="Выбрать диалог ' + esc(row.conversation_id) + '"' + (selectedIds.has(row.id) ? ' checked' : '') + '></label></td><td class="id-cell"><a class="conversation-link" href="#review/' + encodeURIComponent(row.id) + '">' + esc(row.conversation_id) + '</a></td><td><span class="channel">' + icon(row.channel === 'agent chat' ? 'chat' : 'phone') + (row.channel === 'agent chat' ? 'Чат' : 'Звонок') + '</span></td><td><span class="language-pill">' + esc(row.language) + '</span></td><td>' + row.message_count + '</td><td>' + statusHTML(row.evaluation) + '</td><td><a class="icon-btn" href="#review/' + encodeURIComponent(row.id) + '" aria-label="Открыть диалог ' + esc(row.conversation_id) + '">' + icon('arrow') + '</a></td></tr>').join('') +
      '</tbody></table></div><div class="table-foot"><span>' + (page * PAGE_SIZE + 1) + '–' + Math.min((page + 1) * PAGE_SIZE, filtered.length) + ' из ' + filtered.length + ' диалогов</span><div class="pagination"><button class="button small" data-dialog-action="prev"' + (!page ? ' disabled' : '') + ' aria-label="Предыдущая страница">←</button><button class="button small" data-dialog-action="next"' + ((page + 1) * PAGE_SIZE >= filtered.length ? ' disabled' : '') + ' aria-label="Следующая страница">→</button></div></div>' :
      '<div class="empty"><h3>Диалоги не найдены</h3><p>Измените условия или сбросьте фильтры.</p><button class="button secondary" data-dialog-action="reset">Сбросить фильтры</button></div>';
    updateSelection();
    const activeExtra = [filters.language, filters.channel === 'agent chat' ? 'Чаты' : filters.channel ? 'Звонки' : '', filters.sort === 'asc' ? 'Низкий балл сначала' : filters.sort ? 'Высокий балл сначала' : ''].filter(Boolean);
    container.querySelector('[data-extra-filter-count]').textContent = activeExtra.length ? ' · ' + activeExtra.join(' · ') : '';
    container.querySelector('.dialog-filter-context [data-dialog-action="reset"]').hidden = !Object.values(filters).some(Boolean);
    const scroller = container.querySelector('.dialog-table-scroll');
    if (scroller) { sizeObserver.observe(scroller); updateScrollHint(); }
  }
  function updateScrollHint() {
    const scroller = container.querySelector('.dialog-table-scroll');
    if (!scroller || !alive) return;
    const overflow = scroller.scrollWidth > scroller.clientWidth + 1;
    container.querySelector('#dialog-scroll-hint').hidden = !overflow;
    scroller.tabIndex = overflow ? 0 : -1;
  }
  function updateSelection() {
    const pageBox = container.querySelector('#select-page');
    if (pageBox) {
      const count = visible.filter(row => selectedIds.has(row.id)).length;
      pageBox.checked = count > 0 && count === visible.length;
      pageBox.indeterminate = count > 0 && count < visible.length;
    }
    const outside = rows.filter(row => selectedIds.has(row.id) && !filtered.some(item => item.id === row.id)).length;
    const bar = container.querySelector('#selection-bar');
    bar.classList.toggle('has-selection', selectedIds.size > 0);
    bar.innerHTML = '<div class="selection-summary"><span>' + (selectedIds.size ? 'Выбрано: <strong>' + selectedIds.size + '</strong>' + (outside ? ' · вне фильтра: ' + outside : '') : 'Выберите диалоги флажками') + '</span>' + (selectedIds.size ? '<span class="muted">До 100 диалогов в пакете</span>' : '') + '</div><div class="selection-actions"><button class="button small secondary" data-dialog-action="select-filter"' + (!filtered.length ? ' disabled' : '') + '>Выбрать все (' + filtered.length + ')</button>' + (selectedIds.size ? '<button class="button small ghost" data-dialog-action="clear-selection">Снять выбор</button><button class="button small primary" data-dialog-action="batch" aria-describedby="selection-ai-hint"' + (selectedIds.size > 100 || !aiConfigured || opening ? ' disabled' : '') + '>Оценить выбранные</button>' : '') + '</div>' + (selectedIds.size > 100 ? '<p class="form-error" role="alert">В одном пакете допустимо не более 100 диалогов. Уменьшите выбор.</p>' : '') + (selectedIds.size ? '<p id="selection-ai-hint" class="selection-help muted">' + (aiConfigured ? 'Будут проверены все выбранные диалоги, включая скрытые фильтром.' : 'AI пока не подключён. Откройте диалог, чтобы оценить его вручную.') + '</p>' : '');
    container.querySelector('[data-dialog-status]').textContent = 'Найдено диалогов: ' + filtered.length + '. На странице: ' + visible.length + '. Выбрано: ' + selectedIds.size + (outside ? ', вне фильтра: ' + outside : '') + '.';
    try {
      if (findOperation('batch:create') || getLegacyBatchTasks().length) bar.insertAdjacentHTML('beforeend', '<div class="batch-pending-note"><span>Есть сохранённый запрос проверки</span><a class="button small secondary" href="#batches">Восстановить пакет</a></div>');
    } catch {
      bar.insertAdjacentHTML('beforeend', '<p class="form-error" role="alert">Не удалось прочитать журнал проверок. Перед новой оценкой проверьте историю диалогов.</p>');
    }
  }
  function updateStats() {
    const values = container.querySelectorAll('.stat-value');
    values[1].textContent = rows.filter(row => row.evaluation).length;
    values[2].textContent = rows.filter(row => row.evaluation?.needsReview).length;
    values[3].textContent = rows.filter(row => !row.evaluation).length;
  }
  function applyFilters() { page = 0; table(); }
  function input(event) { if (event.target.id === 'search') { filters.search = event.target.value; applyFilters(); } }
  function change(event) {
    const field = event.target;
    if (field.id === 'dataset') return onDatasetChange(field.value);
    const key = {language: 'language', channel: 'channel', 'result-status': 'status', 'score-sort': 'sort'}[field.id];
    if (key) { filters[key] = field.value; applyFilters(); return; }
    if (field.id === 'select-page') {
      visible.forEach(row => field.checked ? selectedIds.add(row.id) : selectedIds.delete(row.id));
      table(); container.querySelector('#select-page')?.focus(); return;
    }
    if (field.dataset.selectDialog) {
      field.checked ? selectedIds.add(field.dataset.selectDialog) : selectedIds.delete(field.dataset.selectDialog);
      field.closest('tr').classList.toggle('is-selected', field.checked); updateSelection();
    }
  }
  async function click(event) {
    const button = event.target.closest('button');
    if (!button || button.disabled || !container.contains(button)) return;
    if (button.id === 'import' || button.id === 'empty-import') return onImport();
    if (button.id === 'export') return onExport();
    if (button.id === 'demo') return onDemo();
    if (button.id === 'refresh-list') return onRefresh();
    const action = button.dataset.dialogAction;
    if (action === 'prev' || action === 'next') { page += action === 'next' ? 1 : -1; table(); (container.querySelector('[data-dialog-action="' + action + '"]:not(:disabled)') || container.querySelector('.pagination button:not(:disabled)'))?.focus(); }
    if (action === 'reset') {
      for (const key of Object.keys(filters)) filters[key] = '';
      for (const id of ['search', 'language', 'channel', 'result-status', 'score-sort']) container.querySelector('#' + id).value = '';
      applyFilters(); container.querySelector('#search').focus();
    }
    if (action === 'select-filter') { filtered.forEach(row => selectedIds.add(row.id)); table(); container.querySelector('[data-dialog-action="select-filter"]').focus(); }
    if (action === 'clear-selection') { selectedIds.clear(); table(); container.querySelector('[data-dialog-action="select-filter"]').focus(); }
    if (action === 'batch' && !opening) {
      const chosenRows = rows.filter(row => selectedIds.has(row.id));
      if (!chosenRows.length) return;
      opening = true; updateSelection();
      try {
        batch = openBatchDialog({
          items: chosenRows.map(({id, conversation_id}) => ({id, conversation_id})),
          aiConfigured,
          onClosed: () => {
            batch = null;
            if (alive) {
              table(); updateStats();
              (container.querySelector('[data-dialog-action="batch"]:not(:disabled)') || container.querySelector('[data-dialog-action="select-filter"]'))?.focus();
            }
          },
        });
      } catch (error) { toast(error.message, 'error'); }
      finally { opening = false; if (alive) updateSelection(); }
    }
  }
}
