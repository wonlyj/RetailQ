const uuid = /^[0-9a-f]{32}$/;
const statuses = new Set(['offered', 'executing', 'unknown', 'completed', 'failed']);

export function restoredActions(message, conversationId) {
  if (message.conversationId !== conversationId) throw new Error('操作所属会话不匹配。');
  return validateActions({ message_id: message.serverMessageId, conversation_id: conversationId,
    turn_id: message.workflowTurnId, actions: message.actions || [] });
}

export class WorkflowFrames {
  constructor() { this.meta = null; this.actions = null; this.messageId = null; this.hasSources = false; }
  accept(event, payload) {
    if (event === 'meta') {
      if (this.meta || !Number.isSafeInteger(payload.conversation_id) || payload.conversation_id < 1 || !uuid.test(payload.turn_id || '')) throw new Error('工作流会话标识异常。');
      this.meta = { conversation_id: payload.conversation_id, turn_id: payload.turn_id };
    } else if (['workflow', 'actions', 'sources'].includes(event)) {
      if (!this.meta || payload.turn_id !== this.meta.turn_id) throw new Error('工作流轮次不匹配。');
      if (event === 'workflow') return;
      if (!/^[1-9]\d*$/.test(payload.message_id || '') || (this.messageId && this.messageId !== payload.message_id)) throw new Error('回复来源消息不匹配。');
      this.messageId = payload.message_id;
      if (event === 'sources') {
        if (this.hasSources) throw new Error('引用来源重复。');
        this.hasSources = true;
      } else {
        if (this.actions !== null || payload.conversation_id !== this.meta.conversation_id) throw new Error('可选操作帧重复或会话不匹配。');
        this.actions = validateActions(payload);
      }
    }
  }
  finish(reason) {
    if (reason === 'stop' && (!this.meta || this.actions === null)) throw new Error('回复操作信息未完整接收，请继续咨询或重新查询。');
  }
}

export function validateActions(payload) {
  if (!payload || !/^[1-9]\d*$/.test(payload.message_id || '') ||
      !Number.isSafeInteger(payload.conversation_id) || payload.conversation_id < 1 ||
      !uuid.test(payload.turn_id || '') || !Array.isArray(payload.actions) || payload.actions.length > 2) {
    throw new Error('可选操作来源格式异常。');
  }
  const kinds = new Set();
  return payload.actions.map(action => {
    if (!action || !['handoff', 'ticket'].includes(action.kind) || kinds.has(action.kind) ||
        !uuid.test(action.action_id || '') || action.origin_assistant_message_id !== payload.message_id ||
        !statuses.has(action.status)) throw new Error('可选操作与原回复不匹配。');
    kinds.add(action.kind);
    return { action_id: action.action_id, kind: action.kind, status: action.status,
      origin_assistant_message_id: action.origin_assistant_message_id, result: action.result || null };
  });
}

function applyResult(action, result) {
  if (!result || result.action_id !== action.action_id || !statuses.has(result.status) ||
      (result.status === 'completed' && (!result.ticket || typeof result.ticket.ticket_no !== 'string' ||
        !/^[A-Za-z0-9_-]{1,32}$/.test(result.ticket.ticket_no)))) throw new Error('工单结果尚无法确认，请核查。');
  action.status = result.status;
  action.result = result.ticket;
  action.verified = true;
}

async function checkTicket(message, action, context) {
  if (action.localPending) return;
  action.localPending = true; action.needsCheck = false;
  context.changed();
  try {
    applyResult(action, await context.request(`/api/conversations/${message.conversationId}/actions/${action.action_id}/ticket?user_id=${encodeURIComponent(context.userId)}`));
  } catch (error) {
    action.status = 'unknown'; action.verified = false;
    context.notify(error.message || '工单结果尚未确认。');
  } finally { action.localPending = false; context.changed(); }
}

function openConfirmation(message, action, context) {
  if (action.localPending || context.isBusy()) return;
  const dialog = document.createElement('dialog'); dialog.className = 'workflow-action-dialog';
  const form = document.createElement('form');
  const title = document.createElement('h2');
  title.textContent = action.kind === 'handoff' ? '确认转人工' : '确认创建工单';
  const note = document.createElement('p');
  note.textContent = action.kind === 'handoff' ? '本章将展示模拟人工客服问候，您仍可继续对话。' : '只记录您填写的问题，不会同时转人工或执行退款。';
  form.append(title, note);
  let description, type;
  if (action.kind === 'ticket') {
    const label = document.createElement('label'); label.textContent = '需要记录的问题';
    description = document.createElement('textarea'); description.required = true; description.maxLength = 2000;
    description.placeholder = '请说明订单号、遇到的问题和希望获得的协助。';
    description.addEventListener('input', () => description.setCustomValidity(''));
    label.append(description);
    const typeLabel = document.createElement('label'); typeLabel.textContent = '工单类型';
    type = document.createElement('select');
    for (const value of ['投诉', '售后', '咨询']) {
      const option = document.createElement('option'); option.value = value; option.textContent = value; type.append(option);
    }
    type.value = '投诉'; typeLabel.append(type); form.append(label, typeLabel);
  }
  const controls = document.createElement('div'); controls.className = 'workflow-confirm-controls';
  const cancel = document.createElement('button'); cancel.type = 'button'; cancel.dataset.cancel = 'yes'; cancel.textContent = '取消';
  const confirm = document.createElement('button'); confirm.type = 'submit'; confirm.textContent = '确认';
  controls.append(cancel, confirm); form.append(controls); dialog.append(form); document.body.append(dialog);
  let submitting = false;
  const close = () => { dialog.close(); dialog.remove(); };
  cancel.addEventListener('click', close);
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submitting || action.localPending || context.isBusy()) return;
    if (description && (!description.value.trim() || description.value.trim().length > 2000)) {
      description.setCustomValidity('请填写1至2000字的问题描述。'); description.reportValidity(); return;
    }
    submitting = true;
    if (action.kind === 'handoff') {
      message.handoffSimulated = true; close();
      context.simulateHandoff(['已转接人工客服', '您好,我是客服小猫,请问有什么可以帮您的']);
      context.changed(); return;
    }
    const body = { user_id: context.userId, description: description.value.trim(), ticket_type: type.value };
    action.localPending = true; action.status = 'executing'; close(); context.changed();
    try {
      const path = `/api/conversations/${message.conversationId}/actions/${action.action_id}/ticket`;
      applyResult(action, await context.request(path, { method: 'POST', body }));
    } catch (error) {
      action.status = (error.status === 409 && error.code === 'conversation_busy') || error.status === 422 ? 'offered' : 'unknown';
      action.verified = action.status === 'offered'; action.result = null;
      context.notify(error.message || '工单提交结果尚未确认，请先核查。');
    } finally { action.localPending = false; context.changed(); }
  });
  dialog.showModal();
  description?.focus();
}

export function renderActions(container, message, context) {
  container.replaceChildren();
  if (message.role !== 'assistant' || message.status !== 'complete' || !message.serverMessageId || !message.conversationId) return;
  for (const action of message.actions || []) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'workflow-action-button';
    button.dataset.kind = action.kind;
    if (action.kind === 'handoff') {
      button.textContent = message.handoffSimulated ? '已展示人工客服' : '转人工';
      button.disabled = Boolean(message.handoffSimulated) || context.isBusy();
      button.addEventListener('click', () => openConfirmation(message, action, context));
    } else {
      const completed = action.verified && action.status === 'completed';
      button.textContent = action.localPending ? '正在核查/提交…' : completed ? `工单 ${action.result.ticket_no} · 待处理` :
        action.status === 'offered' && action.verified ? '建工单' : '核查工单';
      button.disabled = Boolean(action.localPending) || completed || context.isBusy();
      button.addEventListener('click', () => action.status === 'offered' && action.verified ?
        openConfirmation(message, action, context) : checkTicket(message, action, context));
    }
    container.append(button);
    if (action.kind === 'ticket' && action.needsCheck && !action.localPending) {
      void Promise.resolve().then(() => checkTicket(message, action, context));
    }
  }
}
