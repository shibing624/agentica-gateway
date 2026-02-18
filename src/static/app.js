// ============ STATE ============
const API = '';
let curSess = null;
let sessions = {};
let streaming = false;
let abortCtrl = null;
let pendingFiles = [];
let serverModel = '-';
let serverDir = '';
let serverProvider = '';
let serverModelName = '';
let serverVersion = '';
let modelsData = null;
let userScrolledUp = false;

const TOOL_ICONS = {
  ls:'📁', read_file:'📖', write_file:'✏️', edit_file:'✂️', multi_edit_file:'✂️',
  glob:'🔍', grep:'🔎', execute:'⚡', web_search:'🌐',
  fetch_url:'🔗', write_todos:'📋', read_todos:'📋',
  task:'🤖', save_memory:'💾', default:'🔧',
};
function toolIcon(name){return TOOL_ICONS[name]||TOOL_ICONS.default}
// Display name mapping for tools
const TOOL_DISPLAY={'write_todos':'todos','read_todos':'todos'};
function toolDisplay(name){return TOOL_DISPLAY[name]||name}
// Determine section CSS class based on tool type
function toolSecClass(name){
  if(name==='write_todos'||name==='read_todos') return 'sec-todos';
  if(name==='task') return 'sec-task';
  return 'sec-tools';
}
// How many tool rows to show before collapsing (for history)
const TOOL_VISIBLE_LIMIT=3;

// Render a single tool row (flat, minimal style)
function renderToolRow(st, idx){
  const icon=toolIcon(st.name||'');
  const dname=toolDisplay(st.name||'tool');
  const argsHtml=isRichTool(st.name)?fmtToolArgsHtml(st.name,st.rawArgs,st.argsStr):esc(st.argsStr||'');
  const hasResult=!!st.result;
  const isTodo=(st.name==='write_todos'||st.name==='read_todos');
  const isTaskTool=(st.name==='task');
  const todoBody=isTodo?fmtTodoBodyHtml(st.rawArgs):'';
  const taskBody=isTaskTool?fmtTaskBodyHtml(st.result):'';
  const hasExtra=hasResult||todoBody||taskBody;
  let h=`<div class="tg-row${hasExtra?' has-result':''}"${hasExtra?` onclick="toggleToolResult(this)"`:''}>`
  if(hasExtra) h+=`<span class="tg-arrow">&#x25B8;</span>`;
  h+=`<span class="tg-icon">${icon}</span><span class="tg-name">${esc(dname)}</span><span class="tg-args">${argsHtml}</span></div>`;
  if(hasExtra){
    let body='';
    if(todoBody) body+=todoBody;
    if(taskBody) body+=taskBody;
    if(hasResult&&!taskBody) body+=esc(st.result);
    h+=`<div class="tg-result">${body}</div>`;
  }
  return h;
}

// Smart format tool args for display
function fmtToolArgs(name, args){
  if(!args||typeof args!=='object') return args?String(args):'';
  try{
    switch(name){
      case 'read_file':{
        const f=args.file_path||args.file||args.path||args.filename||'';
        const short=shortenFilePath(f);
        const parts=[short];
        if(args.offset!=null||args.limit!=null){
          const s=(args.offset||0)+1, e=args.limit?(args.offset||0)+args.limit:'';
          parts.push(`(L${s}${e?'-'+e:''})`);
        } else if(args.start_line!=null||args.end_line!=null){
          const s=args.start_line||1, e=args.end_line||'';
          parts.push(`(L${s}${e?'-'+e:''})`);
        }
        return parts.join(' ');
      }
      case 'write_file':{
        const f=args.file_path||args.file||args.path||args.filename||'';
        const short=shortenFilePath(f);
        const lines=args._lines||0;
        return short+(lines?' +'+lines:'');
      }
      case 'edit_file':{
        const f=args.file_path||args.file||args.path||args.filename||'';
        const short=shortenFilePath(f);
        const add=args._diff_add||0, del=args._diff_del||0;
        const diff=(add||del)?` +${add} -${del}`:'';
        return short+diff;
      }
      case 'execute':{
        return args.command||args.cmd||JSON.stringify(args).slice(0,200);
      }
      case 'ls':{
        return args.directory||args.path||args.dir||'.';
      }
      case 'glob':{
        const p=args.pattern||'';const d=args.directory||args.path||'';
        return p+(d?' in '+d:'');
      }
      case 'grep':{
        const p=args.pattern||'';const d=args.directory||args.path||'';
        return '"'+p+'"'+(d?' in '+d:'');
      }
      case 'web_search':{
        return args.query||args.q||JSON.stringify(args).slice(0,150);
      }
      case 'fetch_url':{
        return args.url||JSON.stringify(args).slice(0,200);
      }
      case 'task':{
        return args.description||args.task||JSON.stringify(args).slice(0,150);
      }
      case 'write_todos':case 'read_todos':{
        return fmtTodoArgs(args);
      }
      case 'save_memory':{
        const c=args.content||args.text||'';
        return c.length>100?c.slice(0,100)+'…':c;
      }
      default:{
        const s=JSON.stringify(args);
        return s.length>200?s.slice(0,200)+'…':s;
      }
    }
  }catch{return JSON.stringify(args).slice(0,200)}
}

// Generate HTML body for todo list (used in sec-body)
function fmtTodoBodyHtml(args){
  const todos=args&&(args.todos||args.items);
  if(!Array.isArray(todos)||!todos.length) return '';
  return '<div class="todo-list">'+todos.map(t=>{
    const st=t.status==='completed'?'✅':t.status==='in_progress'?'🔄':t.status==='cancelled'?'❌':'⬜';
    return `<div class="todo-row"><span class="todo-st">${st}</span><span class="todo-txt">${esc(t.content||t.text||'')}</span></div>`;
  }).join('')+'</div>';
}

// Generate HTML body for task (subagent) result — shows inner tool calls + execution summary
function fmtTaskBodyHtml(resultStr){
  if(!resultStr) return '';
  let data;
  try{ data=JSON.parse(resultStr); }catch{ return ''; }
  if(!data||!data._task_meta) return '';
  if(!data.success){
    return `<div class="task-inner"><div class="task-inner-row"><span class="ti-info" style="color:var(--fg)">⚠ ${esc(data.error||'Unknown error')}</span></div></div>`;
  }
  const summary=data.tool_calls_summary||[];
  const maxShown=8;
  let h='<div class="task-inner">';
  for(let i=0;i<Math.min(summary.length,maxShown);i++){
    const tc=summary[i];
    const icon=toolIcon(tc.name||'');
    const info=tc.info||'';
    const shortInfo=info.length>90?info.slice(0,87)+'…':info;
    h+=`<div class="task-inner-row"><span class="ti-icon">${icon}</span><span class="ti-name">${esc(tc.name||'')}</span><span class="ti-info">${esc(shortInfo)}</span></div>`;
  }
  if(summary.length>maxShown){
    h+=`<div class="task-inner-more">… and ${summary.length-maxShown} more tool calls</div>`;
  }
  // Execution summary
  const parts=[];
  if(data.tool_count>0) parts.push(`${data.tool_count} tool uses`);
  if(data.execution_time!=null) parts.push(`cost: ${data.execution_time.toFixed(1)}s`);
  if(parts.length) h+=`<div class="task-summary">Execution Summary: ${parts.join(', ')}</div>`;
  h+='</div>';
  return h;
}

// Shorten file path for display: keep filename + parent dir
function shortenFilePath(p){
  if(!p)return '';
  const parts=p.replace(/\\/g,'/').split('/').filter(Boolean);
  if(parts.length<=2) return parts.join('/');
  return '…/'+parts.slice(-2).join('/');
}

// Tools that need HTML-rendered args (not just plain text escape)
const RICH_TOOLS=new Set(['task','write_todos','read_todos','read_file','write_file','edit_file','multi_edit_file']);
function isRichTool(name){return RICH_TOOLS.has(name)}

// Format todo/task args as readable text
function fmtTodoArgs(args){
  if(!args) return '';
  // write_todos: {todos: [{id,content,status},...]}
  const todos=args.todos||args.items;
  if(Array.isArray(todos)){
    return todos.map(t=>{
      const st=t.status==='completed'?'✅':t.status==='in_progress'?'🔄':t.status==='cancelled'?'❌':'⬜';
      return `${st} ${t.content||t.text||''}`;
    }).join(' | ');
  }
  const s=JSON.stringify(args);
  return s.length>200?s.slice(0,200)+'…':s;
}

// Rich HTML format for tool args (task, todo, file tools)
function fmtToolArgsHtml(name, args, argsStr){
  if(!args||typeof args!=='object') return esc(argsStr||'');
  // File tools: read_file, write_file, edit_file
  if(name==='read_file'){
    const f=args.file_path||args.file||args.path||args.filename||'';
    const short=shortenFilePath(f);
    let h=`<span class="file-path" title="${esc(f)}">${esc(short)}</span>`;
    if(args.offset!=null||args.limit!=null){
      const s=(args.offset||0)+1, e=args.limit?(args.offset||0)+args.limit:'';
      h+=` <span class="line-range">L${s}${e?'-'+e:''}</span>`;
    } else if(args.start_line!=null||args.end_line!=null){
      const s=args.start_line||1, e=args.end_line||'';
      h+=` <span class="line-range">L${s}${e?'-'+e:''}</span>`;
    }
    return h;
  }
  if(name==='write_file'){
    const f=args.file_path||args.file||args.path||args.filename||'';
    const short=shortenFilePath(f);
    const lines=args._lines||0;
    let h=`<span class="file-path" title="${esc(f)}">${esc(short)}</span>`;
    if(lines) h+=` <span class="diff-add">+${lines}</span>`;
    return h;
  }
  if(name==='edit_file'){
    const f=args.file_path||args.file||args.path||args.filename||'';
    const short=shortenFilePath(f);
    const add=args._diff_add||0, del=args._diff_del||0;
    let h=`<span class="file-path" title="${esc(f)}">${esc(short)}</span>`;
    if(add||del) h+=` <span class="diff-add">+${add}</span> <span class="diff-del">-${del}</span>`;
    return h;
  }
  if(name==='multi_edit_file'){
    const f=args.file_path||args.file||args.path||args.filename||'';
    const short=shortenFilePath(f);
    const add=args._diff_add||0, del=args._diff_del||0, cnt=args._edit_count||0;
    let h=`<span class="file-path" title="${esc(f)}">${esc(short)}</span>`;
    if(cnt) h+=` <span style="opacity:.6">${cnt} edits</span>`;
    if(add||del) h+=` <span class="diff-add">+${add}</span> <span class="diff-del">-${del}</span>`;
    return h;
  }
  if(name==='task'){
    const desc=args.description||args.task||'';
    const prompt=args.prompt||'';
    let h=`<span style="font-weight:600">${esc(desc)}</span>`;
    if(prompt){
      const short=prompt.length>120?prompt.slice(0,120)+'…':prompt;
      h+=`<br><span style="opacity:.6;font-size:10px">${esc(short)}</span>`;
    }
    return h;
  }
  if(name==='write_todos'||name==='read_todos'){
    const todos=args.todos||args.items;
    if(Array.isArray(todos)){
      const icons=todos.map((t,i)=>{
        const st=t.status==='completed'?'✅':t.status==='in_progress'?'🔄':t.status==='cancelled'?'❌':'⬜';
        return `${i+1} ${st}`;
      });
      return `<span style="font-size:11px">${todos.length} items · ${icons.join(' ')}</span>`;
    }
  }
  return esc(argsStr||'');
}

// ============ UUID ============
function uid(){
  if(crypto && crypto.randomUUID) return 'w:'+crypto.randomUUID().replace(/-/g,'').slice(0,12);
  const a=new Uint8Array(8);crypto.getRandomValues(a);
  return 'w:'+Array.from(a,b=>b.toString(16).padStart(2,'0')).join('').slice(0,12);
}

// ============ THEME ============
function getTheme(){return localStorage.getItem('ag_theme')||'auto'}
function applyTheme(t){
  const d=document.documentElement;
  if(t==='dark')d.setAttribute('data-theme','dark');
  else if(t==='light')d.removeAttribute('data-theme');
  else{if(matchMedia('(prefers-color-scheme:dark)').matches)d.setAttribute('data-theme','dark');else d.removeAttribute('data-theme')}
  document.getElementById('themeBtn').innerHTML=d.hasAttribute('data-theme')?'&#x2600;':'&#x263E;';
}
function toggleTheme(){
  const c=getTheme();let n;
  if(c==='auto')n=matchMedia('(prefers-color-scheme:dark)').matches?'light':'dark';
  else n=c==='dark'?'light':'dark';
  localStorage.setItem('ag_theme',n);applyTheme(n);
}

// ============ INIT ============
document.addEventListener('DOMContentLoaded',()=>{
  applyTheme(getTheme());
  loadSessions();
  loadStatus();
  loadModels();
  setupDragDrop();
  document.getElementById('inputTa').focus();
  matchMedia('(prefers-color-scheme:dark)').addEventListener('change',()=>{if(getTheme()==='auto')applyTheme('auto')});
  renderChat();
  document.addEventListener('click',e=>{
    if(!document.getElementById('modelWrap').contains(e.target)){
      document.getElementById('modelDD').classList.remove('open');
    }
  });
  // scroll button visibility + track user scroll intent
  const chatArea=document.getElementById('chatArea');
  chatArea.addEventListener('scroll',()=>{
    updateScrollBtn();
    // If user scrolled near bottom, re-enable auto-scroll
    if(isNearBottom()) userScrolledUp=false;
  });
  // Detect user-initiated scroll (wheel / touch)
  chatArea.addEventListener('wheel',()=>{if(streaming && !isNearBottom()) userScrolledUp=true},{passive:true});
  chatArea.addEventListener('touchmove',()=>{if(streaming && !isNearBottom()) userScrolledUp=true},{passive:true});
});

async function loadStatus(){
  try{
    const r=await fetch(`${API}/api/status`);const d=await r.json();
    serverModel=d.model||'-';
    serverDir=d.base_dir||d.workspace||'';
    serverProvider=d.model_provider||'';
    serverModelName=d.model_name||'';
    serverVersion=d.version||'';
    document.getElementById('modelLabel').textContent=serverModelName||serverModel;
    if(serverVersion) document.getElementById('verLabel').textContent='v'+serverVersion;
    updateDirDisplay();
  }catch{}
}

async function loadModels(){
  try{
    const r=await fetch(`${API}/api/models`);
    modelsData=await r.json();
    renderModelDD();
  }catch{}
}

function updateDirDisplay(){
  if(serverDir){
    const p=serverDir.split('/').filter(Boolean);
    const short=p.length>3?'…/'+p.slice(-2).join('/'):serverDir;
    document.getElementById('dirVal').textContent=short;
    document.getElementById('dirWrap').title='Working Directory: '+serverDir+' (click to edit)';
    document.getElementById('dirEditInput').value=serverDir;
  }
}

// ============ MODEL DROPDOWN ============
function toggleModelDD(){
  document.getElementById('modelDD').classList.toggle('open');
}

function renderModelDD(){
  if(!modelsData)return;
  const dd=document.getElementById('modelDD');
  let h='';
  for(const [prov, models] of Object.entries(modelsData.providers)){
    h+=`<div class="dd-group"><div class="dd-group-title">${esc(prov)}</div>`;
    for(const m of models){
      const isCur=(prov===modelsData.current_provider && m===modelsData.current_name);
      h+=`<div class="dd-item${isCur?' current':''}" onclick="switchModel('${esc(prov)}','${esc(m)}')">
        <span>${esc(m)}</span>
      </div>`;
    }
    h+='</div>';
  }
  dd.innerHTML=h;
}

async function switchModel(provider, name){
  document.getElementById('modelDD').classList.remove('open');
  if(provider===serverProvider && name===serverModelName) return;
  document.getElementById('modelLabel').textContent=name+' ⏳';
  try{
    const r=await fetch(`${API}/api/model`,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model_provider:provider,model_name:name}),
    });
    const d=await r.json();
    if(d.status==='ok'){
      serverProvider=provider;
      serverModelName=name;
      serverModel=d.model;
      document.getElementById('modelLabel').textContent=name;
      if(modelsData){modelsData.current_provider=provider;modelsData.current_name=name;renderModelDD()}
    }
  }catch(e){
    document.getElementById('modelLabel').textContent=serverModelName||'-';
  }
}

// ============ TOAST ============
let toastTimer=null;
function showToast(msg, duration=2500){
  const el=document.getElementById('toast');
  el.textContent=msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>el.classList.remove('show'), duration);
}

// ============ WORKING DIR ============
let dirHistory=[];

function openDirModal(){
  const overlay=document.getElementById('dirModalOverlay');
  overlay.classList.add('open');
  const inp=document.getElementById('dirEditInput');
  inp.value=serverDir;
  closeDirHistoryDD();
  loadDirHistory();
  setTimeout(()=>inp.focus(),50);
}

function closeDirModal(e){
  if(e&&e.target!==e.currentTarget)return;
  closeDirHistoryDD();
  document.getElementById('dirModalOverlay').classList.remove('open');
}

async function loadDirHistory(){
  try{
    const r=await fetch(`${API}/api/config/dir_history`);
    const d=await r.json();
    dirHistory=d.history||[];
  }catch{ dirHistory=[]; }
}

function shortenPath(p){
  const home=p.startsWith('/Users/')?p.split('/').slice(0,3).join('/'):null;
  if(home&&p.startsWith(home)){
    const rel=p.slice(home.length);
    if(!rel||rel==='/') return '~';
    const parts=rel.split('/').filter(Boolean);
    if(parts.length<=2) return '~/'+parts.join('/');
    return '~/…/'+parts.slice(-2).join('/');
  }
  return p;
}

function renderDirHistory(filter){
  const dd=document.getElementById('dirHistoryDD');
  const q=(filter||'').toLowerCase();
  const filtered=q?dirHistory.filter(p=>p.toLowerCase().includes(q)):dirHistory;
  if(!filtered.length){
    dd.innerHTML='<div class="dir-history-empty">无历史记录</div>';
    return;
  }
  dd.innerHTML=filtered.map(p=>`<div class="dir-history-item" onclick="selectDirHistory('${p.replace(/'/g,"\\'")}')">
    <div class="dh-short">${esc(shortenPath(p))}</div>
    <div class="dh-full">${esc(p)}</div>
  </div>`).join('');
}

function toggleDirHistory(){
  const dd=document.getElementById('dirHistoryDD');
  const btn=document.getElementById('dirToggleBtn');
  const isOpen=dd.classList.toggle('open');
  btn.classList.toggle('open',isOpen);
  if(isOpen) renderDirHistory();
}

function closeDirHistoryDD(){
  document.getElementById('dirHistoryDD').classList.remove('open');
  document.getElementById('dirToggleBtn').classList.remove('open');
}

function filterDirHistory(){
  const dd=document.getElementById('dirHistoryDD');
  if(dd.classList.contains('open')){
    renderDirHistory(document.getElementById('dirEditInput').value);
  }
}

function selectDirHistory(path){
  document.getElementById('dirEditInput').value=path;
  closeDirHistoryDD();
  document.getElementById('dirEditInput').focus();
}

async function saveDir(){
  const inp=document.getElementById('dirEditInput');
  const val=inp.value.trim();
  if(!val)return;
  try{
    const r=await fetch(`${API}/api/config/base_dir`,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({base_dir:val}),
    });
    if(!r.ok){
      const err=await r.json().catch(()=>null);
      showToast(err?.detail||'文件夹路径不存在，需要写一个存在的路径');
      return;
    }
    const d=await r.json();
    if(d.status==='ok'){
      serverDir=d.base_dir;
      updateDirDisplay();
      loadDirHistory();
      if(d.created) showToast('已自动创建文件夹: '+d.base_dir);
    }
  }catch{
    showToast('保存失败，请检查路径');
    return;
  }
  document.getElementById('dirModalOverlay').classList.remove('open');
}

function copyDir(){
  const inp=document.getElementById('dirEditInput');
  const val=inp?inp.value.trim():'';
  const text=val||serverDir;
  if(!text)return;
  // Use clipboard API with fallback for non-HTTPS
  if(navigator.clipboard&&window.isSecureContext){
    navigator.clipboard.writeText(text).then(()=>showToast('已复制: '+text));
  }else{
    const ta=document.createElement('textarea');
    ta.value=text;ta.style.cssText='position:fixed;left:-9999px';
    document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');showToast('已复制: '+text);}
    catch(e){showToast('复制失败');}
    document.body.removeChild(ta);
  }
}

function openInFinder(){
  if(!serverDir)return;
  fetch(`${API}/api/open`,{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:serverDir,app:'finder'}),
  }).catch(()=>{
    window.open('file://'+serverDir,'_blank');
  });
}

function openInTerminal(){
  if(!serverDir)return;
  fetch(`${API}/api/open`,{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({path:serverDir,app:'terminal'}),
  }).catch(()=>{});
}

// ============ DRAG & DROP ============
function setupDragDrop(){
  const box=document.getElementById('inputBox');
  box.addEventListener('dragover',e=>{e.preventDefault();box.classList.add('dragover')});
  box.addEventListener('dragleave',()=>box.classList.remove('dragover'));
  box.addEventListener('drop',e=>{
    e.preventDefault();box.classList.remove('dragover');
    if(e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });
}

// ============ FILE HANDLING ============
function onFilePick(e){
  if(e.target.files.length) addFiles(e.target.files);
  e.target.value='';
}
function addFiles(fileList){
  for(const f of fileList) pendingFiles.push(f);
  renderFileList();
}
function removeFile(i){
  pendingFiles.splice(i,1);
  renderFileList();
}
function renderFileList(){
  const el=document.getElementById('fileList');
  if(!pendingFiles.length){el.innerHTML='';return}
  el.innerHTML=pendingFiles.map((f,i)=>{
    const sz=f.size>1024?(f.size/1024).toFixed(1)+'K':f.size+'B';
    return `<div class="file-chip"><span>${esc(f.name)}</span><span style="color:var(--fg3)">(${sz})</span><span class="fx" onclick="removeFile(${i})">&#x2715;</span></div>`;
  }).join('');
}

async function uploadFiles(targetDir){
  const uploaded=[];
  for(const f of pendingFiles){
    const fd=new FormData();
    fd.append('file',f);
    if(targetDir)fd.append('target_dir',targetDir);
    try{
      const r=await fetch(`${API}/api/upload`,{method:'POST',body:fd});
      const d=await r.json();
      uploaded.push(d.path);
    }catch(e){uploaded.push(`[upload failed: ${f.name}]`)}
  }
  pendingFiles=[];
  renderFileList();
  return uploaded;
}

// ============ SESSIONS ============
function loadSessions(){
  try{sessions=JSON.parse(localStorage.getItem('ag_s')||'{}')}catch{sessions={}}
  renderSidebar();
  const last=localStorage.getItem('ag_a');
  if(last&&sessions[last])switchTo(last);
}
function save(){localStorage.setItem('ag_s',JSON.stringify(sessions))}

function newSession(){
  if(streaming)return;
  const id=uid();
  sessions[id]={title:'New Chat',msgs:[],ts:Date.now(),tokIn:0,tokOut:0,dir:''};
  save();switchTo(id);renderSidebar();
  document.getElementById('inputTa').focus();
}

function switchTo(id){
  curSess=id;
  localStorage.setItem('ag_a',id);
  const s=sessions[id];
  updateTok(s);
  renderSidebar();renderChat();
}

let _confirmCb=null;
function showConfirm(msg,cb){
  _confirmCb=cb;
  document.getElementById('confirmMsg').textContent=msg;
  document.getElementById('confirmOverlay').classList.add('open');
}
function confirmOk(){
  document.getElementById('confirmOverlay').classList.remove('open');
  if(_confirmCb){_confirmCb();_confirmCb=null}
}
function confirmCancel(e){
  if(e&&e.target!==e.currentTarget)return;
  document.getElementById('confirmOverlay').classList.remove('open');
  _confirmCb=null;
}

function delSession(id,ev){
  ev.stopPropagation();
  showConfirm('确定删除该会话？',()=>{
    delete sessions[id];save();
    fetch(`${API}/api/sessions/${id}`,{method:'DELETE'}).catch(()=>{});
    if(curSess===id){curSess=null;updateTok(null);renderChat()}
    renderSidebar();
  });
}

function filterSessions(){
  const q=document.getElementById('searchInput').value.toLowerCase();
  document.querySelectorAll('.s-item').forEach(el=>{
    el.style.display=el.querySelector('.ti').textContent.toLowerCase().includes(q)?'':'none';
  });
}

function renderSidebar(){
  const el=document.getElementById('sessionList');
  const ids=Object.keys(sessions).sort((a,b)=>sessions[b].ts-sessions[a].ts);
  if(!ids.length){el.innerHTML='<div class="s-empty">No sessions</div>';return}
  el.innerHTML=ids.map(id=>{
    const s=sessions[id];const act=id===curSess?'active':'';
    const n=s.msgs?s.msgs.filter(m=>m.role==='user').length:0;
    return `<div class="s-item ${act}" onclick="switchTo('${id}')">
      <div class="ti">${esc(s.title)}</div>
      <div class="mt">${n} msg · ${ago(s.ts)}</div>
      <button class="db" onclick="delSession('${id}',event)" title="Delete">&#x2715;</button>
    </div>`;
  }).join('');
}

function updateTok(s){
  const ti=s?s.tokIn:0, to=s?s.tokOut:0;
  document.getElementById('tokIn').textContent=fmtN(ti);
  document.getElementById('tokOut').textContent=fmtN(to);
  const tip=document.getElementById('tokTip');
  if(tip){
    const total=ti+to;
    tip.textContent=`Input: ${ti.toLocaleString()} tokens · Output: ${to.toLocaleString()} tokens · Total: ${total.toLocaleString()}`;
  }
}
function fmtN(n){if(!n)return'0';if(n>=1000)return(n/1000).toFixed(1)+'K';return String(n)}

// ============ CHAT RENDER ============
function renderChat(){
  const c=document.getElementById('messages');
  if(!curSess||!sessions[curSess]||!sessions[curSess].msgs.length){
    c.innerHTML=`<div class="welcome"><img class="w-icon-img" src="${document.querySelector('.brand-logo').src}" alt="logo"><h2>Agentica</h2><p>AI Agent — send a message to get started.</p></div>`;
    return;
  }
  c.innerHTML=sessions[curSess].msgs.map(renderMsg).join('');
  scrollEnd();
}

function renderMsg(m){
  if(m.role==='user'){
    let h=`<div class="m m-u"><div class="bub">${esc(m.content)}`;
    if(m.files&&m.files.length) h+=m.files.map(f=>`<br><span style="font-size:11px;color:var(--fg3)">📎 ${esc(f)}</span>`).join('');
    h+=`</div></div>`;
    return h;
  }
  let h='<div class="m m-a">';
  if(m.steps&&m.steps.length){
    // Group steps into sections: consecutive thinking → one block, consecutive tools → one block
    const sections=[];
    for(const st of m.steps){
      if(st.type==='thinking'){
        const last=sections[sections.length-1];
        if(last&&last.type==='thinking'){last.items.push(st)}
        else sections.push({type:'thinking',items:[st]});
      } else if(st.type==='tool'){
        const last=sections[sections.length-1];
        if(last&&last.type==='tools'){last.items.push(st)}
        else sections.push({type:'tools',items:[st]});
      }
    }
    for(const sec of sections){
      if(sec.type==='thinking'){
        const text=sec.items.map(s=>s.text).join('\n');
        const preview=text.slice(0,80).replace(/\n/g,' ')+(text.length>80?'…':'');
        const hasBody=text.length>80||text.includes('\n');
        h+=`<div class="think-row${hasBody?' has-body':''}"${hasBody?` onclick="toggleThinkBody(this)"`:''}>`;
        if(hasBody) h+=`<span class="tg-arrow">&#x25B8;</span>`;
        h+=`<span class="think-icon">💭</span><span class="think-lbl">Thinking</span><span class="think-preview">${esc(preview)}</span></div>`;
        if(hasBody) h+=`<div class="think-body">${esc(text)}</div>`;
      } else if(sec.type==='tools'){
        const n=sec.items.length;
        // Check if any are special (todo/task) — those keep their distinctive style
        const specials=sec.items.filter(s=>s.name==='write_todos'||s.name==='read_todos'||s.name==='task');
        const normals=sec.items.filter(s=>s.name!=='write_todos'&&s.name!=='read_todos'&&s.name!=='task');

        // Render specials as sec-block (keep amber/purple styling)
        for(const st of specials){
          const icon=toolIcon(st.name||'');
          const dname=toolDisplay(st.name||'tool');
          const sclass=toolSecClass(st.name||'');
          const argsHtml=isRichTool(st.name)?fmtToolArgsHtml(st.name,st.rawArgs,st.argsStr):esc(st.argsStr||'');
          const isTodo=(st.name==='write_todos'||st.name==='read_todos');
          const isTask=(st.name==='task');
          const todoBody=isTodo?fmtTodoBodyHtml(st.rawArgs):'';
          const taskBody=isTask?fmtTaskBodyHtml(st.result):'';
          const hasBody=st.result||todoBody||taskBody;
          h+=`<div class="sec-block ${sclass}"><div class="sec-toggle"${hasBody?' onclick="toggleSec(this)" style="cursor:pointer"':' style="cursor:default"'}>
            ${hasBody?'<span class="arrow">&#x25B8;</span>':''}
            <span class="sec-icon">${icon}</span>
            <span class="sec-lbl">${esc(dname)}</span>
            <span class="sec-detail">${argsHtml}</span>
          </div>`;
          if(hasBody){
            let bodyH='';
            if(todoBody) bodyH+=todoBody;
            if(taskBody) bodyH+=taskBody;
            if(st.result&&!taskBody) bodyH+=`<div class="tg-result open" style="border:none;margin:0;padding:3px 12px">${esc(st.result)}</div>`;
            h+=`<div class="sec-body">${bodyH}</div>`;
          }
          h+=`</div>`;
        }

        // Render normal tools as flat rows
        if(normals.length){
          h+=`<div class="tool-group">`;
          const showAll=normals.length<=TOOL_VISIBLE_LIMIT+1;
          const visible=showAll?normals:normals.slice(0,TOOL_VISIBLE_LIMIT);
          const hidden=showAll?[]:normals.slice(TOOL_VISIBLE_LIMIT);
          for(const st of visible){
            h+=renderToolRow(st);
          }
          if(hidden.length){
            const gid='tg_'+Math.random().toString(36).slice(2,8);
            h+=`<div class="tg-more" onclick="toggleToolGroup(this,'${gid}')">… ${hidden.length} more tools</div>`;
            h+=`<div id="${gid}" style="display:none">`;
            for(const st of hidden){
              h+=renderToolRow(st);
            }
            h+=`</div>`;
          }
          h+=`</div>`;
        }
      }
    }
  }
  h+=`<div class="bub">${md(m.content)}</div></div>`;
  return h;
}

function toggleSec(el){
  const arrow=el.querySelector('.arrow');
  const body=el.nextElementSibling;
  const open=body.classList.toggle('open');
  arrow.classList.toggle('open',open);
}

// Toggle tool result visibility (flat row style)
function toggleToolResult(row){
  const result=row.nextElementSibling;
  if(!result||!result.classList.contains('tg-result'))return;
  const arrow=row.querySelector('.tg-arrow');
  const open=result.classList.toggle('open');
  if(arrow) arrow.classList.toggle('open',open);
}

// Toggle thinking body visibility (flat row style)
function toggleThinkBody(row){
  const body=row.nextElementSibling;
  if(!body||!body.classList.contains('think-body'))return;
  const arrow=row.querySelector('.tg-arrow');
  const open=body.classList.toggle('open');
  if(arrow) arrow.classList.toggle('open',open);
}

// Toggle collapsed tool group
function toggleToolGroup(moreEl, gid){
  const group=document.getElementById(gid);
  if(!group)return;
  const show=group.style.display==='none';
  group.style.display=show?'':'none';
  moreEl.textContent=show?'… collapse':'… '+(group.children.length)+' more tools';
}

function scrollEnd(){
  const a=document.getElementById('chatArea');
  userScrolledUp=false;
  requestAnimationFrame(()=>{a.scrollTop=a.scrollHeight});
  updateScrollBtn();
}

// Check if user is near the bottom (within half a screen)
function isNearBottom(){
  const a=document.getElementById('chatArea');
  return (a.scrollHeight - a.scrollTop - a.clientHeight) < a.clientHeight*0.5;
}

// Auto-scroll only if user hasn't deliberately scrolled up
function autoScroll(){
  if(userScrolledUp){
    updateScrollBtn();
    return;
  }
  if(isNearBottom()){
    scrollEnd();
  } else {
    updateScrollBtn();
  }
}

function updateScrollBtn(){
  const btn=document.getElementById('scrollBottomBtn');
  if(!btn)return;
  const a=document.getElementById('chatArea');
  // Show button when scrolled up more than ~half a screen
  const threshold=a.clientHeight*0.5;
  const far=(a.scrollHeight - a.scrollTop - a.clientHeight) > threshold;
  btn.classList.toggle('visible', far);
}

// ============ SEND / STREAM ============
function handleKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();if(!streaming)sendMessage()}}
function autoResize(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,200)+'px'}

function onAction(){
  if(streaming){stopGen();return}
  sendMessage();
}

async function sendMessage(){
  const ta=document.getElementById('inputTa');
  let text=ta.value.trim();
  if(!text&&!pendingFiles.length)return;
  if(streaming)return;

  if(!curSess)newSession();
  const s=sessions[curSess];

  // upload files first
  let uploadedPaths=[];
  if(pendingFiles.length){
    const targetDir=s.dir||serverDir||'';
    uploadedPaths=await uploadFiles(targetDir);
    if(!text) text='I uploaded files: '+uploadedPaths.join(', ');
    else text+='\n\n[Attached files: '+uploadedPaths.join(', ')+']';
  }

  // add user msg
  const userMsg={role:'user',content:ta.value.trim()||text};
  if(uploadedPaths.length) userMsg.files=uploadedPaths;
  s.msgs.push(userMsg);
  if(s.msgs.filter(m=>m.role==='user').length===1){
    s.title=userMsg.content.slice(0,50);
  }
  s.ts=Date.now();
  save();renderChat();renderSidebar();

  // clear
  ta.value='';ta.style.height='auto';

  // stream
  streaming=true;
  setStop();
  abortCtrl=new AbortController();

  const aiMsg={role:'assistant',content:'',steps:[]};
  s.msgs.push(aiMsg);
  appendLive();

  // current thinking accumulator
  let curThinking='';
  let approxIn=0,approxOut=0;
  approxIn=Math.ceil(text.length/3.5);

  try{
    const resp=await fetch(`${API}/api/chat/stream`,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text,session_id:curSess,user_id:'default'}),
      signal:abortCtrl.signal,
    });
    const reader=resp.body.getReader();
    const dec=new TextDecoder();
    let buf='';

    while(true){
      const{done,value}=await reader.read();
      if(done)break;
      buf+=dec.decode(value,{stream:true});
      const lines=buf.split('\n');buf=lines.pop();
      for(const line of lines){
        if(!line.startsWith('data: '))continue;
        const raw=line.slice(6);if(raw==='[DONE]')continue;
        try{
          const evt=JSON.parse(raw);
          if(evt.event==='thinking'){
            // Accumulate thinking text into current thinking step
            curThinking+=evt.data;
            approxOut+=Math.ceil(evt.data.length/3.5);
            const last=aiMsg.steps[aiMsg.steps.length-1];
            if(last&&last.type==='thinking'){
              last.text=curThinking;
            } else {
              aiMsg.steps.push({type:'thinking',text:curThinking});
            }
            updateLiveSteps(aiMsg);
          } else if(evt.event==='tool_call'){
            // Reset thinking accumulator — next thinking chunk starts fresh
            curThinking='';
            const name=evt.data.name||'tool';
            const argsStr=fmtToolArgs(name,evt.data.args);
            aiMsg.steps.push({type:'tool',name:name,text:name,argsStr:argsStr,rawArgs:evt.data.args});
            updateLiveSteps(aiMsg);
          } else if(evt.event==='tool_result'){
            // Attach result to the last matching tool step (by name, fallback to last tool)
            const rName=evt.data&&evt.data.name?evt.data.name:null;
            const res=evt.data&&evt.data.result?evt.data.result:(typeof evt.data==='string'?evt.data:JSON.stringify(evt.data));
            let target=null;
            if(rName){
              for(let i=aiMsg.steps.length-1;i>=0;i--){
                if(aiMsg.steps[i].type==='tool'&&aiMsg.steps[i].name===rName&&!aiMsg.steps[i].result){target=aiMsg.steps[i];break}
              }
            }
            if(!target) target=findLastTool(aiMsg.steps);
            if(target) target.result=(target.result||'')+res;
            updateLiveSteps(aiMsg);
          } else if(evt.event==='content'){
            if(curThinking){curThinking=''}
            aiMsg.content+=evt.data;
            approxOut+=Math.ceil(evt.data.length/3.5);
            updateLiveContent(aiMsg);
          } else if(evt.event==='done'){
            if(evt.data){
              const gotIn=evt.data.input_tokens||0;
              const gotOut=evt.data.output_tokens||0;
              if(gotIn>0||gotOut>0){
                s.tokIn=(s.tokIn||0)+gotIn;
                s.tokOut=(s.tokOut||0)+gotOut;
              } else {
                s.tokIn=(s.tokIn||0)+approxIn;
                s.tokOut=(s.tokOut||0)+approxOut;
              }
              updateTok(s);
            }
          } else if(evt.event==='error'){
            aiMsg.content+='\n\n**Error:** '+evt.data;
            updateLiveContent(aiMsg);
          }
        }catch{}
      }
    }
  }catch(err){
    if(err.name!=='AbortError') aiMsg.content+='\n\n**Error:** '+err.message;
    else aiMsg.content+=aiMsg.content?'\n\n*(stopped)*':'*(stopped)*';
  }

  streaming=false;abortCtrl=null;userScrolledUp=false;
  setSend();
  updateScrollBtn();
  s.ts=Date.now();save();
  renderChat();renderSidebar();
  document.getElementById('inputTa').focus();
}

function findLastTool(steps){
  for(let i=steps.length-1;i>=0;i--){
    if(steps[i].type==='tool')return steps[i];
  }
  return null;
}

function stopGen(){if(abortCtrl)abortCtrl.abort()}

// ============ LIVE DOM ============
function appendLive(){
  const c=document.getElementById('messages');
  const w=c.querySelector('.welcome');if(w)w.remove();
  const div=document.createElement('div');
  div.className='m m-a streaming';div.id='live';
  div.innerHTML=`<div id="live-sections"></div>
    <div class="bub" id="live-bub"></div>`;
  c.appendChild(div);scrollEnd();
}

function updateLiveContent(msg){
  const el=document.getElementById('live-bub');
  if(el){el.innerHTML=md(msg.content);autoScroll()}
}

function updateLiveSteps(msg){
  const el=document.getElementById('live-sections');
  if(!el)return;

  // Group steps into sections (same logic as renderMsg)
  const sections=[];
  for(const st of msg.steps){
    if(st.type==='thinking'){
      const last=sections[sections.length-1];
      if(last&&last.type==='thinking'){last.items.push(st)}
      else sections.push({type:'thinking',items:[st]});
    } else if(st.type==='tool'){
      const last=sections[sections.length-1];
      if(last&&last.type==='tools'){last.items.push(st)}
      else sections.push({type:'tools',items:[st]});
    }
  }

  const existingBlocks=el.querySelectorAll(':scope > .live-think-wrap, :scope > .live-tools-wrap');

  for(let si=0;si<sections.length;si++){
    const sec=sections[si];
    const isLast=(si===sections.length-1);
    const existing=existingBlocks[si];

    if(existing){
      if(sec.type==='thinking'){
        // Update thinking flat row in place
        const text=sec.items.map(s=>s.text).join('\n');
        const preview=text.slice(0,80).replace(/\n/g,' ')+(text.length>80?'…':'');
        const previewEl=existing.querySelector('.think-preview');
        const bodyEl=existing.querySelector('.think-body');
        const lblEl=existing.querySelector('.think-lbl');
        if(previewEl) previewEl.textContent=preview;
        if(lblEl) lblEl.textContent=isLast?'Thinking…':'Thinking';
        if(bodyEl) bodyEl.textContent=text;
        // During streaming, auto-open the body for the last (active) thinking
        if(isLast && bodyEl){
          bodyEl.classList.add('open');
          const arrow=existing.querySelector('.tg-arrow');
          if(arrow) arrow.classList.add('open');
        }
      } else if(sec.type==='tools'){
        // During live streaming: always show all tools as flat rows (no collapsing)
        // Specials (todo/task) get sec-block wrapper, normals get tg-row
        // For simplicity during streaming, rebuild innerHTML each time
        existing.innerHTML='';
        existing.className='live-tools-wrap';
        for(const st of sec.items){
          const isSpecial=(st.name==='write_todos'||st.name==='read_todos'||st.name==='task');
          if(isSpecial){
            const icon=toolIcon(st.name||'');
            const dname=toolDisplay(st.name||'tool');
            const sclass=toolSecClass(st.name||'');
            const argsHtml=isRichTool(st.name)?fmtToolArgsHtml(st.name,st.rawArgs,st.argsStr):esc(st.argsStr||'');
            const isTodo=(st.name==='write_todos'||st.name==='read_todos');
            const isTask=(st.name==='task');
            const todoBody=isTodo?fmtTodoBodyHtml(st.rawArgs):'';
            const taskBody=isTask?fmtTaskBodyHtml(st.result):'';
            const hasBody=st.result||todoBody||taskBody;
            let sh=`<div class="sec-block ${sclass}"><div class="sec-toggle"${hasBody?' onclick="toggleSec(this)" style="cursor:pointer"':' style="cursor:default"'}>
              ${hasBody?'<span class="arrow">&#x25B8;</span>':''}
              <span class="sec-icon">${icon}</span>
              <span class="sec-lbl">${esc(dname)}</span>
              <span class="sec-detail">${argsHtml}</span>
            </div>`;
            if(hasBody){
              let bodyH='';
              if(todoBody) bodyH+=todoBody;
              if(taskBody) bodyH+=taskBody;
              if(st.result&&!taskBody) bodyH+=`<div class="tg-result open" style="border:none;margin:0;padding:3px 12px">${esc(st.result)}</div>`;
              sh+=`<div class="sec-body">${bodyH}</div>`;
            }
            sh+=`</div>`;
            existing.insertAdjacentHTML('beforeend',sh);
          } else {
            existing.insertAdjacentHTML('beforeend',renderToolRow(st));
          }
        }
      }
    } else {
      // New section — create fresh
      const div=document.createElement('div');
      if(sec.type==='thinking'){
        const text=sec.items.map(s=>s.text).join('\n');
        const preview=text.slice(0,80).replace(/\n/g,' ')+(text.length>80?'…':'');
        div.className='live-think-wrap';
        div.innerHTML=`<div class="think-row has-body" onclick="toggleThinkBody(this)">
          <span class="tg-arrow${isLast?' open':''}">&#x25B8;</span>
          <span class="think-icon">💭</span>
          <span class="think-lbl">Thinking${isLast?'…':''}</span>
          <span class="think-preview">${esc(preview)}</span>
        </div><div class="think-body${isLast?' open':''}">${esc(text)}</div>`;
      } else if(sec.type==='tools'){
        div.className='live-tools-wrap';
        let toolsH='';
        for(const st of sec.items){
          const isSpecial=(st.name==='write_todos'||st.name==='read_todos'||st.name==='task');
          if(isSpecial){
            const icon=toolIcon(st.name||'');
            const dname=toolDisplay(st.name||'tool');
            const sclass=toolSecClass(st.name||'');
            const argsHtml=isRichTool(st.name)?fmtToolArgsHtml(st.name,st.rawArgs,st.argsStr):esc(st.argsStr||'');
            toolsH+=`<div class="sec-block ${sclass}"><div class="sec-toggle" style="cursor:default">
              <span class="sec-icon">${icon}</span>
              <span class="sec-lbl">${esc(dname)}</span>
              <span class="sec-detail">${argsHtml}</span>
            </div></div>`;
          } else {
            toolsH+=renderToolRow(st);
          }
        }
        div.innerHTML=toolsH;
      }
      // Close previous last section's think-body if user didn't manually toggle it
      if(el.children.length>0){
        const prevBlock=el.children[el.children.length-1];
        const prevBody=prevBlock.querySelector('.think-body.open');
        const prevArrow=prevBlock.querySelector('.tg-arrow.open');
        if(prevBody){prevBody.classList.remove('open')}
        if(prevArrow){prevArrow.classList.remove('open')}
      }
      el.appendChild(div);
    }
  }

  // Auto-scroll the last open think-body
  const bodies=el.querySelectorAll('.think-body.open');
  if(bodies.length){const last=bodies[bodies.length-1];last.scrollTop=last.scrollHeight};
  autoScroll();
}

// ============ ACTION BTN ============
const ICON_SEND='<svg viewBox="0 0 16 16" width="16" height="16"><path d="M13.5 3.5l-4 4m4-4v3m-4-3h4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/><path d="M6.5 6.5H3a.5.5 0 00-.5.5v6a.5.5 0 00.5.5h6a.5.5 0 00.5-.5V9.5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_ENTER='<svg viewBox="0 0 16 16" width="16" height="16"><path d="M12 3v5a2 2 0 01-2 2H4m0 0l3-3M4 10l3 3" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_STOP='<svg viewBox="0 0 16 16" width="16" height="16"><rect x="3" y="3" width="10" height="10" rx="1.5" fill="currentColor"/></svg>';
function setSend(){
  const b=document.getElementById('actBtn');
  b.className='act-btn send';b.innerHTML=ICON_ENTER;b.title='Send (Enter)';b.disabled=false;
}
function setStop(){
  const b=document.getElementById('actBtn');
  b.className='act-btn stop';b.innerHTML=ICON_STOP;b.title='Stop generation';b.disabled=false;
}

// ============ MARKDOWN ============
function md(text){
  if(!text)return '';
  let h=text;
  // Preserve LaTeX formulas before escaping HTML
  const mathBlocks=[];
  // Block math: $$...$$  or \[...\]
  h=h.replace(/\$\$([\s\S]*?)\$\$/g,(_,m)=>{mathBlocks.push({tex:m.trim(),display:true});return `%%MATH${mathBlocks.length-1}%%`});
  h=h.replace(/\\\[([\s\S]*?)\\\]/g,(_,m)=>{mathBlocks.push({tex:m.trim(),display:true});return `%%MATH${mathBlocks.length-1}%%`});
  // Inline math: $...$  or \(...\)
  h=h.replace(/\$([^\$\n]+?)\$/g,(_,m)=>{mathBlocks.push({tex:m.trim(),display:false});return `%%MATH${mathBlocks.length-1}%%`});
  h=h.replace(/\\\((.*?)\\\)/g,(_,m)=>{mathBlocks.push({tex:m.trim(),display:false});return `%%MATH${mathBlocks.length-1}%%`});

  h=h.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  h=h.replace(/```(\w*)\n([\s\S]*?)```/g,(_,lang,code)=>`<pre><code class="lang-${lang}">${code.trim()}</code></pre>`);
  h=h.replace(/`([^`]+)`/g,'<code>$1</code>');
  h=h.replace(/^###### (.+)$/gm,'<h6>$1</h6>');
  h=h.replace(/^##### (.+)$/gm,'<h5>$1</h5>');
  h=h.replace(/^#### (.+)$/gm,'<h4>$1</h4>');
  h=h.replace(/^### (.+)$/gm,'<h3>$1</h3>');
  h=h.replace(/^## (.+)$/gm,'<h2>$1</h2>');
  h=h.replace(/^# (.+)$/gm,'<h1>$1</h1>');
  h=h.replace(/^---+$/gm,'<hr>');
  h=h.replace(/\*\*\*(.*?)\*\*\*/g,'<strong><em>$1</em></strong>');
  h=h.replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>');
  h=h.replace(/\*(.*?)\*/g,'<em>$1</em>');
  h=h.replace(/!\[([^\]]*)\]\(([^)]+)\)/g,'<img src="$2" alt="$1" style="max-width:100%;border-radius:8px;margin:8px 0">');
  h=h.replace(/\[([^\]]+)\]\(([^)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  h=h.replace(/^&gt; (.+)$/gm,'<blockquote>$1</blockquote>');
  h=h.replace(/^\|(.+)\|\s*\n\|[-| :]+\|\s*\n((?:\|.+\|\s*\n?)*)/gm, (_, header, body) => {
    const ths=header.split('|').map(s=>s.trim()).filter(Boolean).map(s=>`<th>${s}</th>`).join('');
    const rows=body.trim().split('\n').map(row=>{
      const tds=row.split('|').map(s=>s.trim()).filter(Boolean).map(s=>`<td>${s}</td>`).join('');
      return `<tr>${tds}</tr>`;
    }).join('');
    return `<table><thead><tr>${ths}</tr></thead><tbody>${rows}</tbody></table>`;
  });
  h=h.replace(/^(\d+)\. (.+)$/gm,'<oli>$2</oli>');
  h=h.replace(/((<oli>.*<\/oli>\n?)+)/g,m=>'<ol>'+m.replace(/<\/?oli>/g,s=>s.replace('oli','li')).replace(/\n/g,'')+'</ol>');
  h=h.replace(/^[-*] (.+)$/gm,'<uli>$1</uli>');
  h=h.replace(/((<uli>.*<\/uli>\n?)+)/g,m=>'<ul>'+m.replace(/<\/?uli>/g,s=>s.replace('uli','li')).replace(/\n/g,'')+'</ul>');
  h=h.replace(/\n\n/g,'</p><p>');
  h=h.replace(/\n/g,'<br>');
  h='<p>'+h+'</p>';
  h=h.replace(/<p><\/p>/g,'');
  h=h.replace(/<p>(<(?:h[1-6]|pre|table|ul|ol|blockquote|hr)[^>]*>)/g,'$1');
  h=h.replace(/(<\/(?:h[1-6]|pre|table|ul|ol|blockquote|hr)>)<\/p>/g,'$1');
  h=h.replace(/<br>(<(?:ul|ol|h[1-6]|pre|table|blockquote|hr)[^>]*>)/g,'$1');
  h=h.replace(/(<\/(?:ul|ol|h[1-6]|pre|table|blockquote|hr)>)<br>/g,'$1');

  // Render LaTeX math
  h=h.replace(/%%MATH(\d+)%%/g,(_,i)=>{
    const m=mathBlocks[parseInt(i)];
    if(!m)return '';
    try{
      if(typeof katex!=='undefined'){
        return katex.renderToString(m.tex,{displayMode:m.display,throwOnError:false,strict:false});
      }
    }catch(e){}
    return m.display?`<div class="katex-display">${esc(m.tex)}</div>`:`<span>${esc(m.tex)}</span>`;
  });
  return h;
}

// ============ UTILS ============
function esc(s){if(!s)return'';const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function ago(ts){const d=Date.now()-ts;const m=Math.floor(d/60000);if(m<1)return'now';if(m<60)return m+'m';const h=Math.floor(m/60);if(h<24)return h+'h';return Math.floor(h/24)+'d'}
function toggleSidebar(){document.getElementById('sidebar').classList.toggle('collapsed')}
