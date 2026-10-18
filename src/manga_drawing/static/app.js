'use strict';
const $ = id => document.getElementById(id);
const state = {csrf:null, project:document.body.dataset.project, page:document.body.dataset.page, provider:'mock'};
const labels = {new:'生成未依頼',running:'生成処理中（未完了）',review:'候補あり・採否未判定',accepted:'採用済み',rejected:'要修正',unknown:'受理・完了状況が不明',error:'生成失敗・処理停止'};
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
function action(id, handler, disabled) { const e=$(id); if(!e)return; e.addEventListener('click', async()=>{
  if(e.disabled)return; e.disabled=true; notice('');
  try {await handler();} catch(error){notice(error.message);} finally{e.disabled=disabled?disabled():false;}
}); }
function applyTheme(value) { const dark=value==='dark'||(value==='system'&&matchMedia('(prefers-color-scheme: dark)').matches); document.documentElement.dataset.theme=dark?'dark':'light'; }
const savedTheme=localStorage.getItem('manga-theme')||'system'; $('theme').value=savedTheme; applyTheme(savedTheme);
$('theme').addEventListener('change',()=>{localStorage.setItem('manga-theme',$('theme').value);applyTheme($('theme').value);});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>applyTheme($('theme').value));
action('close-zoom',()=> $('zoom').close());
async function checkHealth() {
  const h=await api('/api/health');state.provider=h.provider;
  $('worker-status').textContent=(h.worker_running?'生成処理は稼働中':'生成処理が停止しています')+(h.provider==='gemini'?' / Gemini':' / 模擬API')+(h.live_remaining_requests!==null&&h.provider==='gemini'?` / 試験用の残り生成回数：${h.live_remaining_requests}`:'')+(h.rejected_output_error?' / 却下候補のファイル保存に失敗（アプリ内の画像は保持）':'');
}

async function settingsScreen() {
  function filterLibrary() {
    const value=$('library-filter')?.value||'all';
    for(const item of document.querySelectorAll('.episode-item')) item.hidden=value!=='all'&&item.dataset.lifecycle!==value;
    for(const group of document.querySelectorAll('.work-group')) group.hidden=!Array.from(group.querySelectorAll('.episode-item')).some(item=>!item.hidden);
  }
  $('library-filter')?.addEventListener('change',filterLibrary);filterLibrary();
  let importToken=null, expectedVersion=null;
  let projectVersion=Number(document.body.dataset.projectVersion);
  async function importFiles(event) {
    notice(''); $('commit-import').hidden=true;
    const files=Array.from(event.target.files).filter(file=>file.name==='manifest.json'||/\.(png|jpe?g)$/i.test(file.name));
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
  action('commit-import',async()=>{const result=await api(`/api/imports/${importToken}/commit`,'POST',{expected_version:expectedVersion,work_id:$('import-work')?.value||null});location.href=`/projects/${result.project_id}`;});
  action('save-project',async()=>{
    const saved=await api(`/api/projects/${state.project}`,'PATCH',{expected_version:projectVersion,patch:{title:$('project-title').value,work_id:$('project-work').value,work_title:$('work-title').value,lifecycle:$('project-lifecycle').value,work_folder:$('work-folder').value,settings:{width:Number($('output-width').value),height:Number($('output-height').value),scenario:$('scenario').value}}});
    projectVersion=saved.version;notice('作品設定を保存しました。');
  });
  for(const button of document.querySelectorAll('.save-reference'))button.addEventListener('click',async()=>{
    const container=button.closest('.reference');button.disabled=true;
    try{const result=await api(`/api/references/${container.dataset.id}`,'PATCH',{expected_revision:container.dataset.revision,metadata:JSON.parse(container.querySelector('textarea').value)});container.dataset.revision=result.revision_id;notice('資料を保存しました。次回生成から反映します。');}catch(e){notice(e.message);}finally{button.disabled=false;}
  });
}

async function pagesScreen() {
  const selected=new Set(), visiblePages=new Map(); let next=null, loaded=40, busy=false;
  function countSelection(){
    $('selection-count').textContent=selected.size?`生成対象：指定した${selected.size}ページ（採否判定とは別です）`:'生成対象：ボタンに該当する全ページ';
    const boxes=Array.from(document.querySelectorAll('.generation-choice input'));
    $('select-visible').checked=boxes.length>0&&boxes.every(box=>box.checked);
    $('select-visible').indeterminate=boxes.some(box=>box.checked)&&!$('select-visible').checked;
    $('generate').textContent=selected.size?'指定ページのうち未生成を生成':'未生成ページを生成';
    $('regenerate').textContent=selected.size?'指定ページのうち要修正を再生成':'要修正ページをまとめて再生成';
    $('generate-extra').disabled=selected.size===0;
  }
  function showGenerationChoices(){for(const label of document.querySelectorAll('.generation-choice'))label.hidden=!$('batch-selection').open;}
  async function reviewPage(id,decision){
    const page=visiblePages.get(id);
    if(!page)return;
    if(decision==='accept'&&!page.review_candidate_id){location.href=`/pages/${id}`;return;}
    const card=$('page-grid').querySelector(`[data-id="${id}"]`);
    const buttons=Array.from(card.querySelectorAll('button'));buttons.forEach(b=>b.disabled=true);
    try {
      await api(`/api/pages/${id}/review`,'POST',{expected_version:page.version,latest_round_id:page.latest_round_id,decision,candidate_id:decision==='accept'?page.review_candidate_id:null,clear_accepted:decision==='reject'&&!!page.accepted_candidate_id});
      $('review-result').textContent=`${page.number}ページを${decision==='accept'?'採用済み':'要修正'}にしました。`;
      await refresh();
    }catch(error){notice(error.message);await load();}
  }
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
      next=continuation;visiblePages.clear();items.forEach(page=>visiblePages.set(page.id,page));
      const visibleIds=new Set(items.map(p=>p.id));
      for(const card of Array.from($('page-grid').children))if(!visibleIds.has(card.dataset.id))card.remove();
      for(const page of items){
        let card=$('page-grid').querySelector(`[data-id="${page.id}"]`);
        if(!card){
          card=node('article',undefined,'page-card');card.dataset.id=page.id;
          const link=node('a');link.href=`/pages/${page.id}`;
          const thumb=node('div',undefined,'thumbnail');
          const caption=node('div',undefined,'card-caption');caption.append(node('strong',`${page.number}ページ`),node('span',''));
          const phase=node('p',undefined,'generation-phase');phase.setAttribute('role','status');caption.append(phase);link.append(thumb,caption);
          const decisions=node('div',undefined,'card-decisions');
          const accept=node('button','採用する','card-accept');accept.addEventListener('click',()=>reviewPage(page.id,'accept'));
          const reject=node('button','要修正にする','secondary card-reject');reject.addEventListener('click',()=>reviewPage(page.id,'reject'));
          decisions.append(accept,reject);
          const label=node('label',undefined,'generation-choice');const box=node('input');box.type='checkbox';box.setAttribute('aria-label',`${page.number}ページを生成対象にする`);box.addEventListener('change',()=>{box.checked?selected.add(page.id):selected.delete(page.id);countSelection();});label.append(box,document.createTextNode(' 生成対象にする'));card.append(link,decisions,label);$('page-grid').append(card);
        }
        card.querySelector('input').checked=selected.has(page.id);
        const badge=card.querySelector('.card-caption span');badge.textContent=labels[page.status];badge.className='review-status status-'+page.status;card.dataset.status=page.status;
        const accept=card.querySelector('.card-accept'), reject=card.querySelector('.card-reject');
        accept.textContent=page.accepted_candidate_id?'採用済み':!page.reviewable?'今回の生成完了後に採用可能':!page.review_candidate_id?'候補を比較して採用':'採用する';
        accept.disabled=!page.reviewable||!!page.accepted_candidate_id;
        reject.textContent=page.accepted_candidate_id?'採用解除して要修正':page.status==='rejected'?'要修正に設定済み':'要修正にする';
        reject.disabled=!page.reviewable||page.status==='rejected';
        card.querySelector('.generation-phase').textContent=(page.generation_sequence?`テイク${page.generation_sequence}：`:'')+page.generation_label;
        const thumb=card.querySelector('.thumbnail');
        if(page.thumbnail && thumb.dataset.hash!==page.thumbnail){const img=node('img');img.src=`/api/assets/${page.thumbnail}`;img.alt=`${page.number}ページの候補`;img.loading='lazy';thumb.replaceChildren(img);thumb.dataset.hash=page.thumbnail;}
        if(!page.thumbnail){thumb.replaceChildren(node('span',page.generation_label));delete thumb.dataset.hash;}
      }
      $('load-more').hidden=!next;$('empty').hidden=items.length!==0;showGenerationChoices();countSelection();
    } finally {busy=false;}
  }
  async function generate(kind) {
    $('plan-summary').textContent='生成依頼を登録中です。受付完了はまだ確認できていません。';
    let plannedPages=null;
    try { await operation('generate:'+state.project+':'+kind,async()=>{
      const request={kind,candidate_count:Number($('candidate-count').value)};
      if(selected.size)request.page_ids=Array.from(selected);
      const plan=await api(`/api/projects/${state.project}/plans`,'POST',request);
      plannedPages=plan.pages.length;
      $('plan-summary').textContent=`${plan.pages.length}ページ・各${plan.candidate_count}候補の依頼内容を確認済み。開始依頼の受付はまだ確認できていません。`+(plan.excluded.length?' 除外: '+plan.excluded.map(p=>`${p.number}ページ ${p.message}`).join(' / '):'');
      return {url:`/api/projects/${state.project}/batches`,body:{plan_token:plan.plan_token}};
    });
    } catch(error){$('plan-summary').textContent=error.definitive?'生成依頼を開始できませんでした。':'生成依頼の受付状況が不明です。ページの状態を再確認してください。';throw error;}
    $('plan-summary').textContent=`生成依頼を受付済み${plannedPages!==null?`（対象${plannedPages}ページ）`:''}。まだ生成完了ではありません。各ページにAPI送信・生成結果の状態を表示します。`;
    selected.clear();await refresh();
  }
  async function refresh() {
    await checkHealth();
    const p=await api(`/api/projects/${state.project}/progress`);
    $('progress').textContent=Object.entries(p.counts).map(([status,count])=>`${labels[status]} ${count}ページ`).join('　');
    const preserveDiagnostics=(document.activeElement?.matches('input,textarea')&&($('batch-details').contains(document.activeElement)||$('chunk-details').contains(document.activeElement)))||Array.from(document.querySelectorAll('#batch-details input,#chunk-details input')).some(e=>e.dataset.edited==='true');
    if(!preserveDiagnostics){
    const box=$('batch-details');box.replaceChildren();
    $('generation-status').textContent=p.generation_summary||'生成するページは登録されていません。';
    for(const batch of p.batches){
      if(!batch.waiting&&!batch.running)continue;
      const row=node('div',undefined,'diagnostic');
      row.append(node('p',`送信・生成結果の処理中 ${batch.running}件・API送信前の待機中 ${batch.waiting}件${batch.paused?'（開始を一時停止中）':''}`));
      if(batch.waiting){
        const pause=node('button',batch.paused?'待機中の生成を再開':'待機中の生成を一時停止','secondary');pause.addEventListener('click',()=>api(`/api/batches/${batch.id}/pause`,'POST',{paused:!batch.paused}).then(refresh).catch(e=>notice(e.message)));row.append(pause);
        if(batch.spent_reserved_micro+batch.waiting*batch.unit_reserve_micro>batch.cap_micro){
          const resume=node('button',`待機中の${batch.waiting}件を生成する`,'secondary');resume.addEventListener('click',()=>api(`/api/batches/${batch.id}/budget`,'PATCH',{expected_cap:batch.cap_micro,new_cap:batch.spent_reserved_micro+batch.waiting*batch.unit_reserve_micro}).then(refresh).catch(e=>notice(e.message)));row.append(resume);
        }
      }
      box.append(row);
    }
    $('recovery-details').hidden=p.chunks.length===0;
    $('chunk-details').replaceChildren();
    for(const chunk of p.chunks){
      const row=node('div',undefined,'diagnostic');row.append(node('p',chunk.state==='unknown'?'生成結果をまだ確認できません。まず外部APIの結果を再確認してください。':'生成または画像の取得に失敗しました。'));
      const actions=chunk.state==='unknown'?['reconcile','resend']:chunk.remote_name?['refetch']:[];
      for(const kind of actions){const b=node('button',{reconcile:'生成結果を再確認',resend:'重複の可能性を承知して再送',refetch:'生成済みの画像をもう一度取得'}[kind],'secondary');b.addEventListener('click',async()=>{try{if(kind==='resend'&&!confirm('既に生成された可能性があります。再送すると重複します。再送しますか？'))return;await operation('resolve:'+chunk.id,async()=>({url:`/api/chunks/${chunk.id}/resolve`,body:{action:kind,expected_state:chunk.state,explicit_duplicate_risk:kind==='resend'}}));await refresh();}catch(e){notice(e.message);}});row.append(b);}
      if(chunk.state==='unknown'){
        const label=node('label','外部IDを指定');const input=node('input');label.append(input);const attach=node('button','IDを照合','secondary');attach.addEventListener('click',()=>operation('resolve:'+chunk.id,async()=>({url:`/api/chunks/${chunk.id}/resolve`,body:{action:'attach',expected_state:chunk.state,remote_name:input.value}})).then(()=>{input.dataset.edited='false';input.blur();return refresh();}).catch(e=>notice(e.message)));row.append(label,attach);
      }
      $('chunk-details').append(row);
    }
    for(const input of document.querySelectorAll('#batch-details input,#chunk-details input'))input.addEventListener('input',()=>input.dataset.edited='true');
    }
    await load();
  }
  $('filter').addEventListener('change',()=>{selected.clear();load(true).catch(e=>notice(e.message));});
  $('batch-selection').addEventListener('toggle',()=>{showGenerationChoices();if(!$('batch-selection').open){selected.clear();for(const box of document.querySelectorAll('.generation-choice input'))box.checked=false;countSelection();}});
  action('clear-selection',async()=>{selected.clear();await load();});
  $('select-visible').addEventListener('change',()=>{for(const card of $('page-grid').children){$('select-visible').checked?selected.add(card.dataset.id):selected.delete(card.dataset.id);card.querySelector('input').checked=$('select-visible').checked;}countSelection();});
  action('select-filter',async()=>{let cursor=0;do{const r=await api(`/api/projects/${state.project}/pages?limit=100&cursor=${cursor}&status=${$('filter').value}`);r.items.forEach(p=>selected.add(p.id));cursor=r.next_cursor;}while(cursor);await load();});
  action('load-more',()=>load(false,true));
  action('generate',()=>generate('initial'));action('regenerate',()=>generate('revision'));action('generate-extra',()=>generate('extra'),()=>selected.size===0);
  function showExportFolder(result) {
    const button=node('button','画像フォルダーを開く','secondary');
    button.addEventListener('click',async()=>{
      button.disabled=true;
      try {await api(result.open_url,'POST');notice('画像フォルダーを開きました。');}
      catch(error){notice(error.message);}finally{button.disabled=false;}
    });
    const path=node('p',result.folder_path,'muted');
    const takes=node('p','書き出した採用画像：'+(result.pages||[]).map(page=>`${page.number}ページ＝テイク${page.take}`).join('、'));
    $('export-status').replaceChildren(button,takes,path);
  }
  const previousExport=$('export-status').dataset.exportId;
  if(previousExport){try{showExportFolder(await api(`/api/exports/${previousExport}`));}catch(error){notice(error.message);}}
  async function exportPages(mode){const progress=await api(`/api/projects/${state.project}/progress`);const result=await api(`/api/projects/${state.project}/exports`,'POST',{mode,expected_version:progress.project_version});$('export-status').textContent='書き出し中…';const interval=setInterval(async()=>{try{const e=await api(`/api/exports/${result.export_id}`);if(e.state==='complete'){clearInterval(interval);showExportFolder(e);}else if(e.state==='failed'){clearInterval(interval);notice('書き出しに失敗しました。画像の状態を確認してください。');}}catch(e){clearInterval(interval);notice(e.message);}},1000);}
  action('export-all',()=>exportPages('complete'));action('export-partial',()=>exportPages('accepted_only'));
  await refresh();return refresh;
}

async function detailScreen() {
  let page=null, dirty=false, editVersion=null, currentCandidate=null, userSelectedCandidate=false, autoLatestId=null, historyBefore=2147483647;
  const historyCandidates=new Map(), historyRounds=new Map();
  function markDirty(){$('instruction-save-state').textContent='未保存の作画指示・修正があります';$('prompt-mode-warning').hidden=!$('override-enabled').checked;if(!dirty)editVersion=page?.version;dirty=true;$('dirty').textContent='未保存の変更があります';}
  for(const id of ['page-prompt','correction','directions','prompt-override','override-enabled'])$(id).addEventListener('input',markDirty);
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  function candidate(){return [...page.candidates,...historyCandidates.values()].find(c=>c.id===$('candidate-select').value);}
  function cannotAccept(){const c=candidate();return !c||c.availability!=='available'||c.id===page.accepted_candidate_id||(c.round_id!==page.latest_round_id&&!$('restore-old').checked);}
  function showCandidate(){const c=candidate();const round=c?(historyRounds.get(c.round_id)||(page.latest_round?.id===c.round_id?page.latest_round:null)):null;$('saved-round').textContent=round?`保存済みテイク${round.sequence}\n却下理由: ${round.rejection_reason||'なし'}\n\n${round.prompt_text}`:'過去の版を追加表示すると、対応する送信全文を確認できます。';currentCandidate=c?.id||null;$('candidate-empty').textContent=page.generation_label+'。今回の候補画像はまだ表示できません。';$('candidate-context').textContent=c?`表示中：テイク${c.sequence||'?'}の生成結果${c.round_id!==page.latest_round_id?`（過去の版。今回のテイク${page.generation_sequence}ではありません）`:'（今回の版）'}`:'';$('candidate-empty').hidden=!!c;$('candidate-image').hidden=!c;if(c){$('candidate-image').src=`/api/assets/${c.asset_hash}`;$('candidate-image').alt=c.provider==='gemini'?'Geminiの候補画像':'模擬APIの候補画像';}const isOld=!!c&&c.round_id!==page.latest_round_id;
    $('restore-old-label').hidden=!isOld;
    if(!isOld)$('restore-old').checked=false;
    const alreadyAccepted=!!c&&c.id===page.accepted_candidate_id;
    $('accept').textContent=alreadyAccepted?'この候補は採用済み':isOld?'表示中の過去候補を採用':'この候補を採用';
    $('accept').disabled=cannotAccept();
    $('accept-help').textContent=alreadyAccepted?'この画像はすでに採用されています。':!c?'今回の候補画像が届くと採用できます。':isOld?'表示中は過去の版です。採用する場合は上の確認欄にチェックしてください。':'';
  }
  function patch(){return {page_prompt:$('page-prompt').value,correction:$('correction').value,directions:JSON.parse($('directions').value),prompt_override:$('override-enabled').checked?$('prompt-override').value:null};}
  async function refresh() {
    const fetched=await api(`/api/pages/${state.page}`);
    if(page&&fetched.version<page.version)return;
    page=fetched;
    $('revise').textContent=sessionStorage.getItem('manga-operation:revise:'+state.page)?'前回の再生成結果を確認':'修正して再生成';
    $('generation-phase').textContent=(page.generation_sequence?`今回の生成（テイク${page.generation_sequence}）：`:'')+page.generation_label;
    $('page-status').textContent=labels[page.status];$('accepted-label').textContent=page.accepted_candidate_id?'採用画像を保持しています':'';
    const select=$('candidate-select'), old=select.value||currentCandidate;select.replaceChildren();
    const unique=new Map([...page.candidates,...historyCandidates.values()].map(c=>[c.id,c]));
    for(const c of unique.values()){const opt=node('option',`[${c.provider==='gemini'?'Gemini':'模擬'}] テイク${c.sequence||'?'}・候補${c.candidate_index||''}${c.round_id!==page.latest_round_id?'（過去の生成結果）':''}${c.id===page.accepted_candidate_id?'（採用）':''}${c.availability!=='available'?'（欠損）':''}`);opt.value=c.id;select.append(opt);}
    const latest=page.candidates.find(c=>c.round_id===page.latest_round_id&&c.availability==='available');
    if(latest&&latest.id!==autoLatestId){if(!$('restore-old').checked)userSelectedCandidate=false;autoLatestId=latest.id;}
    if(userSelectedCandidate&&unique.has(old))select.value=old;
    else if(latest)select.value=latest.id;
    else if(page.accepted_candidate_id&&unique.has(page.accepted_candidate_id))select.value=page.accepted_candidate_id;
    showCandidate();
    const conflict=dirty&&editVersion!==page.version;$('edit-conflict').hidden=!conflict;if(conflict){$('dirty').textContent='別の画面で更新されています。編集内容を残して競合を確認してください。';$('conflict-saved').textContent=JSON.stringify(page.draft,null,2);}
    if(!dirty){$('instruction-save-state').textContent='保存済み';editVersion=page.version;$('page-prompt').value=page.draft.page_prompt||'';$('correction').value=page.draft.correction;$('directions').value=JSON.stringify(page.draft.directions,null,2);$('override-enabled').checked=page.draft.prompt_override!==null;$('prompt-override').value=page.draft.prompt_override||'';}
    $('prompt-mode-warning').hidden=!$('override-enabled').checked;
    $('job-details').replaceChildren();
    for(const job of page.jobs){if(job.state==='failed'){const b=node('button',`候補${job.candidate_index}の生成を再試行`,'secondary');b.addEventListener('click',()=>operation('retry:'+job.id,async()=>({url:`/api/jobs/${job.id}/retry`,body:{expected_state:'failed',explicit_extra_attempt:true}})).then(refresh).catch(e=>notice(e.message)));$('job-details').append(b);}}
    await checkHealth();
  }
  async function save(){page=await api(`/api/pages/${state.page}/draft`,'PATCH',{expected_version:editVersion,patch:patch()});dirty=false;$('dirty').textContent='保存済み';await refresh();}
  async function decide(decision){const c=candidate();page=await api(`/api/pages/${state.page}/review`,'POST',{expected_version:page.version,latest_round_id:page.latest_round_id,decision,candidate_id:decision==='accept'?c?.id:null,reason:$('reason').value,restore:$('restore-old').checked,clear_accepted:$('clear-accepted').checked});await refresh();}
  action('rebase-edit',()=>{editVersion=page.version;$('edit-conflict').hidden=true;$('dirty').textContent='未保存の変更があります（最新内容を確認済み）';});
  for(const link of document.querySelectorAll('[data-page-switch]'))link.addEventListener('click',async event=>{
    if(event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey||!dirty)return;
    event.preventDefault();
    try{await save();location.href=link.href;}catch(error){notice('移動する前に修正を保存できませんでした。'+error.message);}
  });
  action('save-instructions',async()=>{await save();notice('作画指示を保存しました。次の生成から適用します。');});
  action('normal-prompt-mode',()=>{$('override-enabled').checked=false;markDirty();});
  action('save-draft',save);
  action('accept',()=>decide('accept'),cannotAccept);
  action('reject-save',async()=>{if(dirty)await save();await decide('reject');});
  action('revise',async()=>{const result=await operation('revise:'+state.page,async()=>({url:`/api/pages/${state.page}/revise-and-generate`,body:{expected_version:editVersion,latest_round_id:page.latest_round_id,draft_patch:patch(),reason:$('reason').value,clear_accepted:$('clear-accepted').checked}}));const unchanged=JSON.stringify(patch())===JSON.stringify(result._sentBody.draft_patch);if(unchanged){dirty=false;$('dirty').textContent='保存済み';}else{dirty=true;notice('前回の再生成結果を確認しました。新しい編集内容は未保存のまま保持しています。');}await refresh();});
  action('preview-prompt',async()=>{const p=await api(`/api/pages/${state.page}/prompt`);if($('override-enabled').checked&&dirty&&!confirm('編集中の全文を保存済みの指示で置き換えますか？'))return;$('prompt-override').value=p.prompt;$('attachments').textContent='添付: '+p.attachments.map(a=>a.role+(a.key?' '+a.key:'')).join('、');markDirty();});
  action('copy-reason',()=>{$('correction').value+='\n'+$('reason').value;markDirty();});
  action('history-more',async()=>{const rows=await api(`/api/pages/${state.page}/history?before=${historyBefore}`);for(const r of rows){historyRounds.set(r.id,r);for(const c of r.candidates)historyCandidates.set(c.id,{...c,round_id:r.id,sequence:r.sequence});}if(rows.length)historyBefore=rows.at(-1).sequence;else $('history-more').hidden=true;await refresh();});
  $('candidate-select').addEventListener('change',()=>{userSelectedCandidate=true;$('restore-old').checked=false;showCandidate();});
  $('restore-old').addEventListener('change',showCandidate);
  $('source-select').addEventListener('change',()=>{$('source-image').src=`/api/assets/${$('source-select').value==='name'?page.draft.name_hash:page.draft.instruction_hash}`;$('source-image').alt=$('source-select').value==='name'?'入力のネーム画像':'入力の指示書画像';});
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
  finally{$('app-main').inert=false;$('app-main').setAttribute('aria-busy','false');$('app-loading').hidden=true;}
})();
