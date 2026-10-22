(() => {
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
  const current = () => sessions.find((session) => session.id === activeId);
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
        ).slice(-200).map((message) => message.status === "pending"
          ? { ...message, status: "stopped", detail: "页面已重新打开，上次未完成的回复未带入后续对话。" }
          : message),
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
      });
      $("sessionList").append(button);
    }
  }
  // Build a small, safe Markdown subset from DOM text nodes. Model and user
  // content are never inserted as HTML, including partial streaming content.
  function inline(parent, text) {
    const parts = text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/g);
    for (const part of parts) {
      if (part.startsWith("**") && part.endsWith("**")) {
        const strong = document.createElement("strong");
        strong.textContent = part.slice(2, -2);
        parent.append(strong);
      } else if (part.startsWith("`") && part.endsWith("`") && part.length > 1) {
        const code = document.createElement("code");
        code.textContent = part.slice(1, -1);
        parent.append(code);
      } else parent.append(document.createTextNode(part));
    }
  }
  function richText(parent, text) {
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
        inline(li, bullet[1]);
        list.append(li);
      } else {
        list = null;
        if (!paragraph) { paragraph = document.createElement("p"); fragment.append(paragraph); }
        else paragraph.append(document.createTextNode("\n"));
        inline(paragraph, line.replace(/^#{1,4}\s+/, ""));
      }
    }
    parent.replaceChildren(fragment);
  }
  function renderMessage(message) {
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
    heading.append(document.createTextNode(message.role === "user" ? "你" : "RetailQ 助手"));
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
    content.append(heading, body, tools);
    article.append(avatar, content);
    $("messages").append(article);
    updateMessage(message, { article, body, tools });
    return { article, body, tools };
  }
  function updateMessage(message, elements) {
    const { article, body, tools } = elements;
    article.classList.toggle("streaming", message.status === "pending" && Boolean(message.text));
    if (message.text) richText(body, message.text);
    else if (message.status === "pending") {
      const typing = document.createElement("div");
      typing.className = "typing";
      for (let i = 0; i < 3; i++) typing.append(document.createElement("span"));
      const label = document.createElement("div");
      label.className = "typing-label";
      label.textContent = "正在整理回复";
      typing.append(label);
      body.replaceChildren(typing);
    } else body.textContent = message.role === "assistant" ? "本次未收到完整回复。" : "";
    tools.replaceChildren();
    if (message.role !== "assistant" || message.status === "pending") return;
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
    session.messages.forEach(renderMessage);
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
    $("sendButton").disabled = !busy && !$("messageInput").value.trim();
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
      if (response.ok && health.model_configured) serviceState(true, "服务已就绪");
      else serviceState(false, "服务暂未就绪");
    } catch { serviceState(false, "暂时无法连接"); }
    finally { clearTimeout(timer); }
  }
  async function send(text) {
    if (busy || !text.trim()) return;
    text = text.trim();
    if (text.length > 8000) { notify("单条消息请控制在8000字以内。"); return; }
    const session = current();
    const history = historyFor(session);
    const turnId = uid();
    const createdAt = new Date().toISOString();
    const user = { id: uid(), turnId, role: "user", text, status: "complete", createdAt };
    const assistant = { id: uid(), turnId, role: "assistant", text: "", status: "pending", createdAt };
    if (!session.messages.length) session.title = Array.from(text).slice(0, 20).join("");
    session.messages.push(user, assistant);
    session.messages = session.messages.slice(-200);
    busy = true;
    controller = new AbortController();
    const requestController = controller;
    $("messageInput").value = "";
    $("messageInput").style.height = "40px";
    $("messageInput").style.overflowY = "hidden";
    persist();
    render();
    const article = $("messages").lastElementChild;
    const elements = { article, body: article.querySelector(".message-body"), tools: article.querySelector(".message-tools") };
    let reader;
    let terminal = null;
    let inputRejected = false;
    try {
      const response = await fetch("/api/chat/stream", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history }), signal: requestController.signal,
      });
      if (!response.ok) {
        inputRejected = response.status === 422;
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
          if (event === "error") throw new Error(payload.message || "回复中断，请稍后重试。");
          if (terminal !== null) throw new Error("回复连接异常，未能完整接收。");
          if (event === "token") {
            if (typeof payload.text !== "string") throw new Error("回复格式异常，请重试。");
            assistant.text += payload.text;
            updateMessage(assistant, elements);
            scrollToBottom();
          } else if (event === "done") terminal = payload.finish_reason;
          else if (event !== "meta") throw new Error("回复格式异常，请重试。");
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
      if (terminal !== "stop" || !assistant.text) {
        assistant.status = terminal === "length" ? "truncated" : "error";
        assistant.detail = terminal === "length" ? "回复达到长度限制，未带入后续对话。你可以缩短问题后再试。" : "回复未完整结束，未带入后续对话。";
      } else {
        assistant.status = "complete";
        serviceState(true, "服务已就绪");
        $("announcer").textContent = "客服回复已完成。";
      }
    } catch (error) {
      assistant.status = requestController.signal.aborted ? "stopped" : "error";
      assistant.detail = requestController.signal.aborted ? "已停止回复，未带入后续对话。" :
        `${error instanceof TypeError ? "网络连接中断，请检查服务后重试。" : error.message || "回复异常，请重试。"} 本轮未带入后续对话。`;
      if (!requestController.signal.aborted && !inputRejected) serviceState(false, "本次回复异常");
    } finally {
      try { await reader?.cancel(); } catch { /* Already aborted or closed. */ }
      reader?.releaseLock();
      busy = false;
      controller = null;
      updateMessage(assistant, elements);
      persist();
      syncControls();
      renderSessions();
      scrollToBottom();
      $("messageInput").focus({ preventScroll: true });
    }
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
  window.matchMedia("(max-width: 760px)").addEventListener("change", syncNavAccess);
  restore(); render(); syncNavAccess(); void checkService();
})();
