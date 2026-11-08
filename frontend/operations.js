import {api, getApiBase} from './api.js';

const prefix = 'ketnavr-operation-v1:';
const canonical = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item) ? Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
const copy = value => JSON.parse(JSON.stringify(value));
const fault = (message, code) => Object.assign(new Error(message), {code});

// Persist the exact body and key before any POST. A lost response must never
// silently become a new model invocation, including after a page reload.
export function createOperationStore({storage, getBase = getApiBase, request = api, uuid = () => crypto.randomUUID()} = {}) {
  const getStorage = () => storage || globalThis.sessionStorage;
  const storageKey = (scope, base = getBase()) => prefix + encodeURIComponent(base) + ':' + encodeURIComponent(scope);
  function write(op) {
    try { getStorage().setItem(storageKey(op.scope, op.base), JSON.stringify(op)); }
    catch { throw fault('Не удалось сохранить ключ запроса в браузере. Запрос не отправлен. Разрешите хранилище и повторите.', 'OPERATION_STORAGE_ERROR'); }
  }
  function findOperation(scope) {
    let raw;
    try { raw = getStorage().getItem(storageKey(scope)); }
    catch { throw fault('Хранилище запросов недоступно. Разрешите его перед отправкой.', 'OPERATION_STORAGE_ERROR'); }
    if (!raw) return null;
    try {
      const op = JSON.parse(raw);
      if (op.scope !== scope || op.base !== getBase() || !op.key || !op.path || !op.body) throw new Error();
      return op;
    } catch { throw fault('Сохранённый запрос повреждён. Проверьте историю перед очисткой хранилища браузера.', 'OPERATION_STORAGE_ERROR'); }
  }
  function listOperations(scopePrefix = '') {
    const result = [], store = getStorage(), start = prefix + encodeURIComponent(getBase()) + ':';
    for (let i = 0; i < store.length; i++) {
      const key = store.key(i);
      if (key?.startsWith(start)) {
        const scope = decodeURIComponent(key.slice(start.length));
        if (scope.startsWith(scopePrefix)) result.push(findOperation(scope));
      }
    }
    return result;
  }
  function prepareOperation(scope, path, body) {
    const old = findOperation(scope);
    if (old) {
      if (old.path === path && canonical(old.body) === canonical(body)) return old;
      throw fault('Есть незавершённый запрос с другими данными. Сначала восстановите его результат на странице диалога.', 'OPERATION_PENDING');
    }
    const op = {scope, path, body:copy(body), key:uuid(), base:getBase(), createdAt:new Date().toISOString(), status:'pending'};
    write(op); return copy(op);
  }
  function clearOperation(op) {
    const key = storageKey(op.scope, op.base);
    const current = getStorage().getItem(key);
    if (current && JSON.parse(current).key === op.key) getStorage().removeItem(key);
  }
  async function sendOperation(op) {
    if (op.base !== getBase()) throw fault('Адрес сервера изменился. Вернитесь к исходному серверу для восстановления запроса.', 'SERVER_CHANGED');
    const stored = findOperation(op.scope);
    if (!stored || stored.key !== op.key || stored.path !== op.path || canonical(stored.body) !== canonical(op.body)) throw fault('Сохранённый запрос изменился. Обновите страницу.', 'OPERATION_PENDING');
    const pending = {...stored, status:'pending'};
    write(pending);
    try {
      const result = await request(op.path, {method:'POST', base:op.base, body:copy(op.body), headers:{'Idempotency-Key':op.key}, timeoutMs:185000});
      // Keep the receipt until the caller reads the current server record.
      try { write({...pending, status:'succeeded', resultId:result.id, error:undefined}); } catch { /* Original key is already durable. */ }
      return result;
    } catch (error) {
      const retryUntil = Math.max(error.retryUntil || 0, pending.error?.retryUntil || 0) || null;
      if (retryUntil !== null) error.retryUntil = retryUntil;
      try { write({...pending, status:'error', error:{code:error.code, message:error.message, status:error.status, executionId:error.executionId || pending.error?.executionId, requestId:error.requestId || pending.error?.requestId, retryUntil}}); } catch { /* Keep original key for recovery. */ }
      throw error;
    }
  }
  return {findOperation, listOperations, prepareOperation, sendOperation, clearOperation};
}
const operations = createOperationStore();
export const {findOperation, listOperations, prepareOperation, sendOperation, clearOperation} = operations;
