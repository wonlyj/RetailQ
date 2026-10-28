export const byId = id => document.getElementById(id);
let queueSignature, groupSignature;
export function node(tag, className = '', text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
export function button(text, action, className = 'secondary') {
  const result = node('button', className, text); result.type = 'button';
  result.addEventListener('click', action); return result;
}
export const phaseLabel = phase => ({ queued:'已受理', extraction:'抽取中', dedup:'去重中', classification:'分类建议中',
  waiting_review:'等待人工审核', applying:'应用审核决定', vectorize:'待补检索向量', completed:'已完成',
  failed:'执行未完成', interrupted:'中断待继续', conflict:'需要核查', sealed:'已终止并封存',
  running:'执行中', succeeded:'操作完成', complete:'旧版已完成' }[phase] || phase || '未知');
const decisionLabel = value => ({pending:'待审核', approved:'已批准', rejected:'已拒绝'}[value] || '未知');
const dispositionLabel = value => ({new:'新增知识',same_group:'同组归并',merge_existing:'归并已有',human_rejected:'人工拒绝'}[value] || '待确证');
const sourceId = member => /^conversation:([1-9][0-9]*):/.exec(member.source_ref)?.[1];
export function message(text = '', error = false, container = byId('globalMessage')) {
  container.replaceChildren(); container.textContent = text;
  container.classList.toggle('error', error); container.hidden = !text;
}
export function empty(container, text) { container.replaceChildren(node('p','empty',text)); }
export function pager(page, prefix) {
  byId(`${prefix}Prev`).disabled = !page || page.offset === 0;
  byId(`${prefix}Next`).disabled = !page || page.offset + page.items.length >= page.total;
  byId(`${prefix}Page`).textContent = page ? `共 ${page.total} 项 · ${page.total ? page.offset + 1 : 0}–${page.offset + page.items.length}` : '—';
}
export function stats(summary, run) {
  const counters = run?.summary?.counters;
  for (const [id,key] of [['extractedCount','extracted'],['pendingCount','review_pending'],['appliedCount','applied']]) {
    byId(id).textContent = counters?.[key] ?? '—';
  }
  byId('chunkCount').textContent = summary?.chunks ?? '—';
}
export function runPicker(page, selected) {
  const control = byId('runSelect'); const signature = JSON.stringify([page.items.map(r => [r.kind,r.run_id,r.phase]),selected]);
  if (control.dataset.signature === signature) return;
  control.dataset.signature = signature;
  control.replaceChildren(node('option','',page.items.length ? '选择任务' : '尚无任务')); control.firstChild.value = '';
  for (const run of page.items) {
    const option = node('option','',`${run.kind === 'legacy' ? '旧版' : '审核'} · ${run.run_id} · ${phaseLabel(run.phase)}`);
    option.value = `${run.kind}:${run.run_id}`; control.append(option);
  }
  control.value = selected;
}
export function queue(page, selected, select) {
  const signature = JSON.stringify([page.items,selected]);
  if (signature === queueSignature) return;
  queueSignature = signature;
  const container = byId('queue'); container.replaceChildren();
  if (!page.items.length) return empty(container,'当前筛选下没有审核组。可在“知识去向”查看已处理记录。');
  for (const group of page.items) {
    const row = button('', () => select(group.group_id), `review-item${group.group_id === selected ? ' selected' : ''}`);
    const top = node('div','item-top'); top.append(node('span','item-question',group.question));
    const label = group.decision === 'pending' ? (group.disposition === 'merge' ? '拟归并已有' : '拟新增知识') : decisionLabel(group.decision);
    top.append(node('span',`badge ${group.decision === 'pending' ? 'amber' : group.decision === 'rejected' ? 'gray' : ''}`,label));
    const bottom = node('div','item-bottom');
    bottom.append(node('span','',`组 ${group.group_id}`),node('b','',`${group.decision === 'pending' ? '建议' : '最终'}分类：${group.category || '需人工选择'}`));
    row.append(top,bottom); container.append(row);
  }
}
function traceStep(title, text, action, future = false) {
  const step = node('div',`step${future ? ' future' : ''}`);
  step.append(node('strong','',title),node('p','',text));
  if (action) step.append(button(action.label,action.run));
  return step;
}
function bubbles(messages, container) {
  for (const message of messages) {
    const bubble = node('div',`bubble ${message.role === 'assistant' ? 'assistant' : ''}`);
    bubble.append(node('small','',`${message.role === 'user' ? '用户' : message.role === 'assistant' ? '客服' : '工具'} · 消息 #${message.id}`),node('pre','',message.content ?? '（无正文）'));
    container.append(bubble);
  }
}
export function group(detail, run, openChunk) {
  const signature = JSON.stringify([detail,run?.sources]);
  if (signature === groupSignature) return;
  groupSignature = signature;
  byId('groupEmpty').hidden = Boolean(detail); byId('groupContent').hidden = !detail;
  const trace = byId('trace'), sources = byId('sourcePanels'); trace.replaceChildren(); sources.replaceChildren();
  if (!detail) { empty(trace,'尚未选择审核组'); return; }
  byId('reviewStatus').textContent = decisionLabel(detail.summary.decision);
  byId('reviewStatus').className = `badge ${detail.summary.decision === 'pending' ? 'amber' : detail.summary.decision === 'rejected' ? 'gray' : ''}`;
  byId('questionList').replaceChildren(...detail.questions.map(q => node('p','',q)));
  byId('answerText').textContent = detail.answer;
  const suggestion = detail.summary.suggestion;
  byId('suggestion').textContent = suggestion ? `${suggestion.recommend_reject ? '建议谨慎入库。' : '分类建议：'}${suggestion.category || '需人工选择'}。${suggestion.reason}` : '尚无分类建议，请核对原文。';
  const rejected = detail.summary.decision === 'rejected';
  trace.append(traceStep(`来源：${detail.members.length} 条真实问答`,'展开下方各来源，可核对冻结的原始消息。'));
  trace.append(traceStep(`暂存 QA：${detail.members.map(m => `#${m.staging_id}`).join('、')}`,'组内问法与完整答案保留，不在页面改写。'));
  trace.append(traceStep(decisionLabel(detail.summary.decision),detail.summary.decision === 'pending' ? '分类建议尚未生效，等待本机人工确认。' : `分类：${detail.summary.category || '无'}；应用状态：${detail.application_phase || '尚待应用'}`));
  if (rejected) {
    trace.append(traceStep(detail.application_phase === 'applied' ? '人工拒绝，不生成 chunk' : '拒绝决定待确证','原会话和现存审核记录保留。'));
  } else if (detail.target_confirmed) {
    trace.append(traceStep(`已确认目标 chunk #${detail.target_chunk_id}`,`最终分类：${detail.summary.category || '—'}`,{label:'查看知识原文',run:() => openChunk(detail.target_chunk_id)}));
  } else if (detail.planned_target_id && detail.summary.disposition === 'merge' && !detail.application_phase) {
    trace.append(traceStep(`拟归并已有 chunk #${detail.planned_target_id}`,'需要人工确认；沿用目标原文与分类。',{label:'查看拟归并目标',run:() => openChunk(detail.planned_target_id)},true));
  } else {
    trace.append(traceStep(detail.planned_target_id ? `预定 ID #${detail.planned_target_id}，尚未确证` : '尚未确认 chunk 去向','预定 ID 不代表已入库。',null,true));
  }
  const pendingMerge = detail.summary.disposition === 'merge' && !detail.application_phase;
  trace.append(traceStep(rejected ? '不新增检索向量' : pendingMerge ? '归并不新增重复向量' : detail.vectorize_status === 'done' ? '入库记录：已向量化' : detail.vectorize_status === 'pending' ? '已落原文，待补检索向量' : '检索向量尚未入库',detail.error?.message || (rejected ? '不会因本组拒绝而删除已有知识。' : pendingMerge ? '已有目标按其原入库状态继续服务；这里只等待新来源关联的审核。' : '以实际确认状态为准。'),null,!detail.vectorize_status));
  for (const member of detail.members) {
    const id = sourceId(member), source = run?.sources?.find(s => s.conversation_id === id);
    const panel = node('details'); panel.open = detail.members.length === 1;
    panel.append(node('summary','',`${id ? `会话 #${id}` : member.source_ref} · 暂存 QA #${member.staging_id}`));
    const user = node('div','bubble'); user.append(node('small','','真实问法'),node('pre','',member.question));
    const answer = node('div','bubble assistant'); answer.append(node('small','','完整来源答案'),node('pre','',member.answer));
    panel.append(user,answer);
    const disposition = detail.dispositions.find(d => d.staging_id === member.staging_id);
    panel.append(node('p','disposition',disposition ? `${dispositionLabel(disposition.reason)}${disposition.chunk_id ? ` → chunk #${disposition.chunk_id}` : ' → 无目标 chunk'}` : '去向尚待应用与确证'));
    panel.append(node('small','field-help',`证据消息：${member.message_ids.map(i => `#${i}`).join('、')}`));
    if (source) {
      const full = node('details'); full.append(node('summary','',`查看完整冻结会话（${source.messages.length} 条消息）`));
      const body = node('div'); full.append(body);
      full.addEventListener('toggle',() => { if (full.open && !body.childNodes.length) bubbles(source.messages,body); });
      panel.append(full);
    }
    sources.append(panel);
  }
}
function table(headers) {
  const result = node('table','table'), thead = node('thead'), row = node('tr'), body = node('tbody');
  row.append(...headers.map(h => node('th','',h))); thead.append(row); result.append(thead,body);
  return { result, body };
}
export function destinations(details, openChunk) {
  const {result,body} = table(['来源 / 暂存','真实问法','处理去向','分类','检索状态']);
  for (const detail of details) for (const member of detail.members) {
    const row = node('tr'), target = node('td'), disposition = detail.dispositions.find(d => d.staging_id === member.staging_id);
    if (detail.target_confirmed) target.append(button(`${dispositionLabel(disposition?.reason)} → #${detail.target_chunk_id}`,() => openChunk(detail.target_chunk_id)));
    else target.textContent = detail.summary.decision === 'rejected' ? (detail.application_phase === 'applied' ? '人工拒绝，无 chunk' : '拒绝决定待确证') : detail.application_phase ? '已作决定，去向待确证' : '待审核';
    row.append(node('td','id',`${sourceId(member) ? `会话 #${sourceId(member)}` : member.source_ref}\nQA #${member.staging_id}`),node('td','question',member.question),target,
      node('td','',detail.summary.category || '—'),node('td','',detail.vectorize_status === 'done' ? '已向量化' : detail.vectorize_status === 'pending' ? '待补向量' : '—'));
    body.append(row);
  }
  byId('destinations').replaceChildren(result);
  if (!details.length) empty(byId('destinations'),'当前筛选下没有去向记录。');
}
export function legacy(detail, openChunk) {
  const container = byId('destinations');
  container.replaceChildren(node('p','empty',`旧版任务：${phaseLabel(detail.phase)}。${detail.missing_provenance} 个组未保存可确证去向，不推测关联。`));
  for (const target of detail.explicit_targets) container.append(button(`旧版明确目标：chunk #${target.chunk_id}`,() => openChunk(target.chunk_id)));
}
export function history(page, select) {
  const {result,body} = table(['任务','阶段','抽取 / 待审 / 应用','操作']);
  for (const run of page.items) {
    const row = node('tr'), action = node('td'), c = run.counters;
    action.append(button('查看',() => select(run.run_id,run.kind)));
    row.append(node('td','id',`${run.kind === 'legacy' ? '旧版' : '审核'} · ${run.run_id}`),
      node('td','',`${phaseLabel(run.phase)}${run.error ? `；${run.error.message}` : ''}`),
      node('td','',c ? `${c.extracted} / ${c.review_pending} / ${c.applied}` : '未知 / 未保存'),action); body.append(row);
  }
  byId('runHistory').replaceChildren(result); if (!page.items.length) empty(byId('runHistory'),'尚无挖掘任务。');
}
export function sources(page, selected, change, inspect) {
  const container = byId('sourceOptions'); container.replaceChildren();
  for (const source of page.items) {
    const row = node('div',`source-option${source.eligibility ? '' : ' unavailable'}`), check = node('input');
    check.type = 'checkbox'; check.checked = selected.has(source.id); check.disabled = !source.eligibility;
    check.setAttribute('aria-label',`选择会话 ${source.id}`); check.addEventListener('change',() => change(source.id,check.checked));
    const text = node('div'); text.append(node('span','',`会话 #${source.id} · ${source.status}`),
      node('small','',`${source.message_count} 条消息 · ${source.eligibility ? '可参与挖掘' : source.reason}`),
      button('查看当前对话',() => inspect(source.id)));
    row.append(check,text); container.append(row);
  }
  if (!page.items.length) empty(container,'没有符合筛选条件的会话。');
}
export function snapshot(source) { byId('chunkTitle').textContent = `当前会话 #${source.conversation_id}`; byId('chunkBody').replaceChildren(); bubbles(source.messages,byId('chunkBody')); }
export function chunk(detail) {
  const row = detail.record; byId('chunkTitle').textContent = `知识原文 · chunk #${row.id}`;
  const fields = [['分类',row.draft.category],['全部问法',row.draft.questions],['完整答案',row.draft.answer],
    ['向量状态',row.vectorize_status === 'done' ? '已向量化（按入库记录）' : '待向量化'],['章节路径',row.draft.section_path || '—'],
    ['内容类型',row.draft.content_type || '—'],['关键条款',row.draft.is_key_clause ? '是' : '否'],
    ['前块 / 后块',`${row.prev_chunk_id ?? '—'} / ${row.next_chunk_id ?? '—'}`]];
  const dl = node('dl');
  for (const [label,value] of fields) { const field = node('div','chunk-field'); field.append(node('dt','',label),node('dd','',value)); dl.append(field); }
  byId('chunkBody').replaceChildren(dl);
}
