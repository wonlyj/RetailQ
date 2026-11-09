import { renderActions, validateActions, restoredActions, WorkflowFrames } from './actions.js';
import { renderOrderSelector, validateSelector } from './order-selector.js';
  "use strict";
  const $ = (id) => document.getElementById(id);
  const storageKey = "retailq.chat.v1";
  let sessions = [];
  let activeId;
  let busy = false;
  let controller = null;
  let toastTimer;
  let persistenceAvailable = true;
  const uid = () => crypto.randomUUID();
  const identityKey = 'retailq.user.v1';
  let userId;
  try {
    userId = localStorage.getItem(identityKey) || sessionStorage.getItem(identityKey) || `demo-${uid()}`;
    localStorage.setItem(identityKey, userId);
    sessionStorage.setItem(identityKey, userId);
  } catch { userId = `demo-${uid()}`; }
  const current = () => sessions.find((session) => session.id === activeId);
  function validCitations(value) {
    if (!Array.isArray(value) || value.length > 10) return [];
    const numbers = new Set();
    for (const item of value) {
      if (!item || !Number.isInteger(item.number) || item.number < 1 || item.number > 10 || numbers.has(item.number) ||
          typeof item.chunk_id !== 'string' || !/^[1-9]\d*$/.test(item.chunk_id) ||
          typeof item.text !== 'string' || typeof item.section_path !== 'string' ||
          typeof item.source_key !== 'string' || typeof item.source_version !== 'string') return [];
      numbers.add(item.number);
    }
    return value;
  }
  function validFeedback(message, feedback) {
    return feedback && ['up', 'down'].includes(feedback.value) && feedback.messageId === message.id &&
      feedback.turnId === message.turnId && typeof feedback.createdAt === 'string' &&
      !Number.isNaN(Date.parse(feedback.createdAt)) &&
      (feedback.conversationId === null || Number.isSafeInteger(feedback.conversationId));
  }
  function executionNote(message) {
    return (message.toolTrace || []).some((t) => t.name === 'create_ticket')
      ? '工单操作可能已经提交，请核查后再操作。'
      : (message.toolTrace || []).length ? '已完成的查询记录会保留。' : '本轮回复未完成，可继续咨询。';
  }
  const icon = (name) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", `#i-${name}`);
    svg.setAttribute("aria-hidden", "true");
    svg.append(use);
    return svg;
  };
  function notify(text) {
    $("toast").textContent = text;
    $("toast").classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $("toast").classList.remove("visible"), 2800);
  }
  function persist() {
    if (!persistenceAvailable) return;
    try {
      sessionStorage.setItem(storageKey, JSON.stringify({ sessions, activeId }));
    } catch {
      persistenceAvailable = false;
      notify("会话暂时无法保存，仍可继续本次咨询。");
    }
  }
  function newSession() {
    const session = { id: uid(), title: "新的购物咨询", messages: [] };
    sessions.unshift(session);
    sessions = sessions.slice(0, 20);
    activeId = session.id;
    return session;
  }
  function restore() {
    try {
      const stored = JSON.parse(sessionStorage.getItem(storageKey));
      sessions = (Array.isArray(stored?.sessions) ? stored.sessions : []).filter((session) =>
        typeof session.id === "string" && typeof session.title === "string" && Array.isArray(session.messages)
      ).slice(0, 20).map((session) => ({
        ...session,
        messages: session.messages.filter((message) =>
          typeof message.id === "string" && typeof message.turnId === "string" &&
          typeof message.text === "string" && ["user", "assistant"].includes(message.role)
        ).slice(-200).map((message) => {
          const restored = { ...message, citations: validCitations(message.citations),
            feedback: validFeedback(message, message.feedback) ? message.feedback : null };
          try {
            restored.actions = restoredActions(message, session.conversationId).map(action =>
                ({ ...action, verified: false, needsCheck: action.kind === 'ticket' }));
          } catch { restored.actions = []; }
          return message.status === "pending"
            ? { ...restored, status: "stopped", detail: `上次回复未完成。${executionNote(message)}`,
                toolTrace: (message.toolTrace || []).map((trace) => trace.status === 'running' ? {...trace, status: 'unknown'} : trace) }
            : restored;
        }),
      }));
      activeId = stored?.activeId;
    } catch { sessions = []; }
    if (!sessions.length) newSession();
    if (!current()) activeId = sessions[0].id;
  }
  function closeNav() {
    $("app").classList.remove("nav-open");
    $("navBackdrop").hidden = true;
    $("menuButton").setAttribute("aria-expanded", "false");
    syncNavAccess();
  }
  function syncNavAccess() {
    const sidebar = document.querySelector(".sidebar");
    const hidden = window.matchMedia("(max-width: 760px)").matches && !$("app").classList.contains("nav-open");
    if (hidden && sidebar.contains(document.activeElement)) $("menuButton").focus();
    sidebar.inert = hidden;
  }
  function renderSessions() {
    $("sessionList").replaceChildren();
    $("sessionCount").textContent = String(sessions.length);
    for (const session of sessions) {
      const button = document.createElement("button");
      button.className = `session-button${session.id === activeId ? " active" : ""}`;
      button.disabled = busy;
      button.setAttribute("aria-current", session.id === activeId ? "page" : "false");
      button.title = session.title;
      const title = document.createElement("span");
      title.textContent = session.title;
      button.append(icon("chat"), title);
      button.addEventListener("click", () => {
        if (busy) return;
        activeId = session.id;
        persist();
        render();
        closeNav();
        void refreshPending(session);
      });
      $("sessionList").append(button);
    }
  }
  // Build a small, safe Markdown subset from DOM text nodes. Model and user
  // content are never inserted as HTML, including partial streaming content.
  function citationText(parent, text, message) {
    const citations = message?.role === 'assistant' && message.status === 'complete' ? validCitations(message.citations) : [];
    const byNumber = new Map(citations.map((item) => [item.number, item]));
    let last = 0;
    for (const match of text.matchAll(/\[(\d+)\]/g)) {
      const citation = byNumber.get(Number(match[1]));
      if (!citation) continue;
      parent.append(document.createTextNode(text.slice(last, match.index)));
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'citation-button';
      button.textContent = match[0]; button.setAttribute('aria-label', `查看引用 ${citation.number} 的来源`);
      button.addEventListener('click', () => openSource(message, citation));
      parent.append(button);
      last = match.index + match[0].length;
    }
    parent.append(document.createTextNode(text.slice(last)));
  }
  function inline(parent, text, message) {
    const parts = text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g);
    for (const part of parts) {
      if (part.startsWith("**") && part.endsWith("**")) {
        const strong = document.createElement("strong");
        citationText(strong, part.slice(2, -2), message);
        parent.append(strong);
      } else if (part.startsWith("`") && part.endsWith("`") && part.length > 1) {
        const code = document.createElement("code");
        code.textContent = part.slice(1, -1);
        parent.append(code);
      } else citationText(parent, part, message);
    }
  }
  function richText(parent, text, message) {
    const fragment = document.createDocumentFragment();
    let paragraph = null;
    let list = null;
    let code = null;
    for (const line of text.split("\n")) {
      if (line.trimStart().startsWith("```")) {
        paragraph = list = null;
        if (code) code = null;
        else {
          const pre = document.createElement("pre");
          code = document.createElement("code");
          pre.append(code);
          fragment.append(pre);
        }
        continue;
      }
      if (code) { code.textContent += (code.textContent ? "\n" : "") + line; continue; }
      if (!line.trim()) { paragraph = list = null; continue; }
      const bullet = line.match(/^\s*(?:[-*]\s+|\d+[.)、]\s+)(.*)$/);
      if (bullet) {
        const kind = /^\s*\d/.test(line) ? "OL" : "UL";
        if (!list || list.tagName !== kind) { list = document.createElement(kind); fragment.append(list); }
        paragraph = null;
        const li = document.createElement("li");
        inline(li, bullet[1], message);
        list.append(li);
      } else {
        list = null;
        if (!paragraph) { paragraph = document.createElement("p"); fragment.append(paragraph); }
        else paragraph.append(document.createTextNode("\n"));
        inline(paragraph, line.replace(/^#{1,4}\s+/, ""), message);
      }
    }
    parent.replaceChildren(fragment);
  }
  let sourceView = 0;
  function openSource(message, citation) {
    const sequence = ++sourceView;
    $('sourceTitle').textContent = `引用 [${citation.number}]`;
    $('sourcePath').textContent = citation.section_path || '未提供章节路径';
    $('sourceChunk').textContent = citation.text;
    $('sourceDocument').hidden = true;
    $('sourceOriginal').replaceChildren();
    $('sourceError').hidden = true;
    $('showOriginal').disabled = false;
    $('showOriginal').onclick = async () => {
      if (!Number.isSafeInteger(message.conversationId) || message.conversationId <= 0 ||
          (typeof message.toolCallId !== 'string' && !/^[0-9a-f]{32}$/.test(message.workflowTurnId || ''))) {
        $('sourceError').textContent = '无法定位这条引用的原始会话。'; $('sourceError').hidden = false; return;
      }
      $('showOriginal').disabled = true;
      try {
        const reference = message.workflowTurnId ? `workflow-turns/${message.workflowTurnId}` : `tool-calls/${encodeURIComponent(message.toolCallId)}`;
        const url = `/api/conversations/${message.conversationId}/${reference}/sources/${citation.number}/document?user_id=${encodeURIComponent(userId)}`;
        const response = await fetch(url);
        if (!response.ok) throw new Error('来源原文暂时不可用，请稍后重试。');
        const documentView = await response.json();
        if (sequence !== sourceView || !$('sourceDialog').open) return;
        if (typeof documentView.text !== 'string' || documentView.source_key !== citation.source_key ||
            documentView.source_version !== citation.source_version || documentView.section_path !== citation.section_path) {
          throw new Error('来源版本与当前引用不一致。');
        }
        const lines = documentView.text.split('\n');
        const { start, end } = documentView.anchor || {};
        const pre = $('sourceOriginal'); pre.replaceChildren();
        if (Number.isInteger(start) && Number.isInteger(end) && start >= 1 && end >= start && end <= lines.length) {
          pre.append(document.createTextNode(lines.slice(0, start - 1).join('\n') + (start > 1 ? '\n' : '')));
          const mark = document.createElement('mark'); mark.textContent = lines.slice(start - 1, end).join('\n'); pre.append(mark);
          if (end < lines.length) pre.append(document.createTextNode('\n' + lines.slice(end).join('\n')));
          $('sourceDocument').hidden = false; mark.scrollIntoView({ block: 'nearest' });
        } else {
          pre.textContent = documentView.text; $('sourceDocument').hidden = false;
        }
      } catch (error) {
        if (sequence !== sourceView || !$('sourceDialog').open) return;
        $('sourceError').textContent = error.message || '来源原文暂时不可用。'; $('sourceError').hidden = false;
      } finally {
        if (sequence === sourceView) $('showOriginal').disabled = false;
      }
    };
    if (!$('sourceDialog').open) $('sourceDialog').showModal();
  }
  function renderMessage(message, session = current()) {
    const article = document.createElement("article");
    article.className = `message ${message.role}`;
    article.dataset.messageId = message.id;
    const avatar = document.createElement("span");
    avatar.className = "avatar";
    avatar.append(icon(message.role === "user" ? "user" : "bag"));
    const content = document.createElement("div");
    content.className = "message-content";
    const heading = document.createElement("div");
    heading.className = "message-heading";
    heading.append(document.createTextNode(message.role === "user" ? "你" : message.simulated ? "客服小猫（模拟）" : "RetailQ 助手"));
    const date = new Date(message.createdAt || Date.now());
    if (!Number.isNaN(date.getTime())) {
      const time = document.createElement("time");
      time.dateTime = date.toISOString();
      time.textContent = date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
      heading.append(time);
    }
    const body = document.createElement("div");
    body.className = "message-body";
    const tools = document.createElement("div");
    tools.className = "message-tools";
    const trace = document.createElement('div');
    trace.className = 'tool-trace';
    const actions = document.createElement('div'); actions.className = 'workflow-actions';
    content.append(heading, trace, body, actions, tools);
    article.append(avatar, content);
    $("messages").append(article);
    updateMessage(message, { article, body, tools, trace, actions, session });
    return { article, body, tools, trace, actions, session };
  }
  function updateMessage(message, elements) {
    const { article, body, tools, trace } = elements;
    trace.replaceChildren();
    for (const item of Array.isArray(message.toolTrace) ? message.toolTrace : []) {
      const badge = document.createElement('span');
      badge.className = `tool-badge ${['running','completed','failed'].includes(item.status) ? item.status : 'unknown'}`;
      const labels = {running:'执行中', completed:'已完成', failed: item.code === 'outcome_unknown' ? '结果未确认' : '未成功', unknown:'状态待核查'};
      badge.append(icon(item.status === 'completed' ? 'check' : 'spark'), document.createTextNode(`${item.name} · ${labels[item.status] || labels.unknown}`));
      trace.append(badge);
    }
    trace.hidden = !trace.childElementCount;
    article.classList.toggle("streaming", message.status === "pending" && Boolean(message.text));
    if (message.text) richText(body, message.text, message);
    else if (message.status === "pending") {
      const typing = document.createElement("div");
      typing.className = "typing";
      for (let i = 0; i < 3; i++) typing.append(document.createElement("span"));
      const label = document.createElement("div");
      label.className = "typing-label";
      label.textContent = (message.toolTrace || []).some((t) => t.status === 'running') ? '正在查询，请稍候' : "正在整理回复";
      typing.append(label);
      body.replaceChildren(typing);
    } else body.textContent = message.status === 'waiting_order' ? '请选择一笔订单，选定后将继续本次咨询。' : message.role === "assistant" ? "本次未收到完整回复。" : "";
    tools.replaceChildren();
    if (elements.actions) renderActions(elements.actions, message, {
      userId, isBusy: () => busy,
      request: async (path, options = {}) => {
        const timeout = new AbortController(); const timer = setTimeout(() => timeout.abort(), 10000);
        try {
          const response = await fetch(path, { method: options.method || 'GET',
            ...(options.body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(options.body) } : {}),
            signal: timeout.signal });
          const result = await response.json();
          if (!response.ok) throw Object.assign(new Error(result?.error?.message || '工单操作尚未确认。'),
            { status: response.status, code: result?.error?.code });
          return result;
        } finally { clearTimeout(timer); }
      },
      notify,
      changed: () => { persist(); if (current()?.id === elements.session?.id && !busy) render(); },
      simulateHandoff: lines => {
        const target = elements.session;
        if (!target) return;
        for (const text of lines) target.messages.push({ id: uid(), turnId: uid(), role: 'assistant', text,
          status: 'complete', simulated: true, createdAt: new Date().toISOString(), toolTrace: [] });
      },
    });
    if (elements.actions && message.orderSelector) renderOrderSelector(elements.actions, message.orderSelector, {
      isBusy: () => busy,
      select: orderId => void resumeOrder(elements.session, message, {order_id: orderId}),
      cancel: () => void resumeOrder(elements.session, message, {cancel: true}),
      check: () => void refreshPending(elements.session, true),
    });
    if (message.role !== "assistant" || message.status === "pending") return;
    if (message.status === 'complete' && message.text) {
      const feedback = document.createElement('div');
      feedback.className = 'feedback'; feedback.setAttribute('aria-label', '对这条回答的评价');
      for (const [value, symbol, label] of [['up', '👍', '满意'], ['down', '👎', '不满意']]) {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'feedback-button';
        button.textContent = symbol; button.setAttribute('aria-label', label);
        button.setAttribute('aria-pressed', String(message.feedback?.value === value));
        button.disabled = Boolean(message.feedback);
        button.addEventListener('click', () => {
          if (message.feedback || message.status !== 'complete') return;
          message.feedback = { value, createdAt: new Date().toISOString(), messageId: message.id,
            turnId: message.turnId, conversationId: message.conversationId ?? current()?.conversationId ?? null };
          persist(); updateMessage(message, elements);
          $('announcer').textContent = '已反馈';
        });
        feedback.append(button);
      }
      if (message.feedback) {
        const status = document.createElement('span'); status.className = 'feedback-status'; status.textContent = '已反馈';
        feedback.append(status);
      }
      tools.append(feedback);
    }
    if (message.text) {
      const copy = document.createElement("button");
      copy.className = "copy-button";
      copy.append(icon("copy"), document.createTextNode("复制"));
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(message.text); notify("回复已复制"); }
        catch { notify("复制未成功，请选择文字后手动复制。"); }
      });
      tools.append(copy);
    }
    if (message.status !== "complete") {
      const note = document.createElement("span");
      note.className = "message-status problem";
      note.textContent = message.detail || "这条回复未完成，未带入后续对话。";
      tools.append(note);
    }
  }
  function render() {
    const session = current();
    renderSessions();
    $("conversationTitle").textContent = session.messages.length ? session.title : "购物咨询";
    $("welcome").hidden = Boolean(session.messages.length);
    $("messages").hidden = !session.messages.length;
    $("messages").replaceChildren();
    session.messages.forEach(message => renderMessage(message, session));
    if (session.messages.length) scrollToBottom(true);
    else $("chatScroll").scrollTop = 0;
    syncControls();
  }
  function scrollToBottom(force = false) {
    const view = $("chatScroll");
    if (force || view.scrollHeight - view.scrollTop - view.clientHeight < 180) {
      view.scrollTo({ top: view.scrollHeight, behavior: "instant" });
    }
  }
  function syncControls() {
    $("sendButton").disabled = !busy && (!$("messageInput").value.trim() || Boolean(current()?.pendingOrder));
    $("messageInput").placeholder = current()?.pendingOrder ? '请先选择订单，或取消后继续咨询' : '描述您的购物问题';
    $("sendButton").classList.toggle("stopping", busy);
    $("sendButton").type = busy ? "button" : "submit";
    $("sendLabel").textContent = busy ? "停止回复" : "发送";
    $("sendIcon").replaceChildren(icon(busy ? "stop" : "arrow").firstChild);
    $("sendButton").setAttribute("aria-label", busy ? "停止生成回复" : "发送消息");
    $("newChat").disabled = busy;
    document.querySelectorAll(".suggestion").forEach((button) => { button.disabled = busy; });
    $("charCount").textContent = `${$("messageInput").value.length} / 8000`;
  }
  function historyFor(session) {
    const users = new Map(session.messages.filter((m) => m.role === "user").map((m) => [m.turnId, m]));
    const result = [];
    for (const message of session.messages) {
      if (message.role !== "assistant" || message.status !== "complete") continue;
      const user = users.get(message.turnId);
      if (user) result.push({ role: "user", content: user.text }, { role: "assistant", content: message.text });
    }
    return result.slice(-100);
  }
  function serviceState(ready, text) {
    $("serviceStatus").classList.toggle("offline", !ready);
    $("serviceText").textContent = text;
  }
  async function checkService() {
    const timeout = new AbortController();
    const timer = setTimeout(() => timeout.abort(), 4000);
    try {
      const response = await fetch("/health", { signal: timeout.signal });
      const health = await response.json();
      if (response.ok && health.model_configured && health.database_configured) serviceState(true, "服务已就绪");
      else serviceState(false, "服务暂未就绪");
    } catch { serviceState(false, "暂时无法连接"); }
    finally { clearTimeout(timer); }
  }
  async function send(text, resume = null) {
    if (busy || (!resume && !text.trim())) return;
    text = text.trim();
    if (text.length > 8000) { notify("单条消息请控制在8000字以内。"); return; }
    const session = current();
    if (!resume && session.pendingOrder) { notify('请先选择订单或取消本次选择。'); return; }
    const history = historyFor(session);
    const turnId = uid();
    const createdAt = new Date().toISOString();
    const user = { id: uid(), turnId, role: "user", text, status: "complete", createdAt };
    const assistant = resume ? resume.message : { id: uid(), turnId, role: "assistant", text: "", status: "pending", createdAt, toolTrace: [] };
    if (!session.messages.length) session.title = Array.from(text).slice(0, 20).join("");
    if (!resume) session.messages.push(user, assistant);
    else { assistant.status = 'pending'; assistant.text = ''; assistant.citations = []; assistant.actions = []; assistant.orderSelector = null; }
    session.messages = session.messages.slice(-200);
    busy = true;
    controller = new AbortController();
    const requestController = controller;
    $("messageInput").value = "";
    $("messageInput").style.height = "40px";
    $("messageInput").style.overflowY = "hidden";
    persist();
    render();
    const article = $("messages").children[session.messages.indexOf(assistant)];
    const elements = { article, body: article.querySelector(".message-body"), tools: article.querySelector(".message-tools"),
      trace: article.querySelector('.tool-trace'), actions: article.querySelector('.workflow-actions'), session };
    let reader;
    let terminal = null;
    const workflowFrames = new WorkflowFrames();
    let inputRejected = false;
    try {
      const response = await fetch(resume ? `/api/conversations/${session.conversationId}/turns/${resume.selector.turn_id}/resume` : "/api/chat/stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(resume ? {user_id: userId, interrupt_id: resume.selector.interrupt_id,
          request_key: resume.selector.request_key, ...resume.selection} : { message: text, history: session.conversationId ? [] : history, user_id: userId,
          ...(session.conversationId ? {conversation_id: session.conversationId} : {}) }), signal: requestController.signal,
      });
      if (!response.ok) {
        inputRejected = [400,404,409,422].includes(response.status);
        let detail;
        try { detail = await response.json(); } catch { /* Use the public fallback below. */ }
        throw new Error(detail?.error?.message || (response.status === 422
          ? "输入内容不符合要求，请缩短问题后重试。" : "服务暂时不可用，请稍后重试。"));
      }
      if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream")) {
        throw new Error("回复连接异常，请稍后重试。");
      }
      reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let event = "";
      let data = [];
      function lineReceived(line) {
        if (line.startsWith(":")) return;
        if (line.startsWith("event:")) { event = line.slice(6).trim(); return; }
        if (line.startsWith("data:")) { data.push(line.slice(5).replace(/^ /, "")); return; }
        if (line !== "") return;
        if (data.length) {
          const payload = JSON.parse(data.join("\n"));
          workflowFrames.accept(event, payload);
          if (event === "error") throw new Error(payload.message || "回复中断，请稍后重试。");
          if (terminal !== null) throw new Error("回复连接异常，未能完整接收。");
          if (event === "token") {
            if (typeof payload.text !== "string") throw new Error("回复格式异常，请重试。");
            assistant.text += payload.text;
            updateMessage(assistant, elements);
            scrollToBottom();
          } else if (event === 'meta') {
            if (!Number.isSafeInteger(payload.conversation_id) || payload.conversation_id <= 0) throw new Error('会话标识异常，请新建对话。');
            if ((session.conversationId && payload.conversation_id !== session.conversationId) ||
                (resume && payload.turn_id !== resume.selector.turn_id)) throw new Error('回复所属会话或运行不匹配。');
            session.conversationId = payload.conversation_id;
            assistant.workflowTurnId = payload.turn_id;
            persist();
          } else if (event === 'workflow') {
            if (typeof payload.node !== 'string' || !['enter', 'exit'].includes(payload.phase)) throw new Error('工作流状态格式异常。');
          } else if (event === 'order_selector') {
            const selector = validateSelector(payload);
            if (assistant.orderSelector) throw new Error('重复的订单选择信息。');
            if (selector.conversation_id !== session.conversationId || selector.turn_id !== assistant.workflowTurnId) throw new Error('订单选择所属会话不匹配。');
            session.pendingOrder = selector; assistant.orderSelector = selector; assistant.conversationId = session.conversationId; persist();
          } else if (event === 'paused') {
            if (!assistant.orderSelector || payload.conversation_id !== session.conversationId || payload.turn_id !== assistant.workflowTurnId || payload.interrupt_id !== assistant.orderSelector.interrupt_id) throw new Error('暂停信息不完整。');
            terminal = 'paused';
          } else if (event === 'cancelled') {
            if (payload.turn_id !== assistant.workflowTurnId || payload.conversation_id !== session.conversationId) throw new Error('取消信息不匹配。');
            terminal = 'cancelled';
          } else if (event === 'actions') {
            if (payload.conversation_id !== session.conversationId || payload.turn_id !== assistant.workflowTurnId) throw new Error('操作来源会话不匹配。');
            assistant.actions = validateActions(payload).map(action => ({ ...action, verified: true }));
            assistant.serverMessageId = payload.message_id;
            assistant.conversationId = session.conversationId;
          } else if (event === 'tool_status') {
            if (typeof payload.name !== 'string' || typeof payload.tool_call_id !== 'string' || !['running','completed','failed'].includes(payload.status)) throw new Error('工具状态格式异常。');
            const prior = assistant.toolTrace.findIndex((t) => t.tool_call_id === payload.tool_call_id);
            if (prior < 0) assistant.toolTrace.push(payload);
            else assistant.toolTrace[prior] = payload;
            updateMessage(assistant, elements);
            persist();
            scrollToBottom();
            $('announcer').textContent = `${payload.name} ${payload.status === 'running' ? '执行中' : payload.status === 'completed' ? '已完成' : '结果需核查'}`;
          } else if (event === 'sources') {
            const citations = validCitations(payload.citations);
            if (!Array.isArray(payload.citations) || citations.length !== payload.citations.length ||
                typeof payload.message_id !== 'string' || !/^[1-9]\d*$/.test(payload.message_id) ||
                (typeof payload.tool_call_id !== 'string' && !/^[0-9a-f]{32}$/.test(payload.turn_id || '')) || !session.conversationId) {
              throw new Error('引用来源格式异常，请重试。');
            }
            assistant.citations = citations;
            assistant.toolCallId = payload.tool_call_id;
            assistant.conversationId = session.conversationId;
            assistant.serverMessageId = payload.message_id;
            persist();
          } else if (event === "done") { workflowFrames.finish(payload.finish_reason); terminal = payload.finish_reason; }
          else throw new Error("回复格式异常，请重试。");
        }
        event = "";
        data = [];
      }
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        let newline;
        while ((newline = buffer.indexOf("\n")) !== -1) {
          lineReceived(buffer.slice(0, newline).replace(/\r$/, ""));
          buffer = buffer.slice(newline + 1);
        }
        if (done) break;
      }
      if (terminal === 'paused') { assistant.status = 'waiting_order'; assistant.detail = ''; }
      else if (terminal === 'cancelled') {
        assistant.status = 'cancelled'; assistant.text = '已取消本次订单选择，您可以继续咨询。';
        assistant.orderSelector = null; session.pendingOrder = null;
      } else if (terminal !== "stop" || !assistant.text) {
        assistant.status = terminal === "length" ? "truncated" : "error";
        assistant.detail = terminal === "length" ? "回复达到长度限制。已执行的工具记录会保留，可缩短问题后继续咨询。" : "回复未完整结束，请核查工具状态后继续。";
      } else {
        assistant.status = "complete";
        assistant.orderSelector = null; session.pendingOrder = null;
        serviceState(true, "服务已就绪");
        $("announcer").textContent = "客服回复已完成。";
      }
    } catch (error) {
      assistant.status = requestController.signal.aborted ? "stopped" : "error";
      assistant.detail = requestController.signal.aborted ? `已停止回复。${executionNote(assistant)}` :
        `${error instanceof TypeError ? "网络连接中断，请检查服务。" : error.message || "回复异常。"} ${executionNote(assistant)}`;
      if (!requestController.signal.aborted && !inputRejected) serviceState(false, "本次回复异常");
      if (resume && session.pendingOrder) { session.pendingOrder.uncertain = true; assistant.orderSelector = session.pendingOrder; }
    } finally {
      try { await reader?.cancel(); } catch { /* Already aborted or closed. */ }
      reader?.releaseLock();
      busy = false;
      controller = null;
      assistant.toolTrace = assistant.toolTrace.map((t) => t.status === 'running' ? {...t, status:'unknown'} : t);
      updateMessage(assistant, elements);
      persist();
      syncControls();
      render();
      scrollToBottom();
      $("messageInput").focus({ preventScroll: true });
    }
  }
  async function resumeOrder(session, message, selection) {
    if (busy || current()?.id !== session.id) return;
    const selector = session.pendingOrder;
    if (!selector) return;
    if (selector.selection && JSON.stringify(selector.selection) !== JSON.stringify(selection)) { notify('请先核查已提交的选择。'); return; }
    selector.request_key ||= crypto.randomUUID().replaceAll('-', ''); selector.selection = selection;
    if (selection.order_id) selector.selected_order = selection.order_id;
    persist(); await send('', {selector, message, selection});
  }
  async function refreshPending(session, checkResult = false) {
    if (!session?.conversationId || busy) return;
    try {
      if (checkResult && session.pendingOrder?.selection) {
        const saved = session.pendingOrder;
        const response = await fetch(`/api/conversations/${session.conversationId}/turns/${saved.turn_id}?user_id=${encodeURIComponent(userId)}`);
        if (!response.ok) throw new Error('暂时无法核查，请稍后重试。');
        const status = await response.json();
        const message = session.messages.find(m => m.workflowTurnId === saved.turn_id && m.role === 'assistant');
        if (['completed', 'limited', 'cancelled'].includes(status.status) || status.status === 'waiting_order') {
          if (message) { saved.uncertain = false; await resumeOrder(session, message, saved.selection); return; }
        }
        if (status.status === 'active') { notify('该请求仍在处理中，请稍后核查。'); return; }
        if (['failed', 'interrupted'].includes(status.status)) {
          session.pendingOrder = null;
          if (message) { message.orderSelector = null; message.status = 'error'; message.detail = '本轮处理未完成，请重新咨询。'; }
          persist(); render(); notify('本轮已停止，请重新咨询。'); return;
        }
      }
      const response = await fetch(`/api/conversations/${session.conversationId}/pending?user_id=${encodeURIComponent(userId)}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload?.error?.message || '暂时无法核查订单选择。');
      if (payload.pending) {
        const selector = validateSelector(payload.pending);
        const prior = session.pendingOrder;
        if (prior?.interrupt_id === selector.interrupt_id) Object.assign(selector, {request_key: prior.request_key, selection: prior.selection,
          selected_order: prior.selected_order, uncertain: Boolean(prior.selection)});
        session.pendingOrder = selector;
        let message = session.messages.find(m => m.workflowTurnId === selector.turn_id && m.role === 'assistant');
        if (!message) { message = {id: uid(), turnId: uid(), workflowTurnId: selector.turn_id, role: 'assistant', text: '', createdAt: new Date().toISOString(), toolTrace: []}; session.messages.push(message); }
        Object.assign(message, {status: 'waiting_order', orderSelector: selector, conversationId: session.conversationId});
      } else if (session.pendingOrder && !session.pendingOrder.selection) {
        const message = session.messages.find(m => m.workflowTurnId === session.pendingOrder.turn_id && m.role === 'assistant');
        if (message) message.orderSelector = null;
        session.pendingOrder = null;
      } else if (session.pendingOrder?.selection) {
        session.pendingOrder.uncertain = true;
        const message = session.messages.find(m => m.workflowTurnId === session.pendingOrder.turn_id && m.role === 'assistant');
        if (message) message.orderSelector = session.pendingOrder;
      }
      persist(); if (current()?.id === session.id) render();
    } catch (error) { if (checkResult) notify(error.message); }
  }
  $("chatForm").addEventListener("submit", (event) => { event.preventDefault(); void send($("messageInput").value); });
  $("sendButton").addEventListener("click", () => { if (busy) controller?.abort(); });
  $("messageInput").addEventListener("input", () => {
    $("messageInput").style.height = "40px";
    $("messageInput").style.height = `${Math.min($("messageInput").scrollHeight, 128)}px`;
    $("messageInput").style.overflowY = $("messageInput").scrollHeight > 128 ? "auto" : "hidden";
    syncControls();
  });
  $("messageInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229 && !busy) {
      event.preventDefault();
      $("chatForm").requestSubmit();
    }
  });
  $("newChat").addEventListener("click", () => {
    if (busy) return;
    if (current().messages.length) newSession();
    persist(); render(); closeNav();
    $("messageInput").focus();
  });
  $("suggestions").addEventListener("click", (event) => {
    const button = event.target.closest("[data-prompt]");
    if (button) void send(button.dataset.prompt);
  });
  $("menuButton").setAttribute("aria-expanded", "false");
  $("menuButton").addEventListener("click", () => {
    const open = $("app").classList.toggle("nav-open");
    $("navBackdrop").hidden = !open;
    $("menuButton").setAttribute("aria-expanded", String(open));
    syncNavAccess();
  });
  $("navBackdrop").addEventListener("click", closeNav);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeNav(); });
  const help = () => $("helpDialog").showModal();
  $("helpButton").addEventListener("click", help);
  $("scopeButton").addEventListener("click", help);
  $("closeHelp").addEventListener("click", () => $("helpDialog").close());
  $("helpDone").addEventListener("click", () => { $("helpDialog").close(); $("messageInput").focus(); });
  $('closeSource').addEventListener('click', () => $('sourceDialog').close());
  $('sourceDialog').addEventListener('close', () => { sourceView++; });
  window.matchMedia("(max-width: 760px)").addEventListener("change", syncNavAccess);
  restore(); render(); syncNavAccess(); void checkService(); void refreshPending(current());
