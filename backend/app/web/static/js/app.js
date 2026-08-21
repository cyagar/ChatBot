"use strict";

/* Technician Manual Assistant — vanilla JS frontend (no build step, tablet-first). */

const state = {
  user: null,
  screen: "loading", // loading | auth | picker | chat
  authMode: "login", // login | register
  machine: null, // {id, manufacturer, model_name, ...}
  conversationId: null,
  messages: [],
  machineResults: [],
  recentMachines: [],
  searchQuery: "",
  searchCursor: null, // caret position to restore after a re-render replaces the search <input>
  sending: false,
  online: navigator.onLine,
  authError: null,
  evidenceOpen: {}, // messageId -> bool
  evidenceCache: {}, // chunkId -> data
};

const root = document.getElementById("app");

// --- API helper -------------------------------------------------------

async function api(path, options = {}) {
  const resp = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (resp.status === 503) {
    let body = {};
    try { body = await resp.json(); } catch (_) {}
    if (body.offline) {
      const err = new Error("offline");
      err.offline = true;
      throw err;
    }
  }
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`;
    try {
      const body = await resp.json();
      detail = body.detail || detail;
    } catch (_) {}
    const err = new Error(detail);
    err.status = resp.status;
    throw err;
  }
  if (resp.status === 204 || resp.status === 202) return null;
  return resp.json();
}

// --- Boot ---------------------------------------------------------------

function notifyServiceWorker(message) {
  navigator.serviceWorker?.controller?.postMessage(message);
}

async function boot() {
  window.addEventListener("online", () => { state.online = true; render(); });
  window.addEventListener("offline", () => { state.online = false; render(); });

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  }

  try {
    state.user = await api("/api/auth/me");
    state.screen = "picker";
    // Tells the worker which user's manual cache to read/write from now on --
    // it has no other way to know (concern #21: a shared tablet must not mix
    // one technician's cached manuals into another's session).
    notifyServiceWorker({ type: "SET_USER", userId: state.user.id });
    await loadRecentMachines();
  } catch (e) {
    state.screen = "auth";
  }
  render();
}

async function logout() {
  await api("/api/auth/logout", { method: "POST" });
  notifyServiceWorker({ type: "LOGOUT" });
  state.user = null; state.screen = "auth"; render();
}

// --- Auth -----------------------------------------------------------------

function renderAuth() {
  const isLogin = state.authMode === "login";
  root.innerHTML = `
    <main style="justify-content:center; align-items:center; min-height:100vh;">
      <form id="auth-form" class="picker-card" style="max-width:420px; width:100%;">
        <h1>Technician Manual Assistant</h1>
        <p class="subtitle">${isLogin ? "Sign in to continue." : "Create your technician account."}</p>
        ${state.authError ? `<div class="banner error" role="alert">${escapeHtml(state.authError)}</div>` : ""}
        ${!isLogin ? `
          <label class="sr-only" for="display_name">Name</label>
          <input id="display_name" name="display_name" placeholder="Your name (optional)" autocomplete="name" />
        ` : ""}
        <label class="sr-only" for="email">Email</label>
        <input id="email" name="email" type="email" placeholder="Email" required autocomplete="username" />
        <label class="sr-only" for="password">Password</label>
        <input id="password" name="password" type="password" placeholder="Password" required minlength="8" autocomplete="${isLogin ? "current-password" : "new-password"}" />
        <button type="submit" class="primary">${isLogin ? "Sign in" : "Create account"}</button>
        <button type="button" id="toggle-auth" class="ghost">
          ${isLogin ? "Need an account? Register" : "Already have an account? Sign in"}
        </button>
        <p style="font-size:0.8rem; color:var(--text-dim);">
          The first account created on a fresh install becomes the administrator.
        </p>
      </form>
    </main>
  `;

  document.getElementById("toggle-auth").addEventListener("click", () => {
    state.authMode = isLogin ? "register" : "login";
    state.authError = null;
    render();
  });

  document.getElementById("auth-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const email = document.getElementById("email").value.trim();
    const password = document.getElementById("password").value;
    const path = isLogin ? "/api/auth/login" : "/api/auth/register";
    const payload = isLogin ? { email, password } : {
      email, password, display_name: document.getElementById("display_name")?.value.trim() || null,
    };
    try {
      state.user = await api(path, { method: "POST", body: JSON.stringify(payload) });
      notifyServiceWorker({ type: "SET_USER", userId: state.user.id });
      state.authError = null;
      state.screen = "picker";
      await loadRecentMachines();
      render();
    } catch (err) {
      state.authError = err.message;
      render();
    }
  });
}

// --- Machine picker ---------------------------------------------------

async function loadRecentMachines() {
  try {
    state.recentMachines = await api("/api/machines/recent");
  } catch (_) {
    state.recentMachines = [];
  }
}

async function searchMachines(q) {
  try {
    state.machineResults = await api(`/api/machines?q=${encodeURIComponent(q)}`);
  } catch (_) {
    state.machineResults = [];
  }
  render();
}

function machineListItem(m) {
  return `
    <li>
      <button data-machine-id="${m.id}" class="machine-option">
        <span class="model">${escapeHtml(m.manufacturer)} — ${escapeHtml(m.model_name)}</span>
        <span class="meta">${escapeHtml(m.family || "")}${m.family ? " · " : ""}${m.document_count} manual(s) indexed</span>
      </button>
    </li>
  `;
}

function renderPicker() {
  const showResults = state.searchQuery.trim().length > 0;
  root.innerHTML = `
      <header class="app-header">
        <div class="brand">🔧 Technician Manual Assistant</div>
        <div class="header-actions">
          ${state.user?.role === "administrator" ? `<a href="/admin"><button class="ghost">Admin</button></a>` : ""}
          <button id="logout-btn" class="ghost">Sign out</button>
        </div>
      </header>
      <main>
        ${!state.online ? `<div class="banner offline">You're offline. Machine search and chat answers need a live connection; previously opened manuals may still be viewable.</div>` : ""}
        <div class="picker-card">
          <h1>What machine are you working on?</h1>
          <p class="subtitle">Select or search for a manufacturer and model before asking a question.</p>
          <div class="search-row">
            <input id="machine-search" type="search" placeholder="Search manufacturer or model (e.g. Bunn Axiom, CMA 180UC)"
                   value="${escapeHtml(state.searchQuery)}" autocomplete="off" aria-label="Search machines" />
          </div>
          ${showResults ? `
            <p class="section-label">Results</p>
            <ul class="machine-list">${state.machineResults.map(machineListItem).join("") || `<li class="empty-state">No matching machine with indexed manuals.</li>`}</ul>
          ` : `
            ${state.recentMachines.length ? `
              <p class="section-label">Recent &amp; favorite machines</p>
              <ul class="machine-list">${state.recentMachines.map(machineListItem).join("")}</ul>
            ` : `<p class="subtitle">Start typing to find a machine, or ask a question and I'll try to figure it out.</p>`}
          `}
          <button id="skip-machine" class="ghost">Skip — I'll say which machine in my question</button>
        </div>
      </main>
  `;

  document.getElementById("logout-btn").addEventListener("click", logout);

  const searchInput = document.getElementById("machine-search");
  searchInput.addEventListener("input", debounce((e) => {
    state.searchQuery = e.target.value;
    state.searchCursor = e.target.selectionStart;
    if (state.searchQuery.trim()) searchMachines(state.searchQuery.trim());
    else render();
  }, 250));
  searchInput.focus();
  // A full re-render replaces this <input> with a fresh DOM node, which resets
  // the caret to position 0 even though .value is restored — without this the
  // caret visibly jumps to the start of the text on every debounced update.
  const caretPos = state.searchCursor ?? searchInput.value.length;
  searchInput.setSelectionRange(caretPos, caretPos);

  root.querySelectorAll(".machine-option").forEach((btn) => {
    btn.addEventListener("click", () => selectMachine(parseInt(btn.dataset.machineId, 10)));
  });

  document.getElementById("skip-machine").addEventListener("click", () => startConversation(null));
}

async function selectMachine(machineId) {
  const all = [...state.machineResults, ...state.recentMachines];
  state.machine = all.find((m) => m.id === machineId) || null;
  api(`/api/machines/${machineId}/touch`, { method: "POST" }).catch(() => {});
  await startConversation(machineId);
}

async function startConversation(machineId) {
  try {
    const conv = await api("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ machine_id: machineId }),
    });
    state.conversationId = conv.id;
    state.messages = [];
    state.screen = "chat";
    render();
  } catch (err) {
    alert("Could not start a conversation: " + err.message);
  }
}

// --- Chat -----------------------------------------------------------------

function machineDisplayLabel(m) {
  if (!m) return "No machine selected";
  return m.label || `${m.manufacturer || ""}${m.manufacturer && m.model_name ? " — " : ""}${m.model_name || ""}`;
}

function renderChat() {
  root.innerHTML = `
      <header class="app-header">
        <button id="change-machine" class="machine-pill" aria-label="Change machine">
          <span aria-hidden="true">🔧</span>
          <span class="machine-pill-text">${escapeHtml(machineDisplayLabel(state.machine))}</span>
        </button>
        <div class="header-actions">
          <button id="new-conversation" class="ghost">New conversation</button>
          ${state.user?.role === "administrator" ? `<a href="/admin"><button class="ghost">Admin</button></a>` : ""}
          <button id="logout-btn" class="ghost">Sign out</button>
        </div>
      </header>
      <main>
        ${!state.online ? `<div class="banner offline">You're offline. A live connection is required for new AI answers. Previously loaded manual pages may still open from cache.</div>` : ""}
        <div class="chat-log" id="chat-log" role="log" aria-live="polite">
          ${state.messages.length === 0 ? `
            <div class="empty-state">
              Ask a question below${state.machine ? " about the " + escapeHtml(machineDisplayLabel(state.machine)) : ""}.
              Answers are grounded in the indexed manuals and always cite their source.
            </div>
          ` : state.messages.map(renderMessage).join("")}
          ${state.sending ? `<div class="msg assistant"><div class="bubble loading-dots">Searching manuals</div></div>` : ""}
        </div>
        <div class="composer">
          <form id="composer-form" class="composer-row">
            <label class="sr-only" for="question-input">Your question</label>
            <textarea id="question-input" placeholder="Type your question…" rows="1" required maxlength="2000"></textarea>
            <button type="button" id="mic-btn" class="mic" aria-label="Dictate question" title="Voice input">🎤</button>
            <button type="submit" class="primary send" aria-label="Send" ${state.sending ? "disabled" : ""}>Send</button>
          </form>
        </div>
        <footer class="disclaimer">
          Manual-based assistance only. Always follow your company's safety procedures. Not a substitute for lockout/tagout or manufacturer instructions.
        </footer>
      </main>
  `;

  document.getElementById("change-machine").addEventListener("click", () => {
    state.screen = "picker"; state.searchQuery = ""; render();
  });
  document.getElementById("new-conversation").addEventListener("click", () => startConversation(state.machine?.id ?? null));
  document.getElementById("logout-btn").addEventListener("click", logout);

  document.getElementById("composer-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = document.getElementById("question-input");
    const text = input.value.trim();
    if (!text || state.sending) return;
    input.value = "";
    await sendQuestion(text);
  });

  document.getElementById("question-input").addEventListener("keydown", (e) => {
    // Enter sends; Shift+Enter still inserts a newline (standard chat-app
    // convention). isComposing guards IME text entry (e.g. typing Japanese/
    // Chinese) so committing a candidate with Enter doesn't send early.
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      document.getElementById("composer-form").requestSubmit();
    }
  });

  setupMic();
  wireEvidenceToggles();
  wireMessageActions();

  const log = document.getElementById("chat-log");
  if (log) log.scrollTop = log.scrollHeight;
}

function renderCitation(c) {
  return `<span class="citation-chip">📄 ${escapeHtml(c.filename)}${c.page_number ? " · p." + c.page_number : ""}</span>`;
}

function renderMessage(m) {
  if (m.role === "user") {
    return `<div class="msg user"><div class="bubble">${escapeHtml(m.content)}</div></div>`;
  }

  const bubbleClass = m.is_clarifying_question ? "clarifying" : (m.is_no_answer ? "no-answer" : "");
  const parts = [`<div class="msg assistant" data-message-id="${m.id}">`];
  parts.push(`<div class="bubble ${bubbleClass}">${renderMarkdownish(m.content)}</div>`);

  if (m.is_clarifying_question && m.clarifying_options && m.clarifying_options.length) {
    // Tappable, not "type the machine name again" -- carries the canonical
    // machine ID straight to the confirm endpoint (concern #6).
    parts.push(`<div class="clarify-options">${m.clarifying_options.map((o) =>
      `<button class="ghost clarify-option-btn" data-machine-id="${o.id}">${escapeHtml(o.label)}</button>`
    ).join("")}</div>`);
  } else if (m.is_clarifying_question) {
    parts.push(`<div class="clarify-options"><button class="ghost clarify-pick-btn">Choose a machine</button></div>`);
  }

  if (m.answer_status === "failed") {
    parts.push(`<div class="msg-actions"><button class="ghost retry-btn" data-message-id="${m.id}">↻ Retry</button></div>`);
  }

  if (m.safety_warnings && m.safety_warnings.length) {
    parts.push(`<div class="warning-box"><strong>⚠ Safety warning from the manual</strong>${m.safety_warnings.map(escapeHtml).join("<br>")}</div>`);
  }
  if (m.conflict_note) {
    parts.push(`<div class="conflict-box"><strong>⚠ Documentation conflict</strong>${escapeHtml(m.conflict_note)}</div>`);
  }
  if (m.citations && m.citations.length) {
    parts.push(`<div class="citations">${m.citations.map(renderCitation).join("")}</div>`);
    parts.push(`<button class="ghost evidence-toggle" data-message-id="${m.id}" aria-expanded="${!!state.evidenceOpen[m.id]}">
      ${state.evidenceOpen[m.id] ? "Hide manual evidence" : "View manual evidence"}
    </button>`);
    if (state.evidenceOpen[m.id]) {
      parts.push(`<div class="evidence-panel">${m.citations.map(renderEvidenceEntry).join("<hr style='border-color:var(--border)'>")}</div>`);
    }
  }
  if (!m.is_clarifying_question) {
    parts.push(`<div class="msg-actions">
      <button class="ghost copy-btn" data-message-id="${m.id}">Copy</button>
      <button class="ghost feedback-btn" data-message-id="${m.id}" data-rating="helpful">👍 Helpful</button>
      <button class="ghost feedback-btn" data-message-id="${m.id}" data-rating="incorrect">👎 Report incorrect</button>
      <button class="ghost save-btn" data-message-id="${m.id}">☆ Save</button>
    </div>`);
  }
  parts.push("</div>");
  return parts.join("");
}

function renderEvidenceEntry(c) {
  const imgSrc = c.page_number ? `/api/manuals/${c.document_id}/pages/${c.page_number}/image` : null;
  return `
    <div>
      <strong>${escapeHtml(c.filename)}${c.page_number ? ", page " + c.page_number : ""}${c.section_heading ? " — " + escapeHtml(c.section_heading) : ""}</strong>
      <p>${escapeHtml(c.excerpt)}</p>
      ${imgSrc ? `<img src="${imgSrc}" alt="Scanned page ${c.page_number} of ${escapeHtml(c.filename)}, showing the cited table/diagram/text" loading="lazy" style="max-width:100%; border-radius:8px; border:1px solid var(--border); margin:6px 0;" onerror="this.remove()" />` : ""}
      <a href="/api/manuals/${c.document_id}/file#page=${c.page_number || 1}" target="_blank" rel="noopener">Open manual at this page ↗</a>
    </div>
  `;
}

function wireEvidenceToggles() {
  root.querySelectorAll(".evidence-toggle").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.messageId;
      state.evidenceOpen[id] = !state.evidenceOpen[id];
      render();
    });
  });
}

function previousUserQuestion(messageId) {
  const idx = state.messages.findIndex((m) => String(m.id) === String(messageId));
  for (let i = idx - 1; i >= 0; i--) {
    if (state.messages[i].role === "user") return state.messages[i].content;
  }
  return null;
}

async function confirmMachine(machineId, resendQuestion) {
  try {
    const conv = await api(`/api/conversations/${state.conversationId}/machine`, {
      method: "POST",
      body: JSON.stringify({ machine_id: machineId }),
    });
    state.machine = { id: conv.machine_id, label: conv.machine_label };
    if (resendQuestion) {
      await sendQuestion(resendQuestion);
    } else {
      render();
    }
  } catch (err) {
    alert("Could not set the machine: " + err.message);
  }
}

function wireMessageActions() {
  root.querySelectorAll(".clarify-option-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const messageId = btn.closest(".msg")?.dataset.messageId;
      confirmMachine(parseInt(btn.dataset.machineId, 10), previousUserQuestion(messageId));
    });
  });
  root.querySelectorAll(".clarify-pick-btn").forEach((btn) => {
    btn.addEventListener("click", () => { state.screen = "picker"; state.searchQuery = ""; render(); });
  });
  root.querySelectorAll(".retry-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const q = previousUserQuestion(btn.dataset.messageId);
      if (q) sendQuestion(q);
    });
  });
  root.querySelectorAll(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const m = state.messages.find((x) => String(x.id) === btn.dataset.messageId);
      if (m) navigator.clipboard?.writeText(m.content).then(() => flashButton(btn, "Copied!"));
    });
  });
  root.querySelectorAll(".feedback-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/messages/${btn.dataset.messageId}/feedback`, {
          method: "POST",
          body: JSON.stringify({ rating: btn.dataset.rating }),
        });
        flashButton(btn, "Thanks!");
      } catch (_) { flashButton(btn, "Failed"); }
    });
  });
  root.querySelectorAll(".save-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        await api(`/api/messages/${btn.dataset.messageId}/save`, { method: "POST" });
        flashButton(btn, "Saved");
      } catch (_) { flashButton(btn, "Failed"); }
    });
  });
}

function flashButton(btn, text) {
  const original = btn.textContent;
  btn.textContent = text;
  setTimeout(() => { btn.textContent = original; }, 1500);
}

async function sendQuestion(text) {
  state.messages.push({ id: `local-${Date.now()}`, role: "user", content: text });
  state.sending = true;
  render();

  try {
    const reply = await api(`/api/conversations/${state.conversationId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content: text }),
    });
    state.messages.push(reply);
    if (reply.is_clarifying_question && reply.clarifying_options?.length) {
      // Nothing further needed — options are shown as text; the technician can
      // just answer in their next message ("the AJ series one").
    }
  } catch (err) {
    state.messages.push({
      id: `err-${Date.now()}`,
      role: "assistant",
      content: err.offline
        ? "You're offline right now, so I can't search the manuals or generate an answer. Reconnect and try again."
        : `Something went wrong: ${err.message}`,
      is_no_answer: true,
    });
  } finally {
    state.sending = false;
    render();
  }
}

function setupMic() {
  const micBtn = document.getElementById("mic-btn");
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    micBtn.disabled = true;
    micBtn.title = "Voice input not supported in this browser";
    return;
  }
  const recognizer = new SpeechRecognition();
  recognizer.continuous = false;
  recognizer.interimResults = false;
  recognizer.lang = "en-US";

  micBtn.addEventListener("click", () => {
    if (micBtn.classList.contains("listening")) {
      recognizer.stop();
      return;
    }
    micBtn.classList.add("listening");
    recognizer.start();
  });
  recognizer.onresult = (event) => {
    const text = event.results[0][0].transcript;
    const input = document.getElementById("question-input");
    input.value = (input.value ? input.value + " " : "") + text;
  };
  recognizer.onend = () => micBtn.classList.remove("listening");
  recognizer.onerror = () => micBtn.classList.remove("listening");
}

// --- Rendering markdown-ish text (bold headers/lists from the extractive provider) ---

function renderMarkdownish(text) {
  const escaped = escapeHtml(text);
  return escaped
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/\n/g, "<br>");
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// --- Router -----------------------------------------------------------------

function render() {
  if (state.screen === "auth") return renderAuth();
  if (state.screen === "picker") return renderPicker();
  if (state.screen === "chat") return renderChat();
  root.innerHTML = `<div class="empty-state">Loading…</div>`;
}

boot();
