'use strict';
const $ = id => document.getElementById(id);
const state = {csrf:null, project:document.body.dataset.project, page:document.body.dataset.page};
const labels = {new:'未生成',running:'生成中',review:'確認待ち',accepted:'採用済み',rejected:'要修正',unknown:'結果確認が必要',error:'エラー'};
function notice(message) { $('notice').textContent=message; $('notice').hidden=!message; }
function node(tag,text,cls) { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(cls)e.className=cls; return e; }
function key() { return crypto.randomUUID(); }
async function api(url, method='GET', body, operationKey) {
  const headers={};
  if(method!=='GET') headers['X-CSRF-Token']=state.csrf;
  if(operationKey)headers['Idempotency-Key']=operationKey;
  if(body && !(body instanceof FormData))headers['Content-Type']='application/json';
  const response=await fetch(url,{method,headers,body:body ? (body instanceof FormData ? body : JSON.stringify(body)) : undefined});
  const result=await response.json();
  if(!response.ok){const error=new Error(result.error?.message || '処理できませんでした。再読み込みしてください。');error.definitive=response.status<500;throw error;}
  return result.data;
}
// Persist the exact paid-operation payload until the response is known, including reloads.
async function operation(scope, prepare) {
  const storageKey='manga-operation:'+scope;
  let pending=JSON.parse(sessionStorage.getItem(storageKey)||'null');
  if(!pending){pending={...await prepare(),key:key()};sessionStorage.setItem(storageKey,JSON.stringify(pending));}
  try {const result=await api(pending.url,'POST',pending.body,pending.key);sessionStorage.removeItem(storageKey);return {...result,_sentBody:pending.body};}
  catch(error){if(error.definitive)sessionStorage.removeItem(storageKey);throw error;}
}
function action(id, handler) { const e=$(id); if(!e)return; e.addEventListener('click', async()=>{
  if(e.disabled)return; e.disabled=true; notice('');
  try {await handler();} catch(error){notice(error.message);} finally{e.disabled=false;}
}); }
function applyTheme(value) { const dark=value==='dark'||(value==='system'&&matchMedia('(prefers-color-scheme: dark)').matches); document.documentElement.dataset.theme=dark?'dark':'light'; }
const savedTheme=localStorage.getItem('manga-theme')||'system'; $('theme').value=savedTheme; applyTheme(savedTheme);
$('theme').addEventListener('change',()=>{localStorage.setItem('manga-theme',$('theme').value);applyTheme($('theme').value);});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>applyTheme($('theme').value));
action('close-zoom',()=> $('zoom').close());
async function checkHealth() { const h=await api('/api/health'); $('worker-status').textContent=h.worker_running?'ワーカー稼働中':'ワーカー停止中 · manga workerで起動'; }

async function settingsScreen() {
  let importToken=null, expectedVersion=null;
  let projectVersion=Number(document.body.dataset.projectVersion);
  async function importFiles(event) {
    notice(''); $('commit-import').hidden=true;
    const files=Array.from(event.target.files);
    const manifestFile=files.find(f=>f.name==='manifest.json');
    if(!manifestFile)throw new Error('manifest.jsonを含むフォルダーを選んでください。');
    const manifest=JSON.parse(await manifestFile.text());
    const full=manifestFile.webkitRelativePath||manifestFile.name;
    const prefix=full.slice(0,full.length-'manifest.json'.length);
    const imported=await api('/api/imports','POST'); importToken=imported.token;
    let uploaded=0;
    for(const file of files) {
      if(file===manifestFile)continue;
      const path=(file.webkitRelativePath||file.name).replace(new RegExp('^'+prefix.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')),'');
      const form=new FormData(); form.append('path',path);form.append('file',file);
      await api(`/api/imports/${importToken}/files`,'PUT',form);
      $('import-status').textContent=`資料を登録中 ${++uploaded}/${files.length-1}`;
    }
    const report=await api(`/api/imports/${importToken}/validate`,'POST',{manifest,project_id:state.project||null});
    expectedVersion=report.expected_version;
    $('import-status').textContent=report.errors.length? report.errors.map(e=>`${e.page||e.reference}: ${e.message}`).join('\n') : `${report.pages}ページ・資料${uploaded}ファイルを確認しました。`;
    $('commit-import').hidden=report.errors.length>0;
  }
  for(const id of ['folder','files']) $(id).addEventListener('change',e=>importFiles(e).catch(error=>notice(error.message)));
  action('commit-import',async()=>{const result=await api(`/api/imports/${importToken}/commit`,'POST',{expected_version:expectedVersion});location.href=`/projects/${result.project_id}`;});
  action('save-project',async()=>{
    const saved=await api(`/api/projects/${state.project}`,'PATCH',{expected_version:projectVersion,patch:{title:$('project-title').value,settings:{width:Number($('output-width').value),height:Number($('output-height').value),scenario:$('scenario').value}}});
    projectVersion=saved.version;notice('作品設定を保存しました。');
  });
  for(const button of document.querySelectorAll('.save-reference'))button.addEventListener('click',async()=>{
    const container=button.closest('.reference');button.disabled=true;
    try{const result=await api(`/api/references/${container.dataset.id}`,'PATCH',{expected_revision:container.dataset.revision,metadata:JSON.parse(container.querySelector('textarea').value)});container.dataset.revision=result.revision_id;notice('資料を保存しました。次回生成から反映します。');}catch(e){notice(e.message);}finally{button.disabled=false;}
  });
}

async function pagesScreen() {
  const selected=new Set(); let next=null, loaded=40, busy=false;
  function countSelection(){ $('selection-count').textContent=`${selected.size}ページを選択`; }
  async function load(reset=false,more=false) {
    if(busy)return;busy=true;
    try {
      if(reset){$('page-grid').replaceChildren();loaded=40;}
      if(more)loaded+=40;
      let cursor=0, items=[], continuation=null;
      while(items.length<loaded){
        const result=await api(`/api/projects/${state.project}/pages?limit=${Math.min(100,loaded-items.length)}&cursor=${cursor}&status=${$('filter').value}`);
        items.push(...result.items);continuation=result.next_cursor;
        if(!continuation)break;cursor=continuation;
      }
      next=continuation;
      const visibleIds=new Set(items.map(p=>p.id));
      for(const card of Array.from($('page-grid').children))if(!visibleIds.has(card.dataset.id))card.remove();
      for(const page of items){
        let card=$('page-grid').querySelector(`[data-id="${page.id}"]`);
        if(!card){
          card=node('article',undefined,'page-card');card.dataset.id=page.id;
          const link=node('a');link.href=`/pages/${page.id}`;
          const thumb=node('div',undefined,'thumbnail');thumb.append(node('span','未生成'));
          const caption=node('div',undefined,'card-caption');caption.append(node('strong',`${page.number}ページ`),node('span',''));
          link.append(thumb,caption);const label=node('label');const box=node('input');box.type='checkbox';box.setAttribute('aria-label',`${page.number}ページを選択`);box.addEventListener('change',()=>{box.checked?selected.add(page.id):selected.delete(page.id);countSelection();});label.append(box,document.createTextNode(' 選択'));card.append(link,label);$('page-grid').append(card);
        }
        card.querySelector('input').checked=selected.has(page.id);
        card.querySelector('.card-caption span').textContent=labels[page.status];
        const thumb=card.querySelector('.thumbnail');
        if(page.thumbnail && thumb.dataset.hash!==page.thumbnail){const img=node('img');img.src=`/api/assets/${page.thumbnail}`;img.alt=`${page.number}ページの候補`;img.loading='lazy';thumb.replaceChildren(img);thumb.dataset.hash=page.thumbnail;}
      }
      $('load-more').hidden=!next;$('empty').hidden=items.length!==0;countSelection();
    } finally {busy=false;}
  }
  async function generate(kind) {
    await operation('generate:'+state.project+':'+kind,async()=>{
      const request={kind,candidate_count:Number($('candidate-count').value)};
      if(selected.size)request.page_ids=Array.from(selected);
      const plan=await api(`/api/projects/${state.project}/plans`,'POST',request);
      $('plan-summary').textContent=`対象${plan.pages.length}ページ・各${plan.candidate_count}候補・${plan.jobs}件 / ${plan.chunks}チャンク。模擬API・課金なし。`+(plan.excluded.length?' 除外: '+plan.excluded.map(p=>`${p.number}ページ ${p.message}`).join(' / '):'');
      return {url:`/api/projects/${state.project}/batches`,body:{plan_token:plan.plan_token}};
    });
    selected.clear();await refresh();
  }
  async function refresh() {
    await checkHealth();
    const p=await api(`/api/projects/${state.project}/progress`);
    $('progress').textContent=Object.entries(p.counts).map(([status,count])=>`${labels[status]} ${count}`).join('　');
    const preserveDiagnostics=(document.activeElement?.matches('input,textarea')&&($('batch-details').contains(document.activeElement)||$('chunk-details').contains(document.activeElement)))||Array.from(document.querySelectorAll('#batch-details input,#chunk-details input')).some(e=>e.dataset.edited==='true');
    if(!preserveDiagnostics){
    const box=$('batch-details');box.replaceChildren();
    for(const batch of p.batches){
      const row=node('div',undefined,'diagnostic');
      const remaining=Math.floor((batch.cap_micro-batch.spent_reserved_micro)/batch.unit_reserve_micro);
      row.append(node('p',`残り送信枠 ${remaining}件${remaining===0?' · 未送信があれば枠を追加してください':''}`));
      const pause=node('button',batch.paused?'送信を再開':'新しい送信を停止','secondary');pause.addEventListener('click',()=>api(`/api/batches/${batch.id}/pause`,'POST',{paused:!batch.paused}).then(refresh).catch(e=>notice(e.message)));
      const label=node('label','追加する送信件数');const input=node('input');input.type='number';input.min='1';input.value='1';input.setAttribute('aria-label','追加する送信件数');label.append(input);
      const increase=node('button','送信枠を追加','secondary');increase.addEventListener('click',()=>api(`/api/batches/${batch.id}/budget`,'PATCH',{expected_cap:batch.cap_micro,new_cap:batch.cap_micro+Number(input.value)*batch.unit_reserve_micro}).then(()=>{input.dataset.edited='false';return refresh();}).catch(e=>notice(e.message)));
      row.append(pause,label,increase);box.append(row);
    }
    $('chunk-details').replaceChildren();
    for(const chunk of p.chunks){
      const row=node('div',undefined,'diagnostic');row.append(node('p',`${chunk.state==='unknown'?'結果確認が必要':'処理エラー'} · ${chunk.last_error||''}`));
      const actions=chunk.state==='unknown'?['reconcile','resend']:chunk.remote_name?['refetch']:[];
      for(const kind of actions){const b=node('button',{reconcile:'外部処理を再照合',resend:'重複の可能性を確認して再送',refetch:'結果を再取得'}[kind],'secondary');b.addEventListener('click',async()=>{try{if(kind==='resend'&&!confirm('既に生成された可能性があります。再送すると重複します。再送しますか？'))return;await operation('resolve:'+chunk.id,async()=>({url:`/api/chunks/${chunk.id}/resolve`,body:{action:kind,expected_state:chunk.state,explicit_duplicate_risk:kind==='resend'}}));await refresh();}catch(e){notice(e.message);}});row.append(b);}
      if(chunk.state==='unknown'){
        const label=node('label','外部IDを指定');const input=node('input');label.append(input);const attach=node('button','IDを照合','secondary');attach.addEventListener('click',()=>operation('resolve:'+chunk.id,async()=>({url:`/api/chunks/${chunk.id}/resolve`,body:{action:'attach',expected_state:chunk.state,remote_name:input.value}})).then(()=>{input.dataset.edited='false';input.blur();return refresh();}).catch(e=>notice(e.message)));row.append(label,attach);
      }
      $('chunk-details').append(row);
    }
    for(const input of document.querySelectorAll('#batch-details input,#chunk-details input'))input.addEventListener('input',()=>input.dataset.edited='true');
    }
    await load();
  }
  $('filter').addEventListener('change',()=>load(true).catch(e=>notice(e.message)));
  $('select-visible').addEventListener('change',()=>{for(const card of $('page-grid').children){$('select-visible').checked?selected.add(card.dataset.id):selected.delete(card.dataset.id);card.querySelector('input').checked=$('select-visible').checked;}countSelection();});
  action('select-filter',async()=>{let cursor=0;do{const r=await api(`/api/projects/${state.project}/pages?limit=100&cursor=${cursor}&status=${$('filter').value}`);r.items.forEach(p=>selected.add(p.id));cursor=r.next_cursor;}while(cursor);await load();});
  action('load-more',()=>load(false,true));
  action('generate',()=>generate('initial'));action('regenerate',()=>generate('revision'));action('generate-extra',()=>generate('extra'));
  async function exportPages(mode){const progress=await api(`/api/projects/${state.project}/progress`);const result=await api(`/api/projects/${state.project}/exports`,'POST',{mode,expected_version:progress.project_version});$('export-status').textContent='書き出し中…';const interval=setInterval(async()=>{try{const e=await api(`/api/exports/${result.export_id}`);if(e.state==='complete'){clearInterval(interval);const a=node('a','ZIPをダウンロード');a.href=e.download_url;$('export-status').replaceChildren(a);}else if(e.state==='failed'){clearInterval(interval);notice('書き出しに失敗しました。画像の状態を確認してください。');}}catch(e){clearInterval(interval);notice(e.message);}},1000);}
  action('export-all',()=>exportPages('complete'));action('export-partial',()=>exportPages('accepted_only'));
  await refresh();return refresh;
}

async function detailScreen() {
  let page=null, dirty=false, editVersion=null, currentCandidate=null, historyBefore=2147483647;
  const historyCandidates=new Map(), historyRounds=new Map();
  function markDirty(){if(!dirty)editVersion=page?.version;dirty=true;$('dirty').textContent='未保存の変更があります';}
  for(const id of ['correction','directions','prompt-override','override-enabled'])$(id).addEventListener('input',markDirty);
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  function candidate(){return [...page.candidates,...historyCandidates.values()].find(c=>c.id===$('candidate-select').value);}
  function showCandidate(){const c=candidate();const round=c?(historyRounds.get(c.round_id)||(page.latest_round?.id===c.round_id?page.latest_round:null)):null;$('saved-round').textContent=round?`保存済み第${round.sequence}版\n却下理由: ${round.rejection_reason||'なし'}\n\n${round.prompt_text}`:'過去の版を追加表示すると、対応する送信全文を確認できます。';currentCandidate=c?.id||null;$('candidate-empty').hidden=!!c;$('candidate-image').hidden=!c;if(c)$('candidate-image').src=`/api/assets/${c.asset_hash}`;$('accept').disabled=!c||c.availability!=='available';}
  function patch(){return {correction:$('correction').value,directions:JSON.parse($('directions').value),prompt_override:$('override-enabled').checked?$('prompt-override').value:null};}
  async function refresh() {
    page=await api(`/api/pages/${state.page}`);
    $('revise').textContent=sessionStorage.getItem('manga-operation:revise:'+state.page)?'前回の再生成結果を確認':'修正して再生成';
    $('page-status').textContent=labels[page.status];$('accepted-label').textContent=page.accepted_candidate_id?'採用画像を保持しています':'';
    const select=$('candidate-select'), old=select.value||currentCandidate;select.replaceChildren();
    const unique=new Map([...page.candidates,...historyCandidates.values()].map(c=>[c.id,c]));
    for(const c of unique.values()){const opt=node('option',`第${c.sequence||'?'}版・候補${c.candidate_index||''}${c.id===page.accepted_candidate_id?'（採用）':''}${c.availability!=='available'?'（欠損）':''}`);opt.value=c.id;select.append(opt);}
    if(unique.has(old))select.value=old;else if(page.accepted_candidate_id&&unique.has(page.accepted_candidate_id))select.value=page.accepted_candidate_id;
    showCandidate();
    const conflict=dirty&&editVersion!==page.version;$('edit-conflict').hidden=!conflict;if(conflict){$('dirty').textContent='別の画面で更新されています。編集内容を残して競合を確認してください。';$('conflict-saved').textContent=JSON.stringify(page.draft,null,2);}
    if(!dirty){editVersion=page.version;$('correction').value=page.draft.correction;$('directions').value=JSON.stringify(page.draft.directions,null,2);$('override-enabled').checked=page.draft.prompt_override!==null;$('prompt-override').value=page.draft.prompt_override||'';}
    $('job-details').replaceChildren();
    for(const job of page.jobs){if(job.state==='failed'){const b=node('button',`候補${job.candidate_index}の生成を再試行`,'secondary');b.addEventListener('click',()=>operation('retry:'+job.id,async()=>({url:`/api/jobs/${job.id}/retry`,body:{expected_state:'failed',explicit_extra_attempt:true}})).then(refresh).catch(e=>notice(e.message)));$('job-details').append(b);}}
    await checkHealth();
  }
  async function save(){await api(`/api/pages/${state.page}/draft`,'PATCH',{expected_version:editVersion,patch:patch()});dirty=false;$('dirty').textContent='保存済み';await refresh();}
  async function decide(decision){const c=candidate();page=await api(`/api/pages/${state.page}/review`,'POST',{expected_version:page.version,latest_round_id:page.latest_round_id,decision,candidate_id:decision==='accept'?c?.id:null,reason:$('reason').value,restore:$('restore-old').checked,clear_accepted:$('clear-accepted').checked});await refresh();}
  action('rebase-edit',()=>{editVersion=page.version;$('edit-conflict').hidden=true;$('dirty').textContent='未保存の変更があります（最新内容を確認済み）';});
  action('save-draft',save);
  action('accept',()=>decide('accept'));
  action('reject-save',async()=>{if(dirty)await save();await decide('reject');});
  action('revise',async()=>{const result=await operation('revise:'+state.page,async()=>({url:`/api/pages/${state.page}/revise-and-generate`,body:{expected_version:editVersion,latest_round_id:page.latest_round_id,draft_patch:patch(),reason:$('reason').value,clear_accepted:$('clear-accepted').checked}}));const unchanged=JSON.stringify(patch())===JSON.stringify(result._sentBody.draft_patch);if(unchanged){dirty=false;$('dirty').textContent='保存済み';}else{dirty=true;notice('前回の再生成結果を確認しました。新しい編集内容は未保存のまま保持しています。');}await refresh();});
  action('preview-prompt',async()=>{const p=await api(`/api/pages/${state.page}/prompt`);if($('override-enabled').checked&&dirty&&!confirm('編集中の全文を保存済みの指示で置き換えますか？'))return;$('prompt-override').value=p.prompt;$('attachments').textContent='添付: '+p.attachments.map(a=>a.role+(a.key?' '+a.key:'')).join('、');markDirty();});
  action('copy-reason',()=>{$('correction').value+='\n'+$('reason').value;markDirty();});
  action('history-more',async()=>{const rows=await api(`/api/pages/${state.page}/history?before=${historyBefore}`);for(const r of rows){historyRounds.set(r.id,r);for(const c of r.candidates)historyCandidates.set(c.id,{...c,round_id:r.id,sequence:r.sequence});}if(rows.length)historyBefore=rows.at(-1).sequence;else $('history-more').hidden=true;await refresh();});
  $('candidate-select').addEventListener('change',showCandidate);
  $('source-select').addEventListener('change',()=>{$('source-image').src=`/api/assets/${$('source-select').value==='name'?page.draft.name_hash:page.draft.instruction_hash}`;});
  action('enlarge',()=>{const c=candidate();if(c){$('zoom').querySelector('img').src=`/api/assets/${c.asset_hash}`;$('zoom').showModal();}});
  await refresh();return refresh;
}

(async()=>{
  try {
    let session=await api('/api/session');if(!session.csrf)session=await api('/api/session');state.csrf=session.csrf;
    await checkHealth();
    let refresh=checkHealth;
    if(document.body.dataset.screen==='settings')await settingsScreen();
    if(document.body.dataset.screen==='pages')refresh=await pagesScreen();
    if(document.body.dataset.screen==='detail')refresh=await detailScreen();
    setInterval(()=>{if(!document.hidden)refresh().catch(e=>notice(e.message));},5000);
    document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh().catch(e=>notice(e.message));});
  }catch(e){notice(e.message);}
})();
