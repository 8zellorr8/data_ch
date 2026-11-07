export const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const shapes = {
  menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',
  chart:'<path d="M4 3v17h17M8 15v-4m5 4V6m5 9V9"/>',
  chat:'<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8z"/><path d="M8 10h8M8 14h5"/>',
  phone:'<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6 19.8 19.8 0 0 1-3.1-8.7A2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.4 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/>',
  list:'<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/>',
  shield:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 16v4h16v-4"/>',
  download:'<path d="M12 3v13m-5-5 5 5 5-5M4 16v4h16v-4"/>',
  plus:'<path d="M12 5v14M5 12h14"/>', close:'<path d="m6 6 12 12M6 18 18 6"/>',
  check:'<path d="m5 12 4 4L19 6"/>', arrow:'<path d="M5 12h14m-6-6 6 6-6 6"/>',
  chevron:'<path d="m9 5 7 7-7 7"/>', search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  refresh:'<path d="M20 7v5h-5M4 17v-5h5M6.1 7a7 7 0 0 1 11.6-2L20 8M4 16l2.3 3A7 7 0 0 0 18 17"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  edit:'<path d="m16 3 5 5L9 20H4v-5L16 3ZM13 6l5 5"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  file:'<path d="M14 2H5v20h14V7l-5-5Zm0 0v6h5M8 12h8M8 16h6"/>',
  alert:'<path d="m12 3 10 18H2L12 3ZM12 9v5M12 17h.01"/>',
  settings:'<path d="M12 3v3m0 12v3M3 12h3m12 0h3M5.6 5.6l2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1m-8.6 8.6-2.1 2.1"/><circle cx="12" cy="12" r="5"/>',
};
export function icon(name) { return `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${shapes[name] || shapes.list}</svg>`; }
export function dateText(value) { const date = new Date(value); return Number.isNaN(+date) ? String(value || '—') : new Intl.DateTimeFormat('ru-RU', {day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}).format(date); }
export function scoreText(value) { return value === null || value === undefined ? '—' : new Intl.NumberFormat('ru-RU',{maximumFractionDigits:2}).format(value); }
let toastTimer;
export function toast(message, type='success') {
  const region = document.getElementById('toast-region');
  const previousFocus = document.activeElement;
  clearTimeout(toastTimer);
  region.setAttribute('role', type === 'error' ? 'alert' : 'status');
  region.setAttribute('aria-live', type === 'error' ? 'assertive' : 'polite');
  region.className = `toast ${type}`;
  region.innerHTML = `${icon(type === 'error' ? 'alert':'check')}<span>${esc(message)}</span><button type="button" class="icon-btn toast-dismiss" aria-label="Закрыть уведомление">${icon('close')}</button>`;
  const dismiss = () => {
    const returnFocus = region.contains(document.activeElement);
    clearTimeout(toastTimer); region.className=''; region.replaceChildren();
    region.onmouseenter = region.onmouseleave = region.onfocusin = region.onfocusout = null;
    if(returnFocus) (previousFocus?.isConnected && !region.contains(previousFocus) ? previousFocus : document.getElementById('main'))?.focus({preventScroll:true});
  };
  region.querySelector('button').onclick = dismiss;
  // Give success messages a fresh reading interval after pointer or keyboard
  // attention. Errors remain visible until explicitly dismissed.
  const scheduleDismiss = () => {
    clearTimeout(toastTimer);
    if(type !== 'error') toastTimer = setTimeout(() => {
      if(!region.matches(':hover') && !region.contains(document.activeElement)) dismiss();
    },6000);
  };
  region.onmouseenter = region.onfocusin = () => clearTimeout(toastTimer);
  region.onmouseleave = region.onfocusout = scheduleDismiss;
  scheduleDismiss();
}
export function openModal(title, html, {wide=false}={}) {
  const previousFocus = document.activeElement;
  const el = document.createElement('dialog'); el.className = `modal ${wide?'wide':''}`;
  const titleId = `dialog-${Math.random().toString(36).slice(2)}`;
  el.setAttribute('aria-labelledby', titleId);
  el.innerHTML = `<div class="modal-header"><h2 id="${titleId}">${esc(title)}</h2><button class="icon-btn" type="button" aria-label="Закрыть" data-modal-close>${icon('close')}</button></div><div class="modal-body">${html}</div>`;
  const close = () => {
    el.close();el.remove();
    if(previousFocus?.isConnected) previousFocus.focus({preventScroll:true});
    requestAnimationFrame(() => {
      if(document.activeElement===document.body){
        const target=previousFocus?.isConnected?previousFocus:document.querySelector('#main h1')||document.getElementById('main');
        if(target){if(!target.matches('button,a,input,select,textarea'))target.setAttribute('tabindex','-1');target.focus({preventScroll:true});}
      }
    });
  };
  el.querySelector('[data-modal-close]').addEventListener('click', () => {
    if (el.dispatchEvent(new Event('cancel', {cancelable:true}))) close();
  });
  // Native events may run a microtask checkpoint between listeners. Wait until
  // all feature-specific cancel guards have had a chance to prevent closing.
  el.addEventListener('cancel', event => {
    setTimeout(() => { if (!event.defaultPrevented && el.isConnected) close(); }, 0);
  });
  document.body.append(el); el.showModal();
  return {el,body:el.querySelector('.modal-body'),close};
}
export function downloadJson(data, name) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'}));
  const a = document.createElement('a'); a.href=url; a.download=name; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
}
