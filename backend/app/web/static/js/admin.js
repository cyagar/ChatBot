"use strict";

const state = { tab: "documents", user: null, documents: [], showDeactivated: false, duplicates: [], runs: [], ingestionStatus: null, feedback: [], unanswered: [], queryResult: null, machines: [], allMachines: [], reviewQueue: [], invitations: [], lastInvite: null };
const root = document.getElementById("admin-app");

async function api(path, options = {}) {
  const resp = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`;
    let correlationId = null;
    try {
      const body = await resp.json();
      detail = body.detail || detail;
      correlationId = body.correlation_id || null;
    } catch (_) {}
    // An expired/revoked admin session must not just 401 on whatever action
    // was in flight with no visible error and no path back to a usable
    // state (the only recovery would be a manual page reload). state.user
    // !== null means this wasn't boot()'s own initial,
    // expected-to-401-when-signed-out /api/auth/me probe.
    if (resp.status === 401 && state.user !== null) {
      state.user = null;
      renderLogin();
    }
    const err = new Error(correlationId ? `${detail} (ref: ${correlationId})` : detail);
    err.status = resp.status;
    throw err;
  }
  if (resp.status === 202 || resp.status === 204) return null;
  return resp.json();
}

// Every admin action reachable from here shows its own error instead of
// failing silently -- see guardedClick/guardedSubmit below, which route
// every thrown api() error here.
function showError(message) {
  const banner = document.getElementById("admin-error-banner");
  if (!banner) return;
  banner.textContent = message;
  banner.classList.remove("hidden");
}

function guardedClick(btn, handler) {
  btn.addEventListener("click", async (...args) => {
    if (btn.disabled) return;
    btn.disabled = true;
    try {
      await handler(...args);
    } catch (err) {
      showError(err.message || "Something went wrong. Please try again.");
    } finally {
      if (btn.isConnected) btn.disabled = false;
    }
  });
}

function guardedSubmit(form, handler) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const submitBtn = form.querySelector('button[type="submit"]');
    if (submitBtn?.disabled) return;
    if (submitBtn) submitBtn.disabled = true;
    try {
      await handler(e);
    } catch (err) {
      showError(err.message || "Something went wrong. Please try again.");
    } finally {
      if (submitBtn?.isConnected) submitBtn.disabled = false;
    }
  });
}

async function boot() {
  try {
    state.user = await api("/api/auth/me");
    if (state.user.role !== "administrator") {
      root.innerHTML = `<div class="empty-state">Administrator access required for this account.</div>`;
      return;
    }
  } catch (_) {
    // No valid session cookie. There is no other browser-based login form
    // (the technician client is Android-only) for a signed-out visitor to
    // land on, so /admin renders its own login form instead of assuming one
    // exists elsewhere.
    renderLogin();
    return;
  }
  await loadTab();
  render();
}

function renderLogin() {
  root.innerHTML = `
    <div class="login-shell">
      <h1 class="mb-sm">Admin sign in</h1>
      <p class="text-dim mt-0">Administrator accounts only.</p>
      <form id="login-form" class="edit-form">
        <label>Email <input name="email" type="email" required autofocus /></label>
        <label>Password <input name="password" type="password" required /></label>
        <div id="login-error" class="text-danger hidden"></div>
        <div><button type="submit" class="primary">Sign in</button></div>
      </form>
    </div>
  `;
  const form = document.getElementById("login-form");
  const errorEl = document.getElementById("login-error");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.classList.add("hidden");
    const fd = new FormData(form);
    try {
      await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: fd.get("email"), password: fd.get("password") }),
      });
      await boot();
    } catch (err) {
      errorEl.textContent = err.message;
      errorEl.classList.remove("hidden");
    }
  });
}

const TABS = [
  { id: "review", label: "Review queue" },
  { id: "documents", label: "Manuals & metadata" },
  { id: "duplicates", label: "Duplicates" },
  { id: "ingestion", label: "Ingestion reports" },
  { id: "access", label: "Invitations" },
  { id: "query", label: "Query tester" },
  { id: "feedback", label: "Feedback & gaps" },
];

async function loadTab() {
  if (state.tab === "review") state.reviewQueue = await api("/api/admin/review-queue");
  if (state.tab === "documents") {
    state.documents = await api(`/api/admin/documents${state.showDeactivated ? "?include_deactivated=true" : ""}`);
    if (state.allMachines.length === 0) state.allMachines = await api("/api/admin/machines");
  }
  if (state.tab === "duplicates") state.duplicates = await api("/api/admin/duplicates");
  if (state.tab === "ingestion") {
    state.runs = await api("/api/admin/ingestion/runs");
    state.ingestionStatus = await api("/api/admin/ingestion/status");
  }
  if (state.tab === "access") state.invitations = await api("/api/admin/invitations");
  if (state.tab === "feedback") {
    state.feedback = await api("/api/admin/feedback");
    state.unanswered = await api("/api/admin/unanswered");
  }
  if (state.tab === "query" && state.machines.length === 0) {
    // Uses /api/admin/machines, not the technician-facing
    // /api/machines?limit=500 -- that endpoint caps limit at 100
    // (routes_machines.py), which would fail with 422 on any corpus with
    // >100 machines before the Query tab could even render. The admin
    // endpoint has no such cap and already backs the metadata editor on the
    // Documents tab (same MachineOut response shape).
    state.machines = await api("/api/admin/machines");
  }
}

function render() {
  root.innerHTML = `
    <div class="admin-shell">
      <nav class="admin-nav">
        <div class="admin-nav-title">Admin</div>
        ${TABS.map((t) => `<button data-tab="${t.id}" class="${state.tab === t.id ? "active" : ""}">${t.label}</button>`).join("")}
        <div class="admin-nav-footer"><button id="logout-btn" class="ghost">Sign out</button></div>
      </nav>
      <main class="admin-main">
        <div id="admin-error-banner" class="banner error hidden"></div>
        ${renderTab()}
      </main>
    </div>
  `;
  root.querySelectorAll(".admin-nav button[data-tab]").forEach((btn) => {
    guardedClick(btn, async () => {
      state.tab = btn.dataset.tab;
      await loadTab();
      render();
    });
  });
  const logoutBtn = document.getElementById("logout-btn");
  if (logoutBtn) logoutBtn.addEventListener("click", async () => {
    try { await api("/api/auth/logout", { method: "POST" }); } catch (_) {}
    state.user = null;
    await boot();
  });
  wireTabEvents();
}

function renderTab() {
  if (state.tab === "review") return renderReviewQueue();
  if (state.tab === "documents") return renderDocuments();
  if (state.tab === "duplicates") return renderDuplicates();
  if (state.tab === "ingestion") return renderIngestion();
  if (state.tab === "access") return renderAccess();
  if (state.tab === "query") return renderQuery();
  if (state.tab === "feedback") return renderFeedback();
  return "";
}

// --- Review queue ---

function renderReviewQueue() {
  return `
    <h1>Review queue</h1>
    <p class="text-dim">A Drive edit alone never makes a document retrievable to technicians -- every document and every machine link needs an explicit approval here first. ${state.reviewQueue.length} item(s) need attention.</p>
    ${state.reviewQueue.length === 0 ? `<p>Nothing pending.</p>` : state.reviewQueue.map((d) => `
      <div class="card mb-md">
        <div class="row-between">
          <div>
            <strong>${esc(d.original_filename)}</strong>
            <div class="text-dim text-sm">${esc(d.manufacturer || "—")} · ${esc(d.doc_type || "—")} · ${esc(d.title || "—")}</div>
          </div>
          <span class="status-badge ${d.review_status}">${esc(d.review_status)}</span>
        </div>
        ${d.review_status !== "approved" ? `
          <div class="mt-sm">
            <button class="primary approve-doc-btn" data-doc="${d.id}">Approve document</button>
            <button class="ghost reject-doc-btn" data-doc="${d.id}">Reject document</button>
          </div>
        ` : ""}
        ${d.links.length > 0 ? `
          <table class="admin-table mt-md">
            <thead><tr><th>Machine</th><th>Confidence</th><th>Link status</th><th></th></tr></thead>
            <tbody>
              ${d.links.map((l) => `
                <tr>
                  <td>${esc(l.manufacturer)} — ${esc(l.model_name)}</td>
                  <td>${l.confidence.toFixed(2)}</td>
                  <td><span class="status-badge ${l.review_status}">${esc(l.review_status)}</span></td>
                  <td>
                    ${l.review_status !== "approved" ? `<button class="ghost approve-link-btn" data-doc="${d.id}" data-machine="${l.machine_id}">Approve link</button>` : ""}
                    ${l.review_status !== "rejected" ? `<button class="ghost reject-link-btn" data-doc="${d.id}" data-machine="${l.machine_id}">Reject link</button>` : ""}
                  </td>
                </tr>
              `).join("")}
            </tbody>
          </table>
        ` : `<p class="text-dim mt-sm">No machine links proposed yet.</p>`}
      </div>
    `).join("")}
  `;
}

// --- Invitations ---

function renderAccess() {
  return `
    <h1>Invitations</h1>
    <p class="text-dim">Registration requires an invitation -- there is no public sign-up. Share the link with the invited technician out of band (e.g. in person, by phone, or via your own messaging tool); it is shown only once.</p>
    <form id="invite-form" class="edit-form max-w-lg">
      <label>Email <input name="email" type="email" required /></label>
      <label>Role
        <select name="role">
          <option value="technician" selected>Technician</option>
          <option value="administrator">Administrator</option>
        </select>
      </label>
      <label>Expires in (hours) <input name="expires_in_hours" type="number" value="72" min="1" max="720" /></label>
      <div><button type="submit" class="primary">Create invitation</button></div>
    </form>
    ${state.lastInvite ? `
      <div class="card mt-md">
        <strong>Invitation created for ${esc(state.lastInvite.email)}</strong>
        <p class="text-sm text-dim">Copy this link and send it to them directly -- it will not be shown again.</p>
        <input id="invite-link-input" readonly class="w-full" value="${escAttr(state.lastInvite.link)}" />
      </div>
    ` : ""}
    <table class="admin-table mt-lg">
      <thead><tr><th>Email</th><th>Role</th><th>Created</th><th>Expires</th><th>Status</th><th></th></tr></thead>
      <tbody>
        ${state.invitations.map((i) => {
          const invStatus = i.used_at ? "used" : i.revoked_at ? "revoked" : "pending";
          return `
          <tr>
            <td>${esc(i.email)}</td><td>${esc(i.role)}</td><td>${esc(i.created_at)}</td><td>${esc(i.expires_at)}</td>
            <td><span class="status-badge ${invStatus}">${invStatus}</span></td>
            <td>${invStatus === "pending" ? `<button class="ghost revoke-invite-btn" data-id="${i.id}">Revoke</button>` : ""}</td>
          </tr>
        `;
        }).join("") || `<tr><td colspan="6">No invitations yet.</td></tr>`}
      </tbody>
    </table>
  `;
}

// --- Documents / metadata correction ---

function renderDocuments() {
  const activeCount = state.documents.filter((d) => !d.deactivated_at).length;
  return `
    <h1>Manuals &amp; metadata</h1>
    <p class="text-dim">${activeCount} active document(s). Correct auto-detected metadata below — every edit is logged for audit.</p>
    <label class="text-sm"><input type="checkbox" id="show-deactivated-toggle" ${state.showDeactivated ? "checked" : ""} /> Show deactivated (for reactivation/rollback)</label>
    <table class="admin-table">
      <thead><tr><th>File</th><th>Status</th><th>Review</th><th>Manufacturer</th><th>Doc type</th><th>Title</th><th>Revision</th><th>Machines</th><th></th></tr></thead>
      <tbody>
        ${state.documents.map((d) => `
          <tr${d.deactivated_at ? ' class="deactivated-row"' : ""}>
            <td>${esc(d.original_filename)}<br><span class="text-dim text-sm">${d.file_type} · ${d.page_count ?? "?"} pages${d.is_current_revision ? "" : " · SUPERSEDED"}${d.deactivated_at ? " · DEACTIVATED" : ""}</span></td>
            <td><span class="status-badge ${d.status}">${d.status}</span>${d.status_reason ? `<div class="text-xs text-dim max-w-xs">${esc(d.status_reason)}</div>` : ""}</td>
            <td><span class="status-badge ${d.review_status}">${esc(d.review_status)}</span></td>
            <td>${esc(d.manufacturer || "—")}</td>
            <td>${esc(d.doc_type || "—")}</td>
            <td>${esc(d.title || "—")}</td>
            <td>${esc(d.revision || "—")}</td>
            <td>${d.machines.map(esc).join(", ") || "—"}</td>
            <td>
              <button class="ghost edit-doc-btn" data-id="${d.id}">Edit</button>
              ${d.deactivated_at
                ? `<button class="ghost reactivate-btn" data-id="${d.id}">Reactivate</button>`
                : `<button class="ghost deactivate-btn" data-id="${d.id}">Deactivate</button>`}
            </td>
          </tr>
          <tr class="edit-row hidden" data-edit-for="${d.id}">
            <td colspan="9">
              <form class="edit-form" data-id="${d.id}">
                <label>Manufacturer <input name="manufacturer_name" value="${escAttr(d.manufacturer || "")}" /></label>
                <label>Doc type
                  <select name="doc_type">
                    ${["service_repair","parts","installation_operating","programming","use_and_care","spec_sheet","training","brochure","unknown"]
                      .map((t) => `<option value="${t}" ${t === d.doc_type ? "selected" : ""}>${t}</option>`).join("")}
                  </select>
                </label>
                <label>Title <input name="title" value="${escAttr(d.title || "")}" /></label>
                <label>Revision <input name="revision" value="${escAttr(d.revision || "")}" /></label>
                <label><input type="checkbox" name="is_current_revision" ${d.is_current_revision ? "checked" : ""} /> Current revision (preferred in search)</label>
                <label>Machine association(s) — retrieval only ever returns a document for a machine linked here
                  <!-- The review-status badge below makes a rejected link visible before an admin decides
                       to touch it, and the "machine-picker-touched" flag set by the change listener further
                       down gates whether machine_ids is sent in the PATCH payload at all -- otherwise a pure
                       title/revision correction would silently re-approve a previously-rejected link, since
                       every Save would send the full checked set regardless of whether the admin touched it. -->
                  <input type="text" class="machine-filter" data-doc="${d.id}" placeholder="Filter machines…" />
                  <div class="machine-picker" data-doc="${d.id}">
                    ${state.allMachines.map((m) => {
                      const linkStatus = d.machine_link_review_status[m.id];
                      const badge = linkStatus && linkStatus !== "approved"
                        ? ` <span class="status-badge ${linkStatus} text-xs">${esc(linkStatus)}</span>`
                        : "";
                      return `
                      <label class="machine-option" data-search="${escAttr(`${m.manufacturer} ${m.model_name} ${m.family || ""}`).toLowerCase()}">
                        <input type="checkbox" name="machine_ids" value="${m.id}" ${d.machine_ids.includes(m.id) ? "checked" : ""} />
                        ${esc(m.manufacturer)} — ${esc(m.model_name)}${m.family ? ` <span class="text-dim">(${esc(m.family)})</span>` : ""}${badge}
                      </label>
                    `;}).join("")}
                  </div>
                </label>
                <label>Reason for change (required) <textarea name="reason" required rows="2"></textarea></label>
                <div><button type="submit" class="primary">Save correction</button></div>
              </form>
            </td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

// --- Duplicates ---

function renderDuplicates() {
  return `
    <h1>Duplicates</h1>
    <p class="text-dim">${state.duplicates.length} duplicate match(es) detected during ingestion.</p>
    <table class="admin-table">
      <thead><tr><th>Kept (current)</th><th>Duplicate</th><th>Match type</th><th>Similarity</th><th>Detected</th></tr></thead>
      <tbody>
        ${state.duplicates.map((d) => `
          <tr>
            <td>#${d.kept_id} ${esc(d.kept_name)}</td>
            <td>#${d.dup_id} ${esc(d.dup_name)}</td>
            <td>${esc(d.match_type)}</td>
            <td>${d.similarity != null ? d.similarity.toFixed(2) : "—"}</td>
            <td>${esc(d.detected_at)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

// --- Ingestion reports ---

function renderIngestionStatusBanner() {
  const s = state.ingestionStatus;
  if (!s) return "";

  if (s.is_stale) {
    const since = s.last_success_at
      ? `${Math.round(s.hours_since_last_success)}h ago (run #${s.last_success_run_id}, ${esc(s.last_success_trigger)})`
      : "never";
    return `
      <div class="banner error">
        <strong>Corpus is stale.</strong> Last successful sync: ${since}.
        This exceeds the ${s.staleness_threshold_hours}h freshness SLA.
        ${s.scheduler_enabled ? "" : " The automated scheduler is currently disabled -- only manual re-index will refresh the corpus."}
      </div>`;
  }
  const hadErrors = s.last_success_status === "completed_with_errors";
  return `
    <div class="banner ok">
      Corpus last synced ${Math.round(s.hours_since_last_success * 10) / 10}h ago
      (run #${s.last_success_run_id}, ${esc(s.last_success_trigger)}) -- ${s.active_document_count} active manuals.
      ${s.scheduler_enabled
        ? `Automated sync runs every ${Math.round(s.sync_interval_minutes / 60 * 10) / 10}h.`
        : "Automated sync is disabled; manual re-index is the only freshness mechanism right now."}
      ${hadErrors ? ` <strong>That run completed with some individual file failures -- check the run list below.</strong>` : ""}
    </div>`;
}

function renderIngestion() {
  return `
    <h1>Ingestion reports</h1>
    ${renderIngestionStatusBanner()}
    <div class="card">
      <button id="reindex-btn" class="primary">Run re-index now</button>
      <span class="text-dim ml-sm">Add manuals to the shared Google Drive folder first, then run this. Runs in the background; refresh this tab to see progress.</span>
    </div>
    <table class="admin-table">
      <thead><tr><th>Run</th><th>Started</th><th>Finished</th><th>Trigger</th><th>Status</th><th>Event counts</th><th></th></tr></thead>
      <tbody>
        ${state.runs.map((r) => `
          <tr>
            <td>#${r.id}</td><td>${esc(r.started_at)}</td><td>${esc(r.finished_at || "—")}</td>
            <td>${esc(r.trigger || "manual")}</td>
            <td>${esc(r.status)}</td>
            <td>${Object.entries(r.counts).map(([k, v]) => `${k}: ${v}`).join(", ")}</td>
            <td><button class="ghost view-report-btn" data-run="${r.id}">View report</button></td>
          </tr>
          <tr class="report-row hidden" data-report-for="${r.id}"><td colspan="7"></td></tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

// --- Query tester ---

function renderQuery() {
  return `
    <h1>Query tester</h1>
    <p class="text-dim">Run a question through hybrid retrieval and inspect the exact passages, before any answer is generated.</p>
    <form id="query-form" class="edit-form max-w-xl">
      <label>Question <textarea name="question" rows="2" required>Why is this brewer not heating?</textarea></label>
      <label>Machine (optional filter)
        <select name="machine_id">
          <option value="">— no filter —</option>
          ${state.machines.map((m) => `<option value="${m.id}">${esc(m.manufacturer)} — ${esc(m.model_name)}</option>`).join("")}
        </select>
      </label>
      <div><button type="submit" class="primary">Run retrieval</button></div>
    </form>
    <div id="query-results" class="mt-lg">
      ${state.queryResult ? state.queryResult.passages.map((p) => `
        <div class="passage-card">
          <div class="score">chunk #${p.chunk_id} · ${esc(p.chunk_type)} · doc #${p.document_id} ${esc(p.filename)}${p.page_number ? ", p." + p.page_number : ""}
            · lexical=${p.lexical_score.toFixed(3)} vector=${p.vector_score.toFixed(3)} combined=${p.combined_score.toFixed(3)}
            ${p.is_current_revision ? "" : " · SUPERSEDED"}</div>
          <div>${esc(p.content).slice(0, 600)}${p.content.length > 600 ? "…" : ""}</div>
        </div>
      `).join("") || "<p>No passages returned.</p>" : ""}
    </div>
  `;
}

// --- Feedback & gaps ---

function renderFeedback() {
  return `
    <h1>Feedback &amp; unanswered questions</h1>
    <h2 class="h2-inline">Technician feedback</h2>
    <table class="admin-table">
      <!-- Machine, provider, citations, and the message/conversation ids are
           shown here so an admin triaging an "incorrect" report can see what
           the technician was actually asking about without separately
           hunting down the conversation. -->
      <thead><tr><th>When</th><th>User</th><th>Machine</th><th>Rating</th><th>Comment</th><th>Answer</th><th>Citations</th><th>IDs</th></tr></thead>
      <tbody>${state.feedback.map((f) => `
        <tr>
          <td>${esc(f.created_at)}</td>
          <td>${esc(f.user_email)}</td>
          <td>${esc(f.machine_label || "—")}</td>
          <td>${esc(f.rating)}</td>
          <td>${esc(f.comment || "—")}</td>
          <td class="max-w-sm">${esc((f.answer_content || "").slice(0, 200))}${(f.answer_content || "").length > 200 ? "…" : ""}</td>
          <td>${f.citations && f.citations.length ? f.citations.map((c) => esc(c)).join(", ") : "—"}</td>
          <td class="text-dim text-sm">msg ${f.message_id} · conv ${f.conversation_id}${f.provider ? " · " + esc(f.provider) : ""}</td>
        </tr>
      `).join("") || `<tr><td colspan="8">No feedback yet.</td></tr>`}</tbody>
    </table>
    <h2 class="h2-inline mt-xl">Frequently unanswered questions</h2>
    <table class="admin-table">
      <thead><tr><th>When</th><th>Question</th></tr></thead>
      <tbody>${state.unanswered.map((u) => `
        <tr><td>${esc(u.created_at)}</td><td>${esc(u.question || "—")}</td></tr>
      `).join("") || `<tr><td colspan="2">No unanswered questions logged.</td></tr>`}</tbody>
    </table>
  `;
}

// --- Event wiring ---

function wireTabEvents() {
  // Wired here, not as an inline this.select() event-handler attribute --
  // CSP's script-src 'self' (no unsafe-inline) silently drops that kind of
  // attribute.
  const inviteLinkInput = document.getElementById("invite-link-input");
  if (inviteLinkInput) inviteLinkInput.addEventListener("click", () => inviteLinkInput.select());

  root.querySelectorAll(".approve-doc-btn").forEach((btn) => {
    guardedClick(btn, async () => {
      await api(`/api/admin/documents/${btn.dataset.doc}/review`, { method: "POST", body: JSON.stringify({ decision: "approved" }) });
      state.reviewQueue = await api("/api/admin/review-queue");
      render();
    });
  });
  root.querySelectorAll(".reject-doc-btn").forEach((btn) => {
    guardedClick(btn, async () => {
      if (!confirm("Reject this document? It will never be used to answer technician questions.")) return;
      await api(`/api/admin/documents/${btn.dataset.doc}/review`, { method: "POST", body: JSON.stringify({ decision: "rejected" }) });
      state.reviewQueue = await api("/api/admin/review-queue");
      render();
    });
  });
  root.querySelectorAll(".approve-link-btn").forEach((btn) => {
    guardedClick(btn, async () => {
      await api(`/api/admin/documents/${btn.dataset.doc}/machines/${btn.dataset.machine}/review`,
        { method: "POST", body: JSON.stringify({ decision: "approved" }) });
      state.reviewQueue = await api("/api/admin/review-queue");
      render();
    });
  });
  root.querySelectorAll(".reject-link-btn").forEach((btn) => {
    guardedClick(btn, async () => {
      await api(`/api/admin/documents/${btn.dataset.doc}/machines/${btn.dataset.machine}/review`,
        { method: "POST", body: JSON.stringify({ decision: "rejected" }) });
      state.reviewQueue = await api("/api/admin/review-queue");
      render();
    });
  });

  const inviteForm = document.getElementById("invite-form");
  if (inviteForm) guardedSubmit(inviteForm, async () => {
    const fd = new FormData(inviteForm);
    const invite = await api("/api/admin/invitations", {
      method: "POST",
      body: JSON.stringify({
        email: fd.get("email"),
        role: fd.get("role"),
        expires_in_hours: parseInt(fd.get("expires_in_hours"), 10) || 72,
      }),
    });
    // /invite is a real route (app/main.py) serving a minimal HTML
    // redemption page (invite.html) that calls POST /api/auth/register
    // directly.
    const link = `${window.location.origin}/invite?token=${encodeURIComponent(invite.token)}&email=${encodeURIComponent(invite.email)}`;
    state.lastInvite = { email: invite.email, link };
    state.invitations = await api("/api/admin/invitations");
    render();
  });
  root.querySelectorAll(".revoke-invite-btn").forEach((btn) => {
    guardedClick(btn, async () => {
      if (!confirm("Revoke this invitation? The link will stop working.")) return;
      await api(`/api/admin/invitations/${btn.dataset.id}/revoke`, { method: "POST" });
      state.invitations = await api("/api/admin/invitations");
      render();
    });
  });

  root.querySelectorAll(".edit-doc-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = root.querySelector(`.edit-row[data-edit-for="${btn.dataset.id}"]`);
      row.classList.toggle("hidden");
    });
  });
  root.querySelectorAll(".deactivate-btn").forEach((btn) => {
    guardedClick(btn, async () => {
      if (!confirm("Deactivate this manual? It will be removed from search but kept for audit.")) return;
      await api(`/api/admin/documents/${btn.dataset.id}/deactivate`, { method: "POST" });
      state.documents = await api(`/api/admin/documents${state.showDeactivated ? "?include_deactivated=true" : ""}`);
      render();
    });
  });
  root.querySelectorAll(".reactivate-btn").forEach((btn) => {
    guardedClick(btn, async () => {
      if (!confirm("Reactivate this manual? It will become searchable again.")) return;
      await api(`/api/admin/documents/${btn.dataset.id}/reactivate`, { method: "POST" });
      state.documents = await api(`/api/admin/documents${state.showDeactivated ? "?include_deactivated=true" : ""}`);
      render();
    });
  });
  root.querySelector("#show-deactivated-toggle")?.addEventListener("change", async (e) => {
    state.showDeactivated = e.target.checked;
    state.documents = await api(`/api/admin/documents${state.showDeactivated ? "?include_deactivated=true" : ""}`);
    render();
  });
  root.querySelectorAll(".edit-form[data-id]").forEach((form) => {
    // Only a genuine interaction with the machine picker marks it touched --
    // programmatic pre-checking on render (see renderDocuments) never fires
    // a "change" event, so this stays false for an edit that never went near
    // the machine list at all.
    const picker = form.querySelector(".machine-picker");
    picker?.addEventListener("change", () => { form.dataset.machinesTouched = "true"; }, { once: true });

    guardedSubmit(form, async () => {
      const fd = new FormData(form);
      const payload = {
        manufacturer_name: fd.get("manufacturer_name") || null,
        doc_type: fd.get("doc_type") || null,
        title: fd.get("title") || null,
        revision: fd.get("revision") || null,
        is_current_revision: fd.get("is_current_revision") === "on",
        // machine_ids is omitted entirely unless the admin actually touched
        // the picker this session -- sending it unconditionally would turn
        // every metadata-only correction (title, revision, doc type) into
        // an implicit re-approval of whatever happened to be checked,
        // including previously-rejected links (the picker pre-checks every
        // existing link, rejected ones included). The backend's PATCH
        // handler treats a present machine_ids as "this IS the admin's
        // deliberate human review" -- correct when the admin actually meant
        // it, so this field must only be sent when they did.
        machine_ids: form.dataset.machinesTouched === "true"
          ? fd.getAll("machine_ids").map((v) => parseInt(v, 10))
          : null,
        reason: fd.get("reason"),
      };
      await api(`/api/admin/documents/${form.dataset.id}`, { method: "PATCH", body: JSON.stringify(payload) });
      state.documents = await api(`/api/admin/documents${state.showDeactivated ? "?include_deactivated=true" : ""}`);
      render();
    });
  });
  root.querySelectorAll(".machine-filter").forEach((input) => {
    input.addEventListener("input", () => {
      const picker = root.querySelector(`.machine-picker[data-doc="${input.dataset.doc}"]`);
      const q = input.value.trim().toLowerCase();
      picker.querySelectorAll(".machine-option").forEach((opt) => {
        opt.style.display = opt.dataset.search.includes(q) ? "" : "none";
      });
    });
  });

  const reindexBtn = document.getElementById("reindex-btn");
  if (reindexBtn) guardedClick(reindexBtn, async () => {
    await api("/api/admin/ingestion/reindex", { method: "POST" });
    alert("Re-index started in the background.");
  });
  root.querySelectorAll(".view-report-btn").forEach((btn) => {
    guardedClick(btn, async () => {
      const row = root.querySelector(`.report-row[data-report-for="${btn.dataset.run}"]`);
      const cell = row.querySelector("td");
      if (!row.classList.contains("hidden")) { row.classList.add("hidden"); return; }
      const report = await api(`/api/admin/ingestion/runs/${btn.dataset.run}/report`);
      cell.innerHTML = `<table class="admin-table"><thead><tr><th>File</th><th>Event</th><th>Detail</th></tr></thead><tbody>
        ${report.files.map((f) => `<tr><td>${esc(f.original_filename)}</td><td><span class="status-badge ${f.event}">${esc(f.event)}</span></td><td>${esc(f.detail || "")}</td></tr>`).join("")}
      </tbody></table>`;
      row.classList.remove("hidden");
    });
  });

  const queryForm = document.getElementById("query-form");
  if (queryForm) guardedSubmit(queryForm, async () => {
    const fd = new FormData(queryForm);
    state.queryResult = await api("/api/admin/query-test", {
      method: "POST",
      body: JSON.stringify({
        question: fd.get("question"),
        machine_id: fd.get("machine_id") ? parseInt(fd.get("machine_id"), 10) : null,
      }),
    });
    render();
  });
}

function esc(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

// esc() escapes text-node content (&, <, >) but not quote characters,
// because a text node never needs them escaped -- an ATTRIBUTE value does.
// Used everywhere esc()'s result is
// interpolated inside a "..." HTML attribute (value=, data-search=), where
// an unescaped double quote in admin/PDF-derived data (title, revision,
// manufacturer, machine family) truncates the attribute and lets the rest
// of the string inject new attributes onto that element.
function escAttr(str) {
  return esc(str).replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

boot();
