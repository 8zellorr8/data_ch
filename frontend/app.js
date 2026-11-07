import {api,getApiBase,setApiBase} from './api.js';
import {esc,icon,toast,dateText,scoreText,openModal,downloadJson} from './ui.js';

const state={datasets:[],datasetId:sessionStorage.getItem('cqa-dataset')||'',health:null,cleanup:null,route:'',token:0};
const app=document.getElementById('app');
app.innerHTML=`<div class="shell"><aside class="sidebar"><a class="brand" href="#dialogs" aria-label="Replimetr — диалоги"><div><div class="brand-word">Replimetr</div><div class="brand-sub">Контроль качества диалогов</div></div></a><div class="nav-label">Проверки</div><nav class="nav-links" aria-label="Основная навигация"><a class="nav-link" href="#dialogs" data-nav="dialogs">${icon('chat')}Диалоги</a><a class="nav-link" href="#checklists" data-nav="checklists">${icon('list')}Чек-листы</a><a class="nav-link" href="#appeals" data-nav="appeals">${icon('shield')}Апелляции</a></nav><div class="sidebar-footer"><p>Рабочее место эксперта</p><small>Чек-лист · цитата · решение</small></div></aside><div class="workspace"><header class="topbar"><div class="crumb"><span>Контроль качества</span>${icon('chevron')}<strong id="crumb-current">Диалоги</strong></div><div class="topbar-right"><span class="connection" id="connection"><span class="status-dot"></span>Проверяем сервер</span><button class="icon-btn" id="settings" aria-label="Настройки сервера" title="Настройки сервера">${icon('settings')}</button></div></header><main id="main" tabindex="-1"></main></div></div>`;
const main=document.getElementById('main');
const loading=()=>'<div class="loading" role="status"><span class="spinner"></span>Загружаем данные…</div>';
function setDataset(id){state.datasetId=id;sessionStorage.setItem('cqa-dataset',id);}
async function health(){
  const el=document.getElementById('connection');
  try{state.health=await api('/health');el.className='connection online';el.innerHTML='<span class="status-dot"></span>Сервер подключён';}
  catch{state.health=null;el.className='connection offline';el.innerHTML='<span class="status-dot"></span>Нет соединения';}
}
async function datasets(){
  state.datasets=(await api('/api/v1/datasets')).items;
  if(!state.datasets.some(x=>x.id===state.datasetId))setDataset(state.datasets[0]?.id||'');
  return state.datasets;
}
function datasetSelect(){return `<div class="dataset-field"><label for="dataset">Набор данных</label><select id="dataset">${state.datasets.map(x=>`<option value="${esc(x.id)}" ${x.id===state.datasetId?'selected':''}>${esc(x.name)} · ${esc(x.count)}</option>`).join('')}</select></div>`;}
function errorView(error){main.innerHTML=`<div class="page-heading"><div><h1>Не удалось загрузить данные</h1></div></div><div class="panel"><div class="notice error" role="alert">${esc(error.message)}</div><div class="toolbar" style="margin-top:20px"><button class="button primary" id="retry">${icon('refresh')}Повторить</button><button class="button secondary" id="open-settings">Настройки сервера</button></div></div>`;document.getElementById('retry').onclick=()=>route(true);document.getElementById('open-settings').onclick=settings;}
let reverting=false;
async function route(force=false){
  const next=location.hash||'#dialogs';
  if(reverting){reverting=false;return;}
  if(state.cleanup?.canLeave && !state.cleanup.canLeave()) {if(next!==state.route){reverting=true;location.hash=state.route||'#dialogs';}return;}
  state.cleanup?.();state.cleanup=null;state.route=next;
  const token=++state.token;
  const path=next.slice(1).split('/');const section=path[0]==='review'?'dialogs':path[0];
  document.querySelectorAll('[data-nav]').forEach(x=>{const active=x.dataset.nav===section;x.classList.toggle('active',active);if(active)x.setAttribute('aria-current','page');else x.removeAttribute('aria-current');});
  document.getElementById('crumb-current').textContent=({dialogs:'Диалоги',checklists:'Чек-листы',appeals:'Апелляции'})[section]||'Диалоги';
  main.innerHTML=loading();
  try{
    if(path[0]==='review' && path[1]){
      const {mountReview}=await import('./review.js');if(token!==state.token)return;
      const host=document.createElement('div');main.replaceChildren(host);
      const cleanup=await mountReview(host,{conversationId:decodeURIComponent(path[1]),evaluationId:path[2]?decodeURIComponent(path[2]):undefined,onBack:()=>{location.hash='#dialogs';},onChanged:()=>{}});
      if(token===state.token)state.cleanup=cleanup;else cleanup?.();
    }else if(path[0]==='checklists'){
      const {mountChecklists}=await import('./checklists.js');if(token!==state.token)return;
      const host=document.createElement('div');main.replaceChildren(host);
      const cleanup=await mountChecklists(host);if(token===state.token)state.cleanup=cleanup;else cleanup?.();
    }else if(path[0]==='appeals'){await renderAppeals(token);}
    else{await renderDialogs(token);}
  }catch(error){if(token===state.token)errorView(error);}
}

async function renderDialogs(token){
  await datasets();if(token!==state.token)return;
  const selected=state.datasets.find(d=>d.id===state.datasetId);
  const data=selected?await api(`/api/v1/datasets/${encodeURIComponent(selected.id)}/conversations?limit=1000`):{items:[]};
  if(token!==state.token)return;
  const conversations=data.items;
  const chats=conversations.filter(x=>x.channel==='agent chat').length;
  const languages=[...new Set(conversations.map(x=>x.language))].sort();
  main.innerHTML=`<div class="page-heading"><div><h1>Диалоги</h1><p>Оценки по чек-листам и история проверок</p></div><div class="heading-actions"><button class="button secondary" id="export" ${selected?'':'disabled'}>${icon('download')}Экспорт</button><button class="button ${selected?'primary':'secondary'}" id="import">${icon('plus')}Загрузить диалоги</button></div></div><div class="stats-strip"><div class="stat"><div class="stat-head">Всего диалогов</div><div class="stat-value">${conversations.length}</div><div class="stat-caption">В выбранном наборе</div></div><div class="stat"><div class="stat-head">Чаты</div><div class="stat-value">${chats}</div></div><div class="stat"><div class="stat-head">Звонки</div><div class="stat-value">${conversations.length-chats}</div></div><div class="stat"><div class="stat-head">Языки</div><div class="stat-value languages">${languages.join(' / ')||'—'}</div></div></div>${selected?`<div class="dataset-toolbar">${datasetSelect()}<button class="button ghost" id="refresh-list">${icon('refresh')}Обновить</button></div><div class="table-panel"><div class="table-tools"><div class="search-field">${icon('search')}<input id="search" type="search" aria-label="Поиск по ID диалога" placeholder="Поиск по ID"></div><div class="filter-group"><select id="language" aria-label="Фильтр по языку"><option value="">Все языки</option><option>RU</option><option>KZ</option><option>EN</option></select><select id="channel" aria-label="Фильтр по каналу"><option value="">Все каналы</option><option value="agent chat">Чаты</option><option value="voice call">Звонки</option></select></div></div><div id="table-content"></div></div>`:`<div class="table-panel empty"><div class="empty-symbol">${icon('chat')}</div><h2>Диалогов пока нет</h2><p>Загрузите JSON с диалогами или откройте учебный пример</p><button class="button primary" id="empty-import">${icon('upload')}Загрузить JSON</button> <button class="button secondary" id="demo">Открыть пример</button></div>`}<div class="ai-note">${icon('shield')}<div><strong>${state.health?.ai_configured?'Модель подключена.':'AI не подключён.'}</strong> ${state.health?.ai_configured?'Откройте диалог и выберите версию чек-листа для оценки.':'Доступна ручная оценка с цитатами.'}</div></div>`;
  document.getElementById('import').onclick=importModal;
  document.getElementById('export').onclick=()=>exportDataset();
  if(!selected){document.getElementById('empty-import').onclick=importModal;document.getElementById('demo').onclick=importDemo;return;}
  document.getElementById('dataset').onchange=e=>{setDataset(e.target.value);route(true);};
  document.getElementById('refresh-list').onclick=()=>{health();route(true);};
  let page=0,renderVersion=0;const scores=new Map();
  function table(){
    const stamp=++renderVersion;const q=document.getElementById('search').value.toLocaleLowerCase();
    const lang=document.getElementById('language').value;const channel=document.getElementById('channel').value;
    const items=conversations.filter(x=>(!lang||x.language===lang)&&(!channel||x.channel===channel)&&x.conversation_id.toLocaleLowerCase().includes(q));
    page=Math.min(page,Math.max(0,Math.ceil(items.length/15)-1));const visible=items.slice(page*15,(page+1)*15);
    const host=document.getElementById('table-content');
    host.innerHTML=visible.length?`<div class="table-wrap"><table><thead><tr><th scope="col">Диалог</th><th scope="col">Канал</th><th scope="col">Язык</th><th scope="col">Сообщения</th><th scope="col">Последняя оценка</th><th scope="col"><span class="muted">Открыть</span></th></tr></thead><tbody>${visible.map(x=>`<tr><td class="id-cell"><a class="conversation-link" href="#review/${encodeURIComponent(x.id)}">${esc(x.conversation_id)}</a><div class="row-meta">${esc(selected.group_id)} · ${x.channel==='agent chat'?'Переписка с клиентом':'Транскрипт звонка'}</div></td><td><span class="channel">${icon(x.channel==='agent chat'?'chat':'phone')}${x.channel==='agent chat'?'Чат':'Звонок'}</span></td><td><span class="language-pill">${esc(x.language)}</span></td><td>${x.message_count}</td><td data-score="${esc(x.id)}"><span class="muted small-text">Загружаем…</span></td><td><a class="icon-btn" href="#review/${encodeURIComponent(x.id)}" aria-label="Открыть диалог ${esc(x.conversation_id)}">${icon('arrow')}</a></td></tr>`).join('')}</tbody></table></div><div class="table-foot"><span>${page*15+1}–${Math.min((page+1)*15,items.length)} из ${items.length} диалогов</span><div class="pagination"><button class="button small" id="prev" ${page===0?'disabled':''} aria-label="Предыдущая страница">←</button><button class="button small" id="next" ${(page+1)*15>=items.length?'disabled':''} aria-label="Следующая страница">→</button></div></div>`:'<div class="empty"><h3>Диалоги не найдены</h3><p>Попробуйте другой ID или измените фильтры.</p></div>';
    if(visible.length){document.getElementById('prev').onclick=()=>{page--;table();};document.getElementById('next').onclick=()=>{page++;table();};}
    let cursor=0;
    async function worker(){
      while(cursor<visible.length){const x=visible[cursor++];if(token!==state.token||stamp!==renderVersion)return;
        if(!scores.has(x.id)){try{scores.set(x.id,(await api(`/api/v1/conversations/${encodeURIComponent(x.id)}/evaluations?limit=1`)).items[0]||null);}catch{scores.set(x.id,'error');}}
        if(token!==state.token||stamp!==renderVersion)return;
        const cell=host.querySelector(`[data-score="${CSS.escape(x.id)}"]`);const score=scores.get(x.id);
        if(cell)cell.innerHTML=score==='error'?'<span class="badge amber">Не загружена</span>':score?`<span class="score-inline"><span class="score-number">${scoreText(score.score)}</span><span class="badge ${score.origin==='manual'?'':'green'}">${score.origin==='manual'?'Вручную':'AI'}</span></span>`:'<span class="badge">Не оценён</span>';
      }
    }
    Promise.all([worker(),worker(),worker()]);
  }
  for(const id of ['search','language','channel'])document.getElementById(id).addEventListener(id==='search'?'input':'change',()=>{page=0;table();});
  table();
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
function importModal(){
  const modal=openModal('Загрузить диалоги',`<div class="import-options"><p class="muted">Создайте отдельный набор для проверки. Максимум 1000 диалогов, размер файла — до 8 МБ.</p><form id="import-file-form" class="stack"><div class="form-grid"><div class="field"><label for="dataset-name">Название набора</label><input id="dataset-name" name="name" maxlength="200" required placeholder="Например, DEV · ноябрь"></div><div class="field"><label for="group-id">Группа</label><input id="group-id" name="group" maxlength="100" value="DEV" required></div></div><label class="file-drop" for="json-file">${icon('upload')}<span>Выберите JSON с диалогами</span><input id="json-file" name="file" type="file" accept=".json,application/json" required><span class="muted small-text">Поддерживается формат backend и ответ API организаторов.</span></label><div class="form-error" role="alert" id="import-error"></div><button class="button primary" type="submit">${icon('upload')}Импортировать файл</button></form><div class="separator">или</div><button class="button secondary" id="organizer-import">Загрузить DEV с сервера организаторов</button><p class="muted small-text">Ссылка организаторов должна быть настроена участником backend на сервере. Вставлять её сюда не нужно.</p></div>`);
  let busy=false;
  modal.el.addEventListener('cancel',e=>{if(busy)e.preventDefault();});
  const errorHost=modal.body.querySelector('#import-error');
  const controls=()=>modal.body.querySelectorAll('button,input');
  async function save(payload){
    const result=await api('/api/v1/datasets',{method:'POST',body:payload});setDataset(result.id);modal.close();toast(`Загружено диалогов: ${result.count}`);await route(true);
  }
  modal.body.querySelector('form').onsubmit=async e=>{
    e.preventDefault();if(busy)return;errorHost.textContent='';const form=e.currentTarget;const file=form.elements.file.files[0];
    busy=true;controls().forEach(x=>x.disabled=true);
    try{
      if(file.size>8*1024*1024)throw new Error('Файл превышает 8 МБ.');
      let raw;try{raw=JSON.parse((await file.text()).replace(/^\uFEFF/,''));}catch{throw new Error('Не удалось прочитать JSON. Проверьте формат и кодировку UTF-8.');}
      const records=Array.isArray(raw)?raw:raw?.isSuccess===true?raw.data?.records:raw?.records;
      if(!Array.isArray(records)||!records.length)throw new Error('В файле не найден непустой массив records с диалогами.');
      if(records.length>1000)throw new Error('В одном наборе допустимо не более 1000 диалогов.');
      await save({name:form.elements.name.value.trim(),group_id:form.elements.group.value.trim(),records});
    }catch(error){errorHost.textContent=error.message+(error.details?.length?` ${error.details.slice(0,3).map(x=>`${x.location.join('.')}: ${x.message}`).join('; ')}`:'');}
    finally{busy=false;controls().forEach(x=>x.disabled=false);}
  };
  modal.body.querySelector('#json-file').onchange=e=>{const name=modal.body.querySelector('#dataset-name');if(!name.value&&e.target.files[0])name.value=e.target.files[0].name.replace(/\.json$/i,'').slice(0,200);};
  modal.body.querySelector('#organizer-import').onclick=async()=>{
    if(busy)return;busy=true;controls().forEach(x=>x.disabled=true);errorHost.textContent='';
    try{const result=await api('/api/v1/datasets/import-organizer',{method:'POST',body:{group_id:'DEV',name:'DEV · организаторы'}});setDataset(result.id);modal.close();toast(`Загружено диалогов: ${result.count}`);await route(true);}
    catch(error){errorHost.textContent=error.message;}finally{busy=false;controls().forEach(x=>x.disabled=false);}
  };
}

async function renderAppeals(token){
  await datasets();if(token!==state.token)return;
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
  const modal=openModal('Подключение к серверу',`<form class="stack"><div class="notice">Для совместной работы вся команда использует один backend и одну базу.</div><div class="field"><label for="api-url">Адрес backend</label><input id="api-url" type="url" value="${esc(getApiBase())}" required><small>Для локального запуска: http://127.0.0.1:8000</small></div><div class="form-error" role="alert"></div><div class="modal-actions"><button type="submit" class="button primary">Сохранить и проверить</button></div></form>`);
  modal.body.querySelector('form').onsubmit=async e=>{e.preventDefault();if(state.cleanup?.canLeave&&!state.cleanup.canLeave())return;try{setApiBase(modal.body.querySelector('input').value.trim());modal.close();await health();state.cleanup?.();state.cleanup=null;setDataset('');if(location.hash.startsWith('#review'))location.hash='#dialogs';else await route(true);}catch(error){modal.body.querySelector('.form-error').textContent=error.message;}};
}
document.getElementById('settings').onclick=settings;
document.querySelector('.skip-link').onclick=event=>{event.preventDefault();main.focus();};
window.addEventListener('hashchange',()=>route());
health().then(()=>route());
