import { api } from './api.js';
import { esc, toast, icon, dateText } from './ui.js';

const types = { binary: 'Да / нет', scale: 'Шкала', flag: 'Флаг нарушения' };
const clone = value => JSON.parse(JSON.stringify(value));
const definitionOf = value => ({ name: value.name, global_rules: value.global_rules || '', criteria: clone(value.criteria) });
const newCriterion = id => ({ id, name: '', section: '', type: 'binary', weight: 10, rule: '', call_rule: '', scale: [], allow_na: false, na_rule: '' });

/** Mounts the checklist editor. Call cleanup.canLeave() before navigating away. */
export async function mountChecklists(container, context = {}) {
  let items = [];
  let selected = null;
  let draft = null;
  let dirty = false;
  let busy = false;
  let disposed = false;
  let requestNumber = 0;
  let serverError = '';
  let conflict = false;
  let errors = [];
  let openCriteria = new Set([0]);
  const readonly = () => selected && selected.version < (items.find(i => i.checklist_id === selected.checklist_id)?.version || selected.version);
  const confirmDiscard = () => !dirty || window.confirm('Есть несохранённые изменения. Покинуть редактор и потерять эти изменения?');
  const beforeUnload = event => {
    if (!dirty && !busy) return;
    event.preventDefault();
    event.returnValue = '';
  };
  window.addEventListener('beforeunload', beforeUnload);

  function field(key, label, value, options = {}) {
    const id = `cl-${key}`;
    const error = errors.find(e => e.field === key)?.message;
    const common = `id="${id}" name="${key}" ${options.required ? 'required' : ''} ${readonly() || busy ? 'disabled' : ''} ${error ? `aria-invalid="true" aria-describedby="${id}-error"` : ''}`;
    let control;
    if (options.tag === 'textarea') {
      control = `<textarea ${common} rows="${options.rows || 3}" maxlength="${options.max || 20000}">${esc(value ?? '')}</textarea>`;
    } else if (options.tag === 'select') {
      control = `<select ${common}>${options.values.map(([v, text]) => `<option value="${esc(v)}" ${String(value) === String(v) ? 'selected' : ''}>${esc(text)}</option>`).join('')}</select>`;
    } else {
      control = `<input ${common} value="${esc(value ?? '')}" type="${options.type || 'text'}" ${options.type === 'number' ? 'min="0" max="1000" step="any"' : `maxlength="${options.max || 200}"`} ${options.pattern ? `pattern="${options.pattern}"` : ''}>`;
    }
    return `<div class="field${options.wide ? ' cl-wide' : ''}"><label for="${id}">${label}${options.required ? ' <span class="cl-required" aria-hidden="true">*</span>' : ''}</label>${control}${options.help ? `<small class="muted">${options.help}</small>` : ''}${error ? `<small class="cl-field-error" id="${id}-error">${esc(error)}</small>` : ''}</div>`;
  }

  function criterionCard(c, i) {
    const ro = readonly();
    const prefix = `criterion-${i}`;
    const hasError = errors.some(e => e.field.startsWith(prefix));
    const scaleValue = c._scaleText ?? c.scale.join(', ');
    return `<details class="cl-criterion ${hasError ? 'cl-invalid' : ''}" data-index="${i}" ${openCriteria.has(i) || hasError ? 'open' : ''}>
      <summary><span class="cl-criterion-number">${String(i + 1).padStart(2, '0')}</span><span class="cl-criterion-title"><strong>${esc(c.name || 'Новый критерий')}</strong><small>${esc(c.section || 'Без раздела')}</small></span><span class="badge">${types[c.type]}</span><span class="cl-weight">${c.type === 'flag' ? 'Без веса' : `Вес ${esc(c.weight)}`}</span><span class="cl-chevron">${icon('chevron')}</span></summary>
      <div class="cl-criterion-body"><div class="form-grid cl-fields">
        ${field(`${prefix}-name`, 'Название критерия', c.name, { required: true, wide: true })}
        ${field(`${prefix}-id`, 'ID критерия', c.id, { required: true, max: 100, pattern: '[A-Za-z0-9_-]+', help: 'Латинские буквы, цифры, дефис и подчёркивание' })}
        ${field(`${prefix}-section`, 'Раздел', c.section)}
        ${field(`${prefix}-type`, 'Тип оценки', c.type, { tag: 'select', values: Object.entries(types) })}
        ${c.type === 'flag' ? '<div class="field"><label>Вес</label><div class="cl-static-field">0 · отдельный флаг</div><small class="muted">Нарушение не обнуляет итоговый балл</small></div>' : field(`${prefix}-weight`, 'Вес', c.weight, { type: 'number', required: true })}
        ${c.type === 'scale' ? field(`${prefix}-scale`, 'Значения шкалы', scaleValue, { required: true, wide: true, max: 1000, help: 'Через запятую, например: 0, 4, 7, 10. Обязателен 0; значения уникальны.' }) : ''}
        ${field(`${prefix}-rule`, 'Правило проверки', c.rule, { tag: 'textarea', required: true, rows: 4, wide: true })}
        ${field(`${prefix}-call_rule`, 'Уточнение для звонков', c.call_rule, { tag: 'textarea', rows: 2, wide: true, help: 'Для транскриптов звонков, необязательно' })}
      </div><div class="cl-na-section"><label class="cl-check"><input type="checkbox" name="${prefix}-allow_na" ${c.allow_na ? 'checked' : ''} ${ro || busy ? 'disabled' : ''}><span>Разрешить «Неприменимо» (N/A)</span></label><p class="muted cl-helper">При N/A вес критерия исключается из расчёта</p>
      ${c.allow_na ? field(`${prefix}-na_rule`, 'Когда критерий неприменим', c.na_rule, { tag: 'textarea', rows: 2, required: true, max: 5000 }) : ''}</div>
      ${!ro ? `<div class="cl-criterion-footer"><span class="muted">${esc(c.id)}</span><button type="button" class="button ghost danger" data-action="remove" data-index="${i}" ${draft.criteria.length <= 1 ? 'disabled' : ''}>${icon('trash')} Удалить критерий</button></div>` : ''}</div></details>`;
  }

  function render() {
    if (disposed) return;
    const ro = readonly();
    const latest = selected && items.find(i => i.checklist_id === selected.checklist_id)?.version;
    const weights = draft?.criteria.filter(c => c.type !== 'flag').reduce((sum, c) => sum + (Number(c.weight) || 0), 0) || 0;
    container.innerHTML = `<section class="cqa-checklists">
      <div class="page-heading"><div><h1>Чек-листы</h1><p class="muted">Критерии, веса и версии правил</p></div><button class="button primary" data-action="new" ${busy ? 'disabled' : ''}>${icon('plus')} Новый чек-лист</button></div>
      <div class="cl-layout"><aside class="panel cl-library"><div class="cl-library-title"><h2>Библиотека</h2><span class="badge">${items.length}</span></div>
        ${items.length ? items.map(item => `<button class="cl-library-item ${selected?.checklist_id === item.checklist_id ? 'is-active' : ''}" data-action="select" data-id="${esc(item.checklist_id)}" ${busy ? 'disabled' : ''} ${selected?.checklist_id === item.checklist_id ? 'aria-current="true"' : ''}><span class="cl-library-icon">${icon('list')}</span><span><strong>${esc(item.name)}</strong><small>Версия ${item.version} · ${esc(dateText(item.created_at))}</small></span></button>`).join('') : '<p class="muted cl-library-empty">Нет чек-листов</p>'}
        <div class="cl-library-note">${icon('shield')}<p>Каждое сохранение создаёт новую версию. Прошлые оценки сохраняют свои правила.</p></div></aside>
      <div class="cl-editor">${draft ? `<form id="cl-form" novalidate>
        <div class="panel cl-meta"><div class="cl-editor-top"><div><span class="badge ${ro ? 'amber' : 'green'}">${selected ? `Версия ${selected.version}${ro ? ' · архив' : ' · актуальная'}` : 'Новый чек-лист'}</span>${dirty ? '<span class="cl-unsaved">Есть изменения</span>' : ''}</div><div class="cl-version-controls">${selected ? `<label for="cl-version" class="muted">Версия</label><select id="cl-version" aria-label="Версия чек-листа" ${busy ? 'disabled' : ''}>${Array.from({ length: latest || selected.version }, (_, index) => index + 1).reverse().map(version => `<option value="${version}" ${version === selected.version ? 'selected' : ''}>v${version}${version === latest ? ' · актуальная' : ''}</option>`).join('')}</select><button type="button" class="button ghost" data-action="copy" ${busy ? 'disabled' : ''}>${icon('file')} Создать копию</button>` : ''}</div></div>
        ${ro ? '<div class="notice">Архивная версия доступна для просмотра. Выберите актуальную версию для редактирования или создайте отдельную копию.</div>' : ''}
        ${field('name', 'Название чек-листа', draft.name, { required: true })}
        ${field('global_rules', 'Общие правила', draft.global_rules, { tag: 'textarea', rows: 3, help: 'Применяются ко всем критериям. Можно оставить пустым.' })}
        <div class="cl-meta-bottom"><span><strong>${draft.criteria.length}</strong> критериев</span><span>Сумма весов <strong>${weights}</strong></span><small class="muted">Итог нормируется до 100; сумма весов может отличаться от 100.</small></div></div>
        <div class="cl-section-label"><h2>Критерии оценки</h2><span class="muted">${draft.criteria.length} / 40</span></div>
        <div id="cl-errors" ${errors.length ? 'role="alert" tabindex="-1" class="notice cl-error-summary"' : ''}>${errors.length ? `<strong>Проверьте заполнение</strong><ul>${errors.map(e => `<li><a href="#cl-${e.field}" data-error-target="cl-${e.field}">${esc(e.message)}</a></li>`).join('')}</ul>` : ''}</div>
        ${serverError ? `<div class="notice cl-server-error" role="alert"><strong>${conflict ? 'Чек-лист изменён другим участником' : 'Не удалось сохранить'}</strong><p>${esc(serverError)}</p>${conflict ? '<p>Ваш черновик остаётся в редакторе. Можно сохранить его отдельным чек-листом или загрузить актуальную версию.</p><div class="toolbar"><button type="button" class="button secondary" data-action="copy">Сохранить как отдельный чек-лист</button><button type="button" class="button ghost" data-action="reload">Загрузить актуальную версию</button></div>' : ''}</div>` : ''}
        <div class="cl-criteria">${draft.criteria.map(criterionCard).join('')}</div>
        ${!ro ? `<button type="button" class="cl-add-criterion" data-action="add" ${draft.criteria.length >= 40 || busy ? 'disabled' : ''}>${icon('plus')} Добавить критерий</button><div class="cl-save-bar"><div><strong>${selected ? `Будет создана версия ${(selected.version || 0) + 1}` : 'Будет создан новый чек-лист'}</strong><small class="muted">Прошлые оценки не изменятся</small></div><button type="submit" class="button primary" ${busy ? 'disabled' : ''}>${icon('check')} ${busy ? 'Сохранение…' : selected ? 'Сохранить новую версию' : 'Создать чек-лист'}</button></div>` : ''}
      </form>` : `<div class="panel empty"><h2>Чек-листов пока нет</h2><p class="muted">Добавьте критерии, их веса и правила проверки</p><button class="button primary" data-action="new">${icon('plus')} Создать чек-лист</button></div>`}</div></div></section>`;
    bind();
  }

  function capture() {
    const form = container.querySelector('#cl-form');
    if (!form || !draft || readonly()) return;
    const formData = new FormData(form);
    draft.name = String(formData.get('name') ?? draft.name);
    draft.global_rules = String(formData.get('global_rules') ?? draft.global_rules);
    draft.criteria.forEach((c, i) => {
      const prefix = `criterion-${i}`;
      for (const key of ['id', 'name', 'section', 'rule', 'call_rule', 'na_rule']) {
        if (formData.has(`${prefix}-${key}`)) c[key] = String(formData.get(`${prefix}-${key}`));
      }
      if (formData.has(`${prefix}-weight`)) c.weight = String(formData.get(`${prefix}-weight`));
      if (formData.has(`${prefix}-scale`)) c._scaleText = String(formData.get(`${prefix}-scale`));
      c.allow_na = formData.has(`${prefix}-allow_na`);
      c.type = String(formData.get(`${prefix}-type`) || c.type);
      if (c.type === 'flag') c.weight = 0;
    });
    openCriteria = new Set([...container.querySelectorAll('.cl-criterion[open]')].map(el => Number(el.dataset.index)));
  }

  function validate() {
    errors = [];
    const add = (fieldName, message) => errors.push({ field: fieldName, message });
    if (!draft.name.trim()) add('name', 'Укажите название чек-листа.');
    if (draft.name.length > 200) add('name', 'Название чек-листа: не более 200 символов.');
    if (draft.global_rules.length > 20000) add('global_rules', 'Общие правила: не более 20 000 символов.');
    const seen = new Set();
    const criteria = draft.criteria.map((c, index) => {
      const prefix = `criterion-${index}`;
      const n = index + 1;
      if (!/^[A-Za-z0-9_-]{1,100}$/.test(c.id)) add(`${prefix}-id`, `Критерий ${n}: ID должен содержать 1–100 латинских букв, цифр, дефисов или подчёркиваний.`);
      if (seen.has(c.id)) add(`${prefix}-id`, `Критерий ${n}: ID «${c.id}» уже используется.`);
      seen.add(c.id);
      if (!c.name.trim()) add(`${prefix}-name`, `Критерий ${n}: укажите название.`);
      if (!c.rule.trim()) add(`${prefix}-rule`, `Критерий ${n}: опишите правило проверки.`);
      const weight = c.type === 'flag' ? 0 : Number(c.weight);
      if (String(c.weight).trim() === '' || !Number.isFinite(weight) || weight < 0 || weight > 1000) add(`${prefix}-weight`, `Критерий ${n}: вес должен быть числом от 0 до 1000.`);
      let scale = [];
      if (c.type === 'scale') {
        const parts = String(c._scaleText ?? c.scale.join(',')).split(',').map(v => v.trim());
        scale = parts.map(Number);
        if (parts.some(p => !p) || scale.length < 2 || scale.length > 50 || scale.some(v => !Number.isFinite(v) || v < 0) || !scale.includes(0) || Math.max(...scale) <= 0 || new Set(scale).size !== scale.length) add(`${prefix}-scale`, `Критерий ${n}: укажите 2–50 уникальных неотрицательных значений, включая 0 и положительный максимум.`);
      }
      if (c.allow_na && !c.na_rule.trim()) add(`${prefix}-na_rule`, `Критерий ${n}: укажите условие для N/A.`);
      return { id: c.id, name: c.name, section: c.section, type: c.type, weight, rule: c.rule, call_rule: c.call_rule, scale, allow_na: c.allow_na, na_rule: c.na_rule };
    });
    return { name: draft.name, global_rules: draft.global_rules, criteria };
  }

  async function loadChecklist(id, version) {
    if (busy || disposed) return;
    const ticket = ++requestNumber;
    busy = true;
    container.setAttribute('aria-busy', 'true');
    container.querySelectorAll('input, select, textarea, button').forEach(control => { control.disabled = true; });
    const previousVersion = selected?.version;
    try {
      const value = await api(`/api/v1/checklists/${encodeURIComponent(id)}${version ? `/versions/${version}` : ''}`);
      if (disposed || ticket !== requestNumber) return;
      selected = value;
      draft = definitionOf(value);
      dirty = false;
      errors = [];
      serverError = '';
      conflict = false;
      openCriteria = new Set([0]);
    } catch (error) {
      if (!disposed) toast(error.message || 'Не удалось загрузить чек-лист.', 'error');
      const select = container.querySelector('#cl-version');
      if (select && previousVersion) select.value = String(previousVersion);
    } finally {
      if (!disposed && ticket === requestNumber) { busy = false; container.removeAttribute('aria-busy'); render(); }
    }
  }

  function startNew(copy = false) {
    if (busy) return;
    capture();
    if (!copy && !confirmDiscard()) return;
    const next = copy && draft ? clone(draft) : { name: '', global_rules: '', criteria: [newCriterion('criterion_1')] };
    if (copy) next.name = `${next.name} — копия`.slice(0, 200);
    selected = null;
    draft = next;
    dirty = true;
    errors = [];
    serverError = '';
    conflict = false;
    openCriteria = new Set([0]);
    render();
    container.querySelector('#cl-name')?.focus();
  }

  async function save(event) {
    event.preventDefault();
    if (busy || readonly()) return;
    capture();
    const definition = validate();
    if (errors.length) {
      render();
      container.querySelector('#cl-errors')?.focus();
      return;
    }
    busy = true;
    serverError = '';
    conflict = false;
    render();
    try {
      const saved = await api(selected ? `/api/v1/checklists/${encodeURIComponent(selected.checklist_id)}/versions` : '/api/v1/checklists', {
        method: 'POST',
        body: selected ? { expected_version: selected.version, definition } : definition,
      });
      if (disposed) return;
      selected = saved;
      draft = definitionOf(saved);
      dirty = false;
      const summary = { checklist_id: saved.checklist_id, name: saved.name, version: saved.version, created_at: saved.created_at };
      const at = items.findIndex(item => item.checklist_id === saved.checklist_id);
      if (at < 0) items.unshift(summary); else items[at] = summary;
      toast(`Чек-лист сохранён · версия ${saved.version}`);
      context.onSaved?.(saved);
    } catch (error) {
      if (disposed) return;
      conflict = error.code === 'VERSION_CONFLICT' || error.status === 409;
      serverError = conflict ? 'На сервере уже есть более новая версия. Автоматическое перезаписывание отключено.' : error.message || 'Проверьте соединение с сервером и попробуйте снова.';
    } finally {
      busy = false;
      if (!disposed) render();
    }
  }

  function bind() {
    const form = container.querySelector('#cl-form');
    form?.addEventListener('submit', save);
    form?.addEventListener('input', event => {
      if (!event.target.name || busy || readonly()) return;
      capture();
      dirty = true;
      const current = container.querySelector('.cl-unsaved');
      if (!current) container.querySelector('.cl-editor-top > div')?.insertAdjacentHTML('beforeend', '<span class="cl-unsaved">Есть изменения</span>');
      const node = event.target.closest('.cl-criterion');
      if (node && event.target.name.endsWith('-name')) node.querySelector('.cl-criterion-title strong').textContent = event.target.value || 'Новый критерий';
    });
    form?.addEventListener('change', event => {
      if (!event.target.name || busy || readonly()) return;
      if (event.target.name.endsWith('-type') || event.target.name.endsWith('-allow_na')) {
        const index = Number(event.target.name.split('-')[1]);
        capture();
        dirty = true;
        const changed = draft.criteria[index];
        if (changed?.type === 'scale' && !changed.scale.length && !changed._scaleText) changed._scaleText = '0, 5, 10';
        render();
        container.querySelector(`#cl-${event.target.name}`)?.focus();
      }
    });
    container.querySelector('#cl-version')?.addEventListener('change', event => {
      const nextVersion = Number(event.target.value);
      if (!confirmDiscard()) { event.target.value = String(selected.version); return; }
      loadChecklist(selected.checklist_id, nextVersion);
    });
    container.querySelectorAll('[data-error-target]').forEach(link => link.addEventListener('click', event => {
      event.preventDefault();
      const fieldNode = container.querySelector(`#${link.dataset.errorTarget}`);
      const details = fieldNode?.closest('details');
      if (details) details.open = true;
      fieldNode?.focus();
    }));
    container.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', async () => {
      if (busy) return;
      const action = button.dataset.action;
      if (action === 'new') startNew();
      if (action === 'copy') startNew(true);
      if (action === 'select' && confirmDiscard()) loadChecklist(button.dataset.id);
      if (action === 'reload' && confirmDiscard()) {
        try {
          const fresh = await api('/api/v1/checklists');
          if (disposed) return;
          items = fresh.items;
          loadChecklist(selected.checklist_id);
        } catch (error) { toast(error.message, 'error'); }
      }
      if (action === 'add') {
        capture();
        if (draft.criteria.length >= 40) return;
        let count = draft.criteria.length + 1;
        while (draft.criteria.some(c => c.id === `criterion_${count}`)) count++;
        draft.criteria.push(newCriterion(`criterion_${count}`));
        dirty = true;
        openCriteria.add(draft.criteria.length - 1);
        render();
        container.querySelector(`#cl-criterion-${draft.criteria.length - 1}-name`)?.focus();
      }
      if (action === 'remove') {
        capture();
        const index = Number(button.dataset.index);
        if (draft.criteria.length <= 1 || !window.confirm(`Удалить критерий «${draft.criteria[index].name || draft.criteria[index].id}» из черновика?`)) return;
        draft.criteria.splice(index, 1);
        openCriteria = new Set([...openCriteria].filter(v => v !== index).map(v => v > index ? v - 1 : v));
        errors = [];
        dirty = true;
        render();
      }
    }));
  }

  async function init() {
    container.innerHTML = '<div class="panel empty" role="status">Загружаем чек-листы…</div>';
    try {
      const result = await api('/api/v1/checklists');
      if (disposed) return;
      items = result.items;
      const initial = items.find(i => i.checklist_id === context.checklistId) || items[0];
      if (initial) await loadChecklist(initial.checklist_id);
      else render();
    } catch (error) {
      if (disposed) return;
      container.innerHTML = `<div class="panel empty"><h2>Не удалось загрузить чек-листы</h2><p class="muted">${esc(error.message)}</p><button class="button secondary" id="cl-retry">${icon('refresh')} Повторить</button></div>`;
      container.querySelector('#cl-retry')?.addEventListener('click', init);
    }
  }

  await init();
  const cleanup = () => { disposed = true; requestNumber++; window.removeEventListener('beforeunload', beforeUnload); };
  cleanup.canLeave = () => !busy && confirmDiscard();
  return cleanup;
}
