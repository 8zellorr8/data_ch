import { getApiBase } from './api.js';

const PREFIX = 'ketnavr:draft:v1:';
const MAX_BYTES = 2 * 1024 * 1024;

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

// A change detector, not a security signature. Never persists the source text.
export function draftFingerprint(value) {
  const text = stable(value);
  let hash = 14695981039346656037n;
  for (let index = 0; index < text.length; index++) {
    hash = BigInt.asUintN(64, (hash ^ BigInt(text.charCodeAt(index))) * 1099511628211n);
  }
  return `${text.length}:${hash.toString(16)}`;
}

function key(scope) {
  if (!scope.kind || !scope.entity) throw new Error('Не задана привязка черновика.');
  const server = (scope.server || getApiBase()).replace(/\/+$/, '');
  return PREFIX + [server, scope.kind, scope.entity].map(encodeURIComponent).join(':');
}

export function readDraft(scope) {
  let raw;
  try { raw = sessionStorage.getItem(key(scope)); }
  catch { throw new Error('Браузер не разрешает читать черновики. Не закрывайте форму до сохранения.'); }
  if (!raw) return null;
  let saved;
  try { saved = JSON.parse(raw); }
  catch { throw new Error('Не удалось прочитать сохранённый черновик.'); }
  if (saved.schema !== 1 || !saved.data || typeof saved.data !== 'object') {
    throw new Error('Формат сохранённого черновика не поддерживается.');
  }
  return {
    ...saved,
    compatible: String(saved.version) === String(scope.version) && saved.fingerprint === scope.fingerprint,
  };
}

export function writeDraft(scope, data) {
  const raw = JSON.stringify({schema: 1, version: scope.version, fingerprint: scope.fingerprint, updatedAt: new Date().toISOString(), data});
  if (new Blob([raw]).size > MAX_BYTES) throw new Error('Черновик слишком большой для хранения в браузере. Сохраните форму на сервере.');
  try { sessionStorage.setItem(key(scope), raw); }
  catch { throw new Error('Не удалось сохранить черновик в браузере. Не закрывайте форму до сохранения на сервере.'); }
  return true;
}

export function removeDraft(scope) {
  try { sessionStorage.removeItem(key(scope)); }
  catch { throw new Error('Браузер не разрешает удалить черновик.'); }
}
