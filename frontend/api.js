export const HOSTED_API = 'https://replimetr.alwaysdata.net';
export function defaultApiBase(location = globalThis.location) {
  return ['https:', 'http:'].includes(location?.protocol) ? location.origin : HOSTED_API;
}
export function getApiBase() { try { return localStorage.getItem('cqa-api-base') || defaultApiBase(); } catch { return defaultApiBase(); } }
export function setApiBase(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Укажите HTTP(S) адрес сервера без пароля, параметров и подписи.');
  localStorage.setItem('cqa-api-base', url.href.replace(/\/+$/, ''));
}
const messages = {
  AI_NOT_CONFIGURED: 'Модель пока не подключена. Вы можете выполнить ручную оценку.',
  AI_UNAVAILABLE: 'Модель недоступна. Оценка не сохранена.',
  AI_INVALID_RESULT: 'Ответ модели не прошёл проверку критериев или цитат. Оценка не сохранена.',
  AI_TIMEOUT: 'Время ожидания модели истекло. Проверьте историю перед повторным запуском.',
  ORGANIZER_NOT_CONFIGURED: 'Импорт организаторов не настроен на сервере. Загрузите JSON-файл или попросите backend-участника настроить CQA_ORGANIZER_API_URL.',
  ORGANIZER_CONFIG_ERROR: 'Адрес организаторов на сервере настроен некорректно.',
  ORGANIZER_UNAVAILABLE: 'Сервис организаторов недоступен. Попробуйте позже или загрузите JSON.',
  ORGANIZER_INVALID_RESPONSE: 'Сервис организаторов вернул данные в неизвестном формате.',
  VERSION_CONFLICT: 'Чек-лист уже изменён другим участником. Сначала загрузите последнюю версию.',
  REVISION_CONFLICT: 'Оценка уже изменена другим участником. Обновите её перед повторным решением.',
  APPEAL_EXISTS: 'По этому критерию уже есть открытая апелляция.',
  APPEAL_CLOSED: 'Эта апелляция уже рассмотрена. Обновите оценку.',
  INVALID_EVIDENCE: 'Проверьте цитату: она должна точно совпадать с фрагментом выбранного сообщения.',
  INVALID_ANSWER: 'Значение ответа не соответствует выбранному критерию.',
  INVALID_RESULT: 'Проверьте ответы по всем критериям, пояснения и цитаты.',
  VALIDATION_ERROR: 'Проверьте заполнение полей и формат данных.',
  BODY_TOO_LARGE: 'Размер запроса превышает 8 МБ.',
  DATASET_TOO_LARGE: 'Набор слишком большой: максимум 1000 диалогов и 8 МБ.',
  NOT_FOUND: 'Запись не найдена. Возможно, выбран другой сервер или база.',
  AI_BUSY: 'Все слоты модели заняты. Повторите этот запрос позже.',
  BATCH_QUEUE_FULL: 'Очередь сервера заполнена. Попробуйте позже.',
  IDEMPOTENCY_CONFLICT: 'Этот ключ уже относится к другому запросу. Восстановите исходную операцию.',
  EVALUATION_IN_PROGRESS: 'Запрос ещё выполняется на сервере. Повторите получение результата позже.',
  EVALUATION_FAILED: 'Предыдущий запуск завершился ошибкой. Проверьте историю и расход перед новой оценкой.',
  EVALUATION_INTERRUPTED: 'Запуск был прерван на сервере. Проверьте историю и расход перед новой оценкой.',
};
// Store an absolute deadline so a reload or an idempotent replay cannot restart
// the wait. This is UI feedback only; the backend remains the rate-limit owner.
export function retryAfterDeadline(value, now = Date.now()) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const raw = value.trim();
  let deadline;
  if (/^\d+$/.test(raw)) deadline = now + Number(raw) * 1000;
  else if (/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} [A-Za-z]{3} \d{4} \d{2}:\d{2}:\d{2} GMT$/.test(raw)) deadline = Date.parse(raw);
  else return null;
  return Number.isFinite(deadline) && deadline >= 0 && deadline <= 8.64e15 ? deadline : null;
}
export async function api(path, options = {}) {
  const {body, base = getApiBase(), timeoutMs = 30000, ...rest} = options;
  const controller = new AbortController();
  const abort = () => controller.abort(rest.signal?.reason);
  rest.signal?.addEventListener('abort', abort, {once:true});
  if (rest.signal?.aborted) abort();
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  let response, data;
  try {
    response = await fetch(`${base}${path}`, {
      ...rest, signal: controller.signal, headers: { ...(body !== undefined ? {'Content-Type':'application/json'} : {}), ...rest.headers },
      ...(body !== undefined ? {body: JSON.stringify(body)} : {}),
    });
    try { data = await response.json(); }
    catch (cause) {
      if (timedOut || cause.name === 'AbortError') throw cause;
      const error = new Error('Не удалось прочитать JSON-ответ сервера. При сохранении восстановите результат с прежним ключом.');
      error.code = 'INVALID_RESPONSE'; throw error;
    }
  } catch (cause) {
    if (timedOut) { const error = new Error('Время ожидания истекло. Запрос может продолжаться на сервере; восстановите результат с сохранённым ключом.'); error.code = 'CLIENT_TIMEOUT'; throw error; }
    if (cause.name === 'AbortError') throw cause;
    if (cause.code === 'INVALID_RESPONSE') throw cause;
    const error = new Error('Не удалось связаться с сервером. Проверьте, что backend запущен и адрес сервера указан правильно. Если ошибка возникла при сохранении, обновите историю перед повтором.');
    error.code = 'NETWORK_ERROR'; throw error;
  } finally { clearTimeout(timer); rest.signal?.removeEventListener('abort', abort); }
  if (!response.ok) {
    const detail = data.error || {};
    const error = new Error(messages[detail.code] || detail.message || `Ошибка сервера (${response.status})`);
    error.code = detail.code || 'HTTP_ERROR'; error.status = response.status; error.details = detail.details;
    error.requestId = response.headers.get('X-Request-ID'); error.executionId = response.headers.get('X-Execution-ID');
    error.retryUntil = retryAfterDeadline(response.headers.get('Retry-After'));
    if (error.retryUntil !== null && error.code === 'AI_UNAVAILABLE') error.message = 'Сервис модели запросил паузу. Повторное обращение возможно не раньше указанного времени.';
    throw error;
  }
  return data;
}
