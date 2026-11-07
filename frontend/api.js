const DEFAULT_BASE = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)
  ? 'http://127.0.0.1:8000' : location.origin;
export function getApiBase() { return localStorage.getItem('cqa-api-base') || DEFAULT_BASE; }
export function setApiBase(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Укажите HTTP(S) адрес сервера без пароля, параметров и подписи.');
  localStorage.setItem('cqa-api-base', url.href.replace(/\/+$/, ''));
}
const messages = {
  AI_NOT_CONFIGURED: 'Модель пока не подключена. Вы можете выполнить ручную оценку.',
  AI_UNAVAILABLE: 'Модель недоступна. Оценка не сохранена.',
  AI_INVALID_RESULT: 'Ответ модели не прошёл проверку критериев или цитат. Оценка не сохранена.',
  AI_TIMEOUT: 'Модель не ответила за 3 минуты. Оценка не сохранена.',
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
};
export async function api(path, options = {}) {
  const {body, ...rest} = options;
  let response;
  try {
    response = await fetch(`${getApiBase()}${path}`, {
      ...rest, headers: { ...(body !== undefined ? {'Content-Type':'application/json'} : {}), ...rest.headers },
      ...(body !== undefined ? {body: JSON.stringify(body)} : {}),
    });
  } catch (cause) {
    if (cause.name === 'AbortError') throw cause;
    const error = new Error('Не удалось связаться с сервером. Проверьте, что backend запущен и адрес сервера указан правильно. Если ошибка возникла при сохранении, обновите историю перед повтором.');
    error.code = 'NETWORK_ERROR'; throw error;
  }
  let data;
  try { data = await response.json(); } catch { throw new Error('Сервер вернул ответ не в формате JSON. Проверьте адрес сервера.'); }
  if (!response.ok) {
    const detail = data.error || {};
    const error = new Error(messages[detail.code] || detail.message || `Ошибка сервера (${response.status})`);
    error.code = detail.code || 'HTTP_ERROR'; error.status = response.status; error.details = detail.details;
    throw error;
  }
  return data;
}
