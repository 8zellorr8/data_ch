import {api,getApiBase,setApiBase,defaultApiBase} from './api.js';
import {esc,icon,toast,dateText,scoreText,openModal,downloadJson} from './ui.js';

const state={datasets:[],datasetId:sessionStorage.getItem('cqa-dataset')||'',health:null,cleanup:null,route:'',token:0};
if (new URLSearchParams(location.search).get('server') === 'team') {
  setApiBase(defaultApiBase());
  const url = new URL(location.href); url.searchParams.delete('server'); history.replaceState(null,'',url);
}
const app=document.getElementById('app');
app.innerHTML=`<div class="shell"><aside class="sidebar"><a class="brand" href="#dialogs" aria-label="Replimetr — диалоги"><div><div class="brand-word">Replimetr</div><div class="brand-sub">Контроль качества диалогов</div></div></a><button class="button secondary navigation-toggle" id="navigation-toggle" type="button" aria-controls="main-navigation" aria-expanded="false">${icon('menu')}<span>Разделы</span></button><div class="nav-label">Проверки</div><nav id="main-navigation" class="nav-links" aria-label="Основная навигация"><a class="nav-link" href="#dialogs" data-nav="dialogs">${icon('chat')}Диалоги</a><a class="nav-link" href="#checklists" data-nav="checklists">${icon('list')}Чек-листы</a><a class="nav-link" href="#appeals" data-nav="appeals">${icon('shield')}Апелляции</a><a class="nav-link" href="#batches" data-nav="batches">${icon('clock')}Пакеты проверок</a><a class="nav-link" href="#reports" data-nav="reports">${icon('chart')}Отчёты</a><a class="nav-link" href="#submission" data-nav="submission">${icon('download')}Сдача результатов</a></nav><div class="sidebar-footer"><p>Рабочее место эксперта</p><small>Чек-лист · цитата · решение</small></div></aside><div class="workspace"><header class="topbar"><div class="crumb"><span>Контроль качества</span>${icon('chevron')}<strong id="crumb-current">Диалоги</strong></div><div class="topbar-right"><span class="connection" id="connection"><span class="status-dot"></span>Проверяем сервер</span><button class="icon-btn" id="settings" aria-label="Настройки сервера" title="Настройки сервера">${icon('settings')}</button></div></header><main id="main" tabindex="-1"></main></div></div>`;
const main=document.getElementById('main');
const sidebar=document.querySelector('.sidebar');
const navigationToggle=document.getElementById('navigation-toggle');
const compactNavigation=matchMedia('(max-width:800px)');
function setNavigation(open) {
  sidebar.dataset.navigationOpen=String(open);
  navigationToggle.setAttribute('aria-expanded',String(open));
}
setNavigation(false);
navigationToggle.onclick=()=>setNavigation(navigationToggle.getAttribute('aria-expanded')!=='true');
sidebar.addEventListener('keydown',event=>{if(event.key==='Escape'&&compactNavigation.matches&&sidebar.dataset.navigationOpen==='true'){setNavigation(false);navigationToggle.focus();}});
sidebar.addEventListener('click',event=>{
  if(event.target.closest('.nav-link')&&compactNavigation.matches){
    setNavigation(false);
    // Keep focus visible even if a dirty form blocks navigation or the current
    // section is selected again. A successful route then focuses its heading.
    navigationToggle.focus({preventScroll:true});
  }
});
compactNavigation.addEventListener('change',()=>{
  const focusInNavigation=document.getElementById('main-navigation').contains(document.activeElement);
  const focusOnToggle=document.activeElement===navigationToggle;
  setNavigation(false);
  if(compactNavigation.matches&&focusInNavigation)navigationToggle.focus();
  if(!compactNavigation.matches&&focusOnToggle)document.querySelector('.nav-link.active')?.focus();
});

const loading=()=>'<div class="loading" role="status"><span class="spinner"></span>Загружаем данные…</div>';
function setDataset(id){state.datasetId=id;sessionStorage.setItem('cqa-dataset',id);}
async function health() {
  const el = document.getElementById('connection'), base = getApiBase();
  try {
    const result = await api('/health');
    if (base !== getApiBase()) return;
    state.health = result; el.className = 'connection online'; el.innerHTML = '<span class="status-dot"></span>Сервер подключён';
  } catch {
    if (base !== getApiBase()) return;
    state.health = null; el.className = 'connection offline'; el.innerHTML = '<span class="status-dot"></span>Нет соединения';
  }
}
async function datasets(token = state.token) {
  const base = getApiBase();
  const result = await api('/api/v1/datasets');
  if (token !== state.token || base !== getApiBase()) return;
  state.datasets = result.items;
  if (!state.datasets.some(item => item.id === state.datasetId)) setDataset(state.datasets[0]?.id || '');
  return state.datasets;
}
function datasetSelect(){return `<div class="dataset-field"><label for="dataset">Набор данных</label><select id="dataset">${state.datasets.map(x=>`<option value="${esc(x.id)}" ${x.id===state.datasetId?'selected':''}>${esc(x.name)} · ${esc(x.count)}</option>`).join('')}</select></div>`;}
function errorView(error){main.innerHTML=`<div class="page-heading"><div><h1>Не удалось загрузить данные</h1></div></div><div class="panel"><div class="notice error" role="alert">${esc(error.message)}</div><div class="toolbar" style="margin-top:20px"><button class="button primary" id="retry">${icon('refresh')}Повторить</button><button class="button secondary" id="open-settings">Настройки сервера</button></div></div>`;document.getElementById('retry').onclick=()=>route(true);document.getElementById('open-settings').onclick=settings;}
let reverting=false;
async function route(force=false){
  const next=location.hash||'#dialogs';
  if(reverting){reverting=false;return;}
  if(state.cleanup?.canLeave && !state.cleanup.canLeave()) {if(next!==state.route){reverting=true;location.hash=state.route||'#dialogs';}return;}
  const navigated=next!==state.route;
  state.cleanup?.();state.cleanup=null;state.route=next;
  const token=++state.token;
  const [routePath,query='']=next.slice(1).split('?');
  const params=new URLSearchParams(query);
  const path=routePath.split('/');const section=path[0]==='review'?'dialogs':path[0];
  document.querySelectorAll('[data-nav]').forEach(x=>{const active=x.dataset.nav===section;x.classList.toggle('active',active);if(active)x.setAttribute('aria-current','page');else x.removeAttribute('aria-current');});
  document.getElementById('crumb-current').textContent=({dialogs:'Диалоги',checklists:'Чек-листы',appeals:'Апелляции',batches:'Пакеты проверок',reports:'Отчёты',submission:'Сдача результатов'})[section]||'Диалоги';
  main.setAttribute('aria-busy','true'); main.innerHTML=loading();
  try{
    if(path[0]==='review' && path[1]){
      const {mountReview}=await import('./review.js');if(token!==state.token)return;
      const host=document.createElement('div');main.replaceChildren(host);
      const conversationId=decodeURIComponent(path[1]);
      const cleanup=await mountReview(host,{conversationId,evaluationId:path[2]?decodeURIComponent(path[2]):undefined,checklistId:params.get('checklist')||undefined,checklistVersion:Number(params.get('version'))||undefined,newEvaluation:params.get('new')==='1',onBack:()=>{location.hash='#dialogs';},onChanged:()=>{},onSelectionChanged:selection=>{if(token!==state.token||!host.isConnected)return;const query=new URLSearchParams(location.hash.split('?')[1]||'');query.set('checklist',selection.checklistId);query.set('version',String(selection.version));const next=location.hash.split('?')[0]+'?'+query;history.replaceState(null,'',next);state.route=next;},onEditChecklist:context=>{const query=new URLSearchParams({checklist:context.checklistId,version:String(context.checklistVersion),conversation:context.conversationId});location.hash=`#checklists?${query}`;}});
      if(token===state.token)state.cleanup=cleanup;else cleanup?.();
    }else if(path[0]==='checklists'){
      const {mountChecklists}=await import('./checklists.js');if(token!==state.token)return;
      const host=document.createElement('div');main.replaceChildren(host);
      const returnConversationId=params.get('conversation')||undefined;
      const cleanup=await mountChecklists(host,{initialChecklistId:params.get('checklist')||undefined,initialVersion:Number(params.get('version'))||undefined,returnConversationId,onSelectionChanged:selection=>{if(token!==state.token||!host.isConnected)return;const query=new URLSearchParams(location.hash.split('?')[1]||'');if(selection.checklistId){query.set('checklist',selection.checklistId);query.set('version',String(selection.version));}else{query.delete('checklist');query.delete('version');}const next='#checklists'+(query.size?'?'+query:'');history.replaceState(null,'',next);state.route=next;},onApplyToConversation:rules=>{if(!returnConversationId)return;const query=new URLSearchParams({checklist:rules.checklist_id,version:String(rules.version),new:'1'});location.hash=`#review/${encodeURIComponent(returnConversationId)}?${query}`;}});if(token===state.token)state.cleanup=cleanup;else cleanup?.();
    }else if(['batches','reports','submission'].includes(path[0])){
      const section=path[0];
      if(section!=='batches') await datasets(token);
      if(token!==state.token)return;
      const module=await import(section==='batches'?'./batch.js':section==='reports'?'./reports.js':'./submission.js');
      if(token!==state.token)return;
      const host=document.createElement('div');main.replaceChildren(host);
      const mount=section==='batches'?module.mountBatches:section==='reports'?module.mountReports:module.mountSubmission;
      const cleanup=await mount(host,{datasets:state.datasets,datasetId:state.datasetId,batchId:path[1]?decodeURIComponent(path[1]):params.get('batch'),executionId:params.get('execution'),onDatasetChange:id=>{if(token!==state.token)return;setDataset(id);route(true);}});
      if(token===state.token)state.cleanup=cleanup;else cleanup?.();
    }else if(path[0]==='appeals'){await renderAppeals(token);}
    else{await renderDialogs(token);}
  }catch(error){if(token===state.token)errorView(error);}
  finally {
    if(token===state.token){
      main.removeAttribute('aria-busy');
      const heading=main.querySelector('h1');
      document.title=(heading?.textContent||document.getElementById('crumb-current').textContent)+' — Replimetr';
      if(navigated||force){
        const target=heading||main;target.setAttribute('tabindex','-1');target.focus({preventScroll:true});
        if(navigated)window.scrollTo({top:0,behavior:'instant'});
      }
    }
  }
}

async function renderDialogs(token) {
  await datasets(token);
  if (token !== state.token) return;
  const { mountDialogs } = await import('./dialogs.js');
  if (token !== state.token) return;
  const host = document.createElement('div'); main.replaceChildren(host);
  const cleanup = await mountDialogs(host, {
    datasets: state.datasets, datasetId: state.datasetId, aiConfigured: state.health?.ai_configured === true,
    onDatasetChange: id => { setDataset(id); route(true); },
    onImport: importModal, onExport: exportDataset, onDemo: importDemo,
    onRefresh: async () => { await health(); route(true); },
  });
  if (token === state.token) state.cleanup = cleanup; else cleanup?.();
}

async function exportDataset(){
  const button=document.getElementById('export');if(button)button.disabled=true;
  try{const result=await api(`/api/v1/datasets/${encodeURIComponent(state.datasetId)}/export`);downloadJson(result,`replimetr-export-${new Date().toISOString().slice(0,10)}.json`);toast('Экспорт скачан: диалоги, оценки и апелляции.');}
  catch(error){toast(error.message,'error');}finally{if(button)button.disabled=false;}
}
async function importDemo(){
  const button=document.getElementById('demo');if(button)button.disabled=true;
  try{const response=await fetch('./demo-dataset.json');const payload=await response.json();const result=await api('/api/v1/datasets',{method:'POST',body:payload});setDataset(result.id);toast('Учебный пример загружен. Это искусственные диалоги.');await route(true);}
  catch(error){toast(error.message,'error');if(button)button.disabled=false;}
}
function importModal() {
  const modal = openModal('Загрузить диалоги',
    '<form id="import-form" class="stack" novalidate>' +
    '<fieldset class="import-modes"><legend>Источник диалогов</legend><label><input type="radio" name="source" value="file" checked> JSON-файл</label><label><input type="radio" name="source" value="organizer"> Сервер организаторов</label></fieldset>' +
    '<div class="form-grid"><div class="field"><label for="dataset-name">Название набора</label><input id="dataset-name" name="name" maxlength="200" required placeholder="Например, контрольный набор"></div>' +
    '<div class="field"><label for="group-id">Группа набора</label><input id="group-id" name="group" maxlength="100" value="DEV" required aria-describedby="group-help"><small id="group-help">Для контрольного набора введите group_id организаторов</small></div></div>' +
    '<div data-import-file><label class="file-drop" for="json-file">' + icon('upload') + '<span>Выберите JSON с диалогами</span><input id="json-file" name="file" type="file" accept=".json,application/json" required><span class="muted small-text">До 1000 диалогов, файл до 8 МБ</span></label></div>' +
    '<p data-import-organizer class="import-help" hidden>Диалоги будут загружены для указанной группы. Адрес организаторов настраивается на сервере.</p>' +
    '<div class="form-error" role="alert" id="import-error" tabindex="-1"></div>' +
    '<button class="button primary" type="submit" id="import-submit">' + icon('upload') + 'Импортировать файл</button></form>');
  const form = modal.body.querySelector('form');
  const errorHost = form.querySelector('#import-error');
  const submit = form.querySelector('#import-submit');
  let busy = false;
  modal.el.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  const showError = error => { errorHost.textContent = error.message || error; errorHost.focus(); };
  function sourceChanged() {
    const remote = form.elements.source.value === 'organizer';
    form.querySelector('[data-import-file]').hidden = remote;
    form.querySelector('[data-import-organizer]').hidden = !remote;
    form.elements.file.required = !remote;
    form.elements.name.required = !remote;
    submit.innerHTML = icon(remote ? 'download' : 'upload') + (remote ? 'Загрузить группу' : 'Импортировать файл');
    errorHost.textContent = '';
  }
  form.querySelectorAll('[name="source"]').forEach(input => input.addEventListener('change', sourceChanged));
  form.elements.file.onchange = event => {
    if (!form.elements.name.value && event.target.files[0]) form.elements.name.value = event.target.files[0].name.replace(/\.json$/i, '').slice(0, 200);
  };
  form.onsubmit = async event => {
    event.preventDefault();
    if (busy) return;
    errorHost.textContent = '';
    const source = form.elements.source.value;
    const group_id = form.elements.group.value.trim();
    const name = form.elements.name.value.trim();
    if (!group_id) { showError('Введите группу набора, например DEV или выданный контрольный group_id.'); form.elements.group.focus(); return; }
    if (source === 'file' && !name) { showError('Введите название набора.'); form.elements.name.focus(); return; }
    if (!form.reportValidity()) return;
    const file = form.elements.file.files[0];
    busy = true; form.setAttribute('aria-busy', 'true');
    form.querySelectorAll('button,input').forEach(input => input.disabled = true);
    submit.textContent = 'Загружаем диалоги…';
    try {
      let result;
      if (source === 'organizer') {
        result = await api('/api/v1/datasets/import-organizer', {method: 'POST', body: {group_id, name: name || group_id + ' · организаторы'}});
      } else {
        if (!file) throw new Error('Выберите JSON-файл.');
        if (file.size > 8 * 1024 * 1024) throw new Error('Файл превышает 8 МБ.');
        let raw;
        try { raw = JSON.parse((await file.text()).replace(/^\uFEFF/, '')); }
        catch { throw new Error('Не удалось прочитать JSON. Проверьте формат и кодировку UTF-8.'); }
        const records = Array.isArray(raw) ? raw : raw?.isSuccess === true ? raw.data?.records : raw?.records;
        if (!Array.isArray(records) || !records.length) throw new Error('В файле не найден непустой массив records с диалогами.');
        if (records.length > 1000) throw new Error('В одном наборе допустимо не более 1000 диалогов.');
        result = await api('/api/v1/datasets', {method: 'POST', body: {name, group_id, records}});
      }
      setDataset(result.id); modal.close(); toast('Загружено диалогов: ' + result.count); await route(true);
    } catch (error) {
      showError(error.message + (error.details?.length ? ' ' + error.details.slice(0, 3).map(item => item.location.join('.') + ': ' + item.message).join('; ') : ''));
    } finally {
      busy = false; form.removeAttribute('aria-busy');
      form.querySelectorAll('button,input').forEach(input => input.disabled = false);
      const previousError = errorHost.textContent; sourceChanged(); errorHost.textContent = previousError;
    }
  };
}

async function renderAppeals(token){
  await datasets(token);if(token!==state.token)return;
  main.innerHTML=`<div class="page-heading"><div><h1>Апелляции</h1><p class="route-description">Аргументы автора и решения по спорным критериям</p></div><button class="button secondary" id="refresh-appeals">${icon('refresh')}Обновить</button></div>${state.datasets.length?`<div class="dataset-toolbar">${datasetSelect()}<span class="muted small-text">Все оценки выбранного набора</span></div>`:''}<div id="queue">${loading()}</div>`;
  document.getElementById('refresh-appeals').onclick=()=>route(true);
  if(state.datasets.length)document.getElementById('dataset').onchange=e=>{setDataset(e.target.value);route(true);};
  if(!state.datasetId){document.getElementById('queue').innerHTML='<div class="panel empty"><h2>Пока нет диалогов</h2><p>Загрузите набор и выполните первую оценку.</p><a href="#dialogs" class="button primary">К диалогам</a></div>';return;}
  // Backend has no global appeal endpoint. One export is a coherent snapshot of this dataset.
  const snapshot=await api(`/api/v1/datasets/${encodeURIComponent(state.datasetId)}/export`);if(token!==state.token)return;
  const appeals=snapshot.evaluations.flatMap(e=>(e.appeals||[]).map(a=>({appeal:a,evaluation:e})));
  appeals.sort((a,b)=>Number(a.appeal.status!=='open')-Number(b.appeal.status!=='open')||b.appeal.created_at.localeCompare(a.appeal.created_at));
  document.getElementById('queue').innerHTML=appeals.length?`<div class="section-heading"><h2>Обращения <span class="badge">${appeals.length}</span></h2><span class="muted small-text">Открыто: ${appeals.filter(x=>x.appeal.status==='open').length}</span></div><div class="table-panel">${appeals.map(({appeal:a,evaluation:e})=>`<article class="queue-row"><div class="queue-main"><div class="toolbar"><span class="badge ${a.status==='open'?'amber':'green'}">${a.status==='open'?'На рассмотрении':'Рассмотрена'}</span><span class="muted small-text">${esc(e.conversation.conversation_id)} · ${esc(dateText(a.created_at))}</span></div><h3>${esc(e.checklist.criteria.find(c=>c.id===a.criterion_id)?.name||a.criterion_id)}</h3><p>${esc(a.comment)}</p><span class="muted small-text">Автор: ${esc(a.author)}</span></div><a class="button secondary" href="#review/${encodeURIComponent(e.conversation_id)}/${encodeURIComponent(e.id)}">${a.status==='open'?'Рассмотреть':'Открыть решение'}${icon('arrow')}</a></article>`).join('')}</div>`:'<div class="panel empty"><div class="empty-symbol">'+icon('shield')+'</div><h2>Апелляций пока нет</h2><p>Здесь появятся обращения по критериям из выбранного набора</p><a class="button secondary" href="#dialogs">Перейти к диалогам</a></div>';
}
function settings(){
  const modal=openModal('Подключение к серверу',`<form class="stack"><div class="notice">Для совместной работы вся команда использует один backend и одну базу.</div><div class="field"><label for="api-url">Адрес backend</label><input id="api-url" type="url" value="${esc(getApiBase())}" required><small>Для локального запуска: http://127.0.0.1:8000</small></div><div class="toolbar"><button class="button secondary" type="button" data-api-preset="hosted">Сервер команды</button><button class="button ghost" type="button" data-api-preset="local">Локальный сервер</button></div><div class="form-error" role="alert"></div><div class="modal-actions"><button type="submit" class="button primary">Сохранить и проверить</button></div></form>`);
  modal.body.querySelectorAll('[data-api-preset]').forEach(button=>button.onclick=()=>{modal.body.querySelector('input').value=button.dataset.apiPreset==='hosted'?defaultApiBase():'http://127.0.0.1:8000';});
  modal.body.querySelector('form').onsubmit=async e=>{e.preventDefault();if(state.cleanup?.canLeave&&!state.cleanup.canLeave())return;try{setApiBase(modal.body.querySelector('input').value.trim());modal.close();const settingsToken=++state.token;state.cleanup?.();state.cleanup=null;state.health=null;setDataset('');main.innerHTML=loading();await health();if(settingsToken!==state.token)return;if(location.hash!=='#dialogs')location.hash='#dialogs';else await route(true);}catch(error){modal.body.querySelector('.form-error').textContent=error.message;}};
}
document.getElementById('settings').onclick=settings;
document.querySelector('.skip-link').onclick=event=>{event.preventDefault();main.focus();};
window.addEventListener('hashchange',()=>route());
window.addEventListener('storage', async event => {
  if (event.key !== 'cqa-api-base') return;
  const token = ++state.token;
  state.cleanup?.(); state.cleanup = null; state.health = null; setDataset('');
  main.innerHTML = loading();
  await health();
  if (token !== state.token) return;
  toast('Сервер изменён в другой вкладке. Данные загружены заново.');
  if (location.hash !== '#dialogs') location.hash = '#dialogs'; else await route(true);
});
health().then(()=>route());
