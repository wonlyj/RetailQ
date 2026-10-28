import { consoleRequest, newRequestKey } from './knowledge-api.js';
import * as view from './knowledge-views.js';
const $ = view.byId;
const initial = new URL(location.href);
const initialRun = initial.searchParams.get('run');
const state = { runId:/^[0-9a-f]{32}$/.test(initialRun || '') ? initialRun : null,
  kind:initial.searchParams.get('legacy') === '1' ? 'legacy' : 'review',
  tab:initial.searchParams.get('legacy') === '1' ? 'knowledge' : 'review', groupId:null,
  run:null, detail:null, summary:null, ready:false, busy:false, uncertain:false, operationId:null,
  pending:null, reading:false, again:false, delay:5000, offset:0, runOffset:0, sourceOffset:0,
  selected:new Set(), drafts:new Map(), catalog:[], catalogTotal:0 };
let timer, debounceTimer, categoryTimer;
function notify(text, error = false) {
  const target = $('miningDialog').open ? $('miningMessage') : $('sealDialog').open ? $('sealMessage') : $('globalMessage');
  view.message(text,error,target);
  if (state.uncertain && state.pending) target.append(view.button('重试相同请求',() => write(state.pending.path,state.pending.data)));
}
function schedule() {
  clearTimeout(timer);
  if (!document.hidden) timer = setTimeout(refresh,state.delay);
}
function selectRun(runId,kind = 'review') {
  state.runId = runId; state.kind = kind; state.groupId = null; state.detail = null; state.offset = 0;
  const url = new URL(location.href); url.searchParams.set('run',runId);
  if (kind === 'legacy') { url.searchParams.set('legacy','1'); state.tab = 'knowledge'; $('queueFilter').value = 'all'; }
  else url.searchParams.delete('legacy');
  history.replaceState(null,'',url); setTab(state.tab,false); refresh();
}
function setTab(tab,load = true) {
  clearTimeout(timer); state.tab = tab;
  for (const button of document.querySelectorAll('[data-tab]')) { const selected = button.dataset.tab === tab; button.classList.toggle('active',selected); button.setAttribute('aria-selected',String(selected)); }
  for (const name of ['review','knowledge','runs']) $(`${name}View`).hidden = name !== tab;
  $('groupFilters').hidden = tab === 'runs' || state.kind === 'legacy';
  if (load) { state.offset = 0; $('queueFilter').value = tab === 'review' ? 'pending' : 'all'; refresh(); }
}
function draftFor(detail) {
  const id = `${state.runId}:${detail.summary.group_id}`;
  if (!state.drafts.has(id)) {
    const suggestion = detail.summary.suggestion?.category || '';
    state.drafts.set(id,{existing:state.catalog.includes(suggestion) ? suggestion : '',fresh:state.catalog.includes(suggestion) ? '' : suggestion,reason:''});
  }
  return state.drafts.get(id);
}
function fieldValue(control,value) { if (control.value !== value) control.value = value; }
function editor() {
  const detail = state.detail;
  if (!detail) { $('approveButton').disabled = $('rejectButton').disabled = true; return; }
  const draft = draftFor(detail), pending = detail.summary.decision === 'pending';
  const merge = detail.summary.disposition === 'merge';
  $('categoryEditor').hidden = merge; $('mergeCategory').hidden = !merge;
  $('mergeCategory').textContent = `归并沿用目标分类“${detail.summary.category || '未知'}”及完整原文，不修改已有知识。`;
  fieldValue($('newCategory'),pending ? draft.fresh : detail.summary.category || '');
  fieldValue($('categorySelect'),draft.existing); fieldValue($('rejectReason'),draft.reason);
  const disabled = !pending || state.busy || state.uncertain || state.run?.summary.phase === 'sealed';
  for (const id of ['categoryQuery','categorySelect','newCategory','rejectReason','approveButton','rejectButton']) $(id).disabled = disabled;
  $('rejectReasonLabel').hidden = !pending;
  $('approveButton').textContent = merge ? '确认归并' : '通过并入库';
  $('actionHelp').textContent = disabled && !pending ? `当前应用：${detail.application_phase || '待继续'}` : merge ? '确认归并，不新增重复原文' : '通过后才落原文并补检索向量';
  $('finalCategory').textContent = `最终分类：${pending ? draft.existing || draft.fresh || '尚未选择' : detail.summary.category || '—'}`;
}
async function categories() {
  const query = $('categoryQuery').value;
  const page = await consoleRequest(`/categories?${new URLSearchParams({query,limit:'100'})}`);
  state.catalog = page.items; state.catalogTotal = page.total;
  const current = state.detail ? draftFor(state.detail).existing : '';
  const values = current && !page.items.includes(current) ? [current,...page.items] : page.items;
  $('categorySelect').replaceChildren(view.node('option','','请选择已有分类')); $('categorySelect').firstChild.value = '';
  for (const category of values) { const option = view.node('option','',category); option.value = category; $('categorySelect').append(option); }
  $('categorySelect').value = current;
  $('categoryCatalogInfo').textContent = `本次展示 ${page.items.length} / ${page.total} 个匹配分类；可搜索完整目录。`;
  editor();
}
async function refresh() {
  clearTimeout(timer);
  if (state.reading) { state.again = true; return; }
  state.reading = true;
  try {
    const [summary,runs] = await Promise.all([consoleRequest('/summary'),consoleRequest('/runs?limit=100')]);
    state.summary = summary; state.ready = true;
    if (!state.runId) { const candidate = summary.current_run_id || runs.items.find(r => r.kind === 'review')?.run_id; if (candidate) state.runId = candidate; }
    view.runPicker(runs,state.runId ? `${state.kind}:${state.runId}` : '');
    if (state.operationId) {
      try {
        const operation = await consoleRequest(`/operations/${state.operationId}`);
        if (['succeeded','failed','interrupted','sealed'].includes(operation.phase)) {
          state.busy = false; state.uncertain = false; state.pending = null; state.operationId = null;
          notify(operation.error?.message || (operation.phase === 'succeeded' ? '操作已完成，请查看实际审核结果与去向。' : view.phaseLabel(operation.phase)),Boolean(operation.error));
        }
      } catch (error) { if (error.status !== 404 || !state.uncertain) throw error; }
    }
    const chosen = `${state.kind}:${state.runId}`;
    if (state.runId && state.kind === 'review') {
      const run = await consoleRequest(`/runs/${state.runId}`);
      if (chosen !== `${state.kind}:${state.runId}`) { state.again = true; return; }
      state.run = run;
      const filters = new URLSearchParams({query:$('searchInput').value,disposition:$('queueFilter').value,
        category:$('categoryFilter').value,offset:String(state.offset),limit:'20'});
      if (run.sources !== null) {
        const groups = await consoleRequest(`/runs/${state.runId}/groups?${filters}`);
        if (!state.groupId && groups.items.length) state.groupId = groups.items[0].group_id;
        view.queue(groups,state.groupId,id => { state.groupId = id; refresh(); });
        view.pager(groups,'queue'); view.pager(groups,'dest');
        if (state.tab === 'knowledge') {
          const details = await Promise.all(groups.items.map(g => consoleRequest(`/runs/${state.runId}/groups/${g.group_id}`)));
          view.destinations(details,openChunk);
        }
        if (state.groupId) {
          try { state.detail = await consoleRequest(`/runs/${state.runId}/groups/${state.groupId}`); }
          catch (error) { if (error.status !== 404) throw error; state.detail = null; state.groupId = null; }
        } else state.detail = null;
        view.group(state.detail,run,openChunk); editor();
      } else {
        state.detail = null; view.empty($('queue'),'审计记录损坏或缺失，请核查或终止并封存。'); view.group(null,null,openChunk); editor();
      }
      $('runProgress').textContent = `${view.phaseLabel(run.summary.phase)} · 已完成批次 ${run.completed_batches ?? '未知'} / ${run.total_batches ?? '未知'}`;
      $('runActions').hidden = false;
      $('runState').textContent = `任务 ${state.runId} · ${view.phaseLabel(run.summary.phase)}${run.summary.error ? `；${run.summary.error.message}` : ''}`;
      const terminal = ['completed','sealed'].includes(run.summary.phase);
      $('resumeRun').hidden = terminal || run.summary.phase === 'waiting_review'; $('retryVectors').hidden = terminal || !run.summary.counters?.pending_vectors;
      $('sealRun').hidden = run.summary.phase === 'sealed';
      for (const id of ['resumeRun','retryVectors']) $(id).disabled = state.busy || state.uncertain;
      $('sealRun').disabled = state.busy && state.pending?.path.endsWith('/seal');
    } else if (state.runId) {
      state.run = null; state.detail = null; $('runActions').hidden = true;
      view.legacy(await consoleRequest(`/legacy-runs/${state.runId}`),openChunk);
      $('runProgress').textContent = '旧版任务只读；缺失去向保持未知。';
    } else {
      state.run = null; state.detail = null; view.empty($('queue'),'尚无审核任务。选择历史对话，准备待审核知识。');
      view.group(null,null,openChunk); $('runActions').hidden = true;
    }
    if (state.tab === 'runs') { const page = await consoleRequest(`/runs?offset=${state.runOffset}&limit=20`); view.history(page,selectRun); view.pager(page,'runs'); }
    view.stats(summary,state.run);
    $('consoleStatus').textContent = state.busy ? '操作执行中' : state.run ? view.phaseLabel(state.run.summary.phase) : '本机知识空间';
    $('startMining').textContent = summary.current_run_id ? '继续审核 / 查看任务' : '＋ 开始挖掘';
    $('startMining').disabled = state.busy || state.uncertain;
    state.delay = state.busy || state.operationId ? 1000 : 5000;
    if (summary.error) notify(summary.error.message,true);
  } catch (error) {
    if (error.name !== 'AbortError') {
      notify(`${error.message}${error.code ? `（${error.code}）` : ''}`,true);
      if (error.status === 503) { state.ready = false; $('startMining').disabled = true; }
      $('consoleStatus').textContent = '状态暂不可用'; state.delay = Math.min(30000,state.delay * 2);
    }
  } finally {
    state.reading = false;
    if (state.again) { state.again = false; state.delay = 1; }
    schedule();
  }
}
async function write(path,data) {
  const signature = JSON.stringify([path,data]);
  const isSeal = path.endsWith('/seal');
  if (state.busy && (!isSeal || state.pending?.path === path)) return;
  // Explicit sealing supersedes polling/retrying this run's earlier command;
  // the server drains it at a safe boundary and keeps the immutable audit.
  if (isSeal && state.pending?.path !== path) { state.pending = null; state.uncertain = false; }
  if (state.pending && state.pending.signature !== signature) { notify('先确认上一请求状态，再提交其他操作。',true); return; }
  if (!state.pending) state.pending = {path,data,signature,key:newRequestKey()};
  const pending = state.pending;
  state.busy = true; editor(); $('startMining').disabled = true; $('submitMining').disabled = true;
  try {
    const operation = await consoleRequest(path,{method:'POST',body:{request_key:pending.key,...data}});
    state.operationId = operation.operation_id; state.uncertain = false;
    if (path === '/runs') { state.tab = 'review'; $('queueFilter').value = 'pending'; selectRun(operation.run_id,'review'); }
    $('miningDialog').close(); $('sealDialog').close(); notify('请求已受理，正在读取实际处理状态。');
  } catch (error) {
    state.busy = false;
    if (!error.status) { state.uncertain = true; state.operationId = pending.key; }
    else { state.pending = null; state.uncertain = false; }
    notify(error.message,true); editor();
  } finally { state.delay = 1000; refresh(); }
}
async function decision(kind) {
  const detail = state.detail; if (!detail || state.busy || state.uncertain) return;
  const draft = draftFor(detail), category = draft.existing || draft.fresh;
  if (kind === 'approve' && detail.summary.disposition !== 'merge' && !category.trim()) { notify('请选择已有分类，或确认一个新分类。',true); return; }
  const data = {expected_revision:detail.summary.revision,decision:kind};
  if (kind === 'approve' && detail.summary.disposition !== 'merge') data.category = category;
  if (kind === 'reject' && draft.reason) data.reason = draft.reason;
  await write(`/runs/${state.runId}/groups/${detail.summary.group_id}/decision`,data);
}
async function openChunk(id) {
  try { view.chunk(await consoleRequest(`/chunks/${id}`)); if (!$('chunkDialog').open) $('chunkDialog').showModal(); }
  catch (error) { notify(error.message,true); }
}
async function openSource(id) {
  try { view.snapshot(await consoleRequest(`/sources/${id}`)); if (!$('chunkDialog').open) $('chunkDialog').showModal(); }
  catch (error) { notify(error.message,true); }
}
function selectionChanged() {
  $('selectionCount').textContent = `已选 ${state.selected.size} / 100`;
  $('submitMining').disabled = !state.selected.size || state.selected.size > 100 || state.busy || state.uncertain;
}
async function loadSources() {
  try {
    const query = new URLSearchParams({query:$('sourceSearch').value,eligibility:$('sourceEligibility').value,offset:String(state.sourceOffset),limit:'20'});
    const page = await consoleRequest(`/sources?${query}`);
    view.sources(page,state.selected,(id,checked) => { checked ? state.selected.add(id) : state.selected.delete(id); selectionChanged(); },openSource);
    view.pager(page,'sources'); selectionChanged();
  } catch (error) { notify(error.message,true); }
}
function filterChanged() { clearTimeout(debounceTimer); debounceTimer = setTimeout(() => { state.offset = 0; refresh(); },200); }
for (const button of document.querySelectorAll('[data-tab]')) button.addEventListener('click',() => setTab(button.dataset.tab));
$('runSelect').addEventListener('change',() => { if ($('runSelect').value) { const [kind,id] = $('runSelect').value.split(':'); selectRun(id,kind); } });
$('refreshButton').addEventListener('click',refresh);
for (const id of ['searchInput','categoryFilter']) $(id).addEventListener('input',filterChanged);
$('queueFilter').addEventListener('change',filterChanged);
$('approveButton').addEventListener('click',() => decision('approve')); $('rejectButton').addEventListener('click',() => decision('reject'));
$('categorySelect').addEventListener('change',() => { if (!state.detail) return; const draft = draftFor(state.detail); draft.existing = $('categorySelect').value; if (draft.existing) draft.fresh = ''; editor(); });
$('newCategory').addEventListener('input',() => { if (!state.detail) return; const draft = draftFor(state.detail); draft.fresh = $('newCategory').value; if (draft.fresh) draft.existing = ''; editor(); });
$('rejectReason').addEventListener('input',() => { if (state.detail) draftFor(state.detail).reason = $('rejectReason').value; });
$('categoryQuery').addEventListener('input',() => { clearTimeout(categoryTimer); categoryTimer = setTimeout(() => categories().catch(error => notify(error.message,true)),200); });
for (const [prefix,field,load] of [['queue','offset',refresh],['dest','offset',refresh],['runs','runOffset',refresh],['sources','sourceOffset',loadSources]]) {
  $(`${prefix}Prev`).addEventListener('click',() => { state[field] = Math.max(0,state[field] - 20); load(); });
  $(`${prefix}Next`).addEventListener('click',() => { state[field] += 20; load(); });
}
$('startMining').addEventListener('click',() => {
  if (state.summary?.current_run_id) { selectRun(state.summary.current_run_id); setTab('review'); return; }
  state.selected.clear(); state.sourceOffset = 0; $('miningMessage').hidden = true; $('miningDialog').showModal(); loadSources();
});
$('closeMining').addEventListener('click',() => $('miningDialog').close());
$('submitMining').addEventListener('click',() => write('/runs',{conversation_ids:[...state.selected]}));
for (const id of ['sourceSearch','sourceEligibility']) $(id).addEventListener(id === 'sourceSearch' ? 'input' : 'change',() => { state.sourceOffset = 0; loadSources(); });
$('closeChunk').addEventListener('click',() => $('chunkDialog').close());
$('resumeRun').addEventListener('click',() => write(`/runs/${state.runId}/resume`,{}));
$('retryVectors').addEventListener('click',() => write(`/runs/${state.runId}/retry-vectors`,{}));
$('sealRun').addEventListener('click',() => { $('sealRunId').textContent = state.runId; $('sealAcknowledge').checked = false; $('confirmSeal').disabled = true; $('sealMessage').hidden = true; $('sealDialog').showModal(); });
$('sealAcknowledge').addEventListener('change',() => { $('confirmSeal').disabled = !$('sealAcknowledge').checked; });
$('cancelSeal').addEventListener('click',() => $('sealDialog').close());
$('confirmSeal').addEventListener('click',() => { if ($('sealAcknowledge').checked) write(`/runs/${state.runId}/seal`,{confirm_run_id:state.runId}); });
document.addEventListener('visibilitychange',() => { clearTimeout(timer); if (!document.hidden) refresh(); });
window.addEventListener('pagehide',() => { clearTimeout(timer); clearTimeout(debounceTimer); clearTimeout(categoryTimer); });
setTab(state.tab,false);
await refresh();
if (state.ready) await categories().catch(error => notify(error.message,true));
