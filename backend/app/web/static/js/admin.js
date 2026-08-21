"use strict";

const state = { tab: "documents", user: null, documents: [], duplicates: [], runs: [], feedback: [], unanswered: [], queryResult: null, machines: [], allMachines: [], reviewQueue: [], invitations: [], lastInvite: null };
const root = document.getElementById("admin-app");

async function api(path, options = {}) {
  const resp = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`;
    try { detail = (await resp.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  if (resp.status === 202 || resp.status === 204) return null;
  return resp.json();
}

async function boot() {
  try {
    state.user = await api("/api/auth/me");
    if (state.user.role !== "administrator") {
      root.innerHTML = `<div class="empty-state" style="padding:40px;">Administrator access required. <a href="/">Back to chat</a></div>`;
      return;
    }
  } catch (_) {
    window.location.href = "/";
    return;
  }
  await loadTab();
  render();
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
    state.documents = await api("/api/admin/documents");
    if (state.allMachines.length === 0) state.allMachines = await api("/api/admin/machines");
  }
  if (state.tab === "duplicates") state.duplicates = await api("/api/admin/duplicates");
  if (state.tab === "ingestion") state.runs = await api("/api/admin/ingestion/runs");
  if (state.tab === "access") state.invitations = await api("/api/admin/invitations");
  if (state.tab === "feedback") {
    state.feedback = await api("/api/admin/feedback");
    state.unanswered = await api("/api/admin/unanswered");
  }
  if (state.tab === "query" && state.machines.length === 0) {
    state.machines = await api("/api/machines?limit=500");
  }
}

function render() {
  root.innerHTML = `
    <div class="admin-shell">
      <nav class="admin-nav">
        <div style="padding:10px 10px 20px; font-weight:700;">Admin</div>
        ${TABS.map((t) => `<button data-tab="${t.id}" class="${state.tab === t.id ? "active" : ""}">${t.label}</button>`).join("")}
        <div style="margin-top:auto; padding-top:20px;"><a href="/">← Back to chat</a></div>
      </nav>
      <main class="admin-main">${renderTab()}</main>
    </div>
  `;
  root.querySelectorAll(".admin-nav button[data-tab]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      state.tab = btn.dataset.tab;
      await loadTab();
      render();
    });
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
    <p style="color:var(--text-dim)">A Drive edit alone never makes a document retrievable to technicians -- every document and every machine link needs an explicit approval here first. ${state.reviewQueue.length} item(s) need attention.</p>
    ${state.reviewQueue.length === 0 ? `<p>Nothing pending.</p>` : state.reviewQueue.map((d) => `
      <div class="card" style="margin-bottom:14px;">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:12px;">
          <div>
            <strong>${esc(d.original_filename)}</strong>
            <div style="color:var(--text-dim); font-size:0.85rem;">${esc(d.manufacturer || "—")} · ${esc(d.doc_type || "—")} · ${esc(d.title || "—")}</div>
          </div>
          <span class="status-badge ${d.review_status}">${esc(d.review_status)}</span>
        </div>
        ${d.review_status !== "approved" ? `
          <div style="margin-top:8px;">
            <button class="primary approve-doc-btn" data-doc="${d.id}">Approve document</button>
            <button class="ghost reject-doc-btn" data-doc="${d.id}">Reject document</button>
          </div>
        ` : ""}
        ${d.links.length > 0 ? `
          <table class="admin-table" style="margin-top:10px;">
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
        ` : `<p style="color:var(--text-dim); margin-top:8px;">No machine links proposed yet.</p>`}
      </div>
    `).join("")}
  `;
}

// --- Invitations ---

function renderAccess() {
  return `
    <h1>Invitations</h1>
    <p style="color:var(--text-dim)">Registration requires an invitation -- there is no public sign-up. Share the link with the invited technician out of band (e.g. in person, by phone, or via your own messaging tool); it is shown only once.</p>
    <form id="invite-form" class="edit-form" style="max-width:480px;">
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
      <div class="card" style="margin-top:14px;">
        <strong>Invitation created for ${esc(state.lastInvite.email)}</strong>
        <p style="font-size:0.85rem; color:var(--text-dim);">Copy this link and send it to them directly -- it will not be shown again.</p>
        <input readonly style="width:100%;" value="${esc(state.lastInvite.link)}" onclick="this.select()" />
      </div>
    ` : ""}
    <table class="admin-table" style="margin-top:20px;">
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
  return `
    <h1>Manuals &amp; metadata</h1>
    <p style="color:var(--text-dim)">${state.documents.length} active document(s). Correct auto-detected metadata below — every edit is logged for audit.</p>
    <table class="admin-table">
      <thead><tr><th>File</th><th>Status</th><th>Review</th><th>Manufacturer</th><th>Doc type</th><th>Title</th><th>Revision</th><th>Machines</th><th></th></tr></thead>
      <tbody>
        ${state.documents.map((d) => `
          <tr>
            <td>${esc(d.original_filename)}<br><span style="color:var(--text-dim);font-size:0.8rem;">${d.file_type} · ${d.page_count ?? "?"} pages${d.is_current_revision ? "" : " · SUPERSEDED"}</span></td>
            <td><span class="status-badge ${d.status}">${d.status}</span>${d.status_reason ? `<div style="font-size:0.78rem;color:var(--text-dim);max-width:220px;">${esc(d.status_reason)}</div>` : ""}</td>
            <td><span class="status-badge ${d.review_status}">${esc(d.review_status)}</span></td>
            <td>${esc(d.manufacturer || "—")}</td>
            <td>${esc(d.doc_type || "—")}</td>
            <td>${esc(d.title || "—")}</td>
            <td>${esc(d.revision || "—")}</td>
            <td>${d.machines.map(esc).join(", ") || "—"}</td>
            <td>
              <button class="ghost edit-doc-btn" data-id="${d.id}">Edit</button>
              <button class="ghost deactivate-btn" data-id="${d.id}">Deactivate</button>
            </td>
          </tr>
          <tr class="edit-row" data-edit-for="${d.id}" style="display:none;">
            <td colspan="9">
              <form class="edit-form" data-id="${d.id}">
                <label>Manufacturer <input name="manufacturer_name" value="${esc(d.manufacturer || "")}" /></label>
                <label>Doc type
                  <select name="doc_type">
                    ${["service_repair","parts","installation_operating","programming","use_and_care","spec_sheet","training","brochure","unknown"]
                      .map((t) => `<option value="${t}" ${t === d.doc_type ? "selected" : ""}>${t}</option>`).join("")}
                  </select>
                </label>
                <label>Title <input name="title" value="${esc(d.title || "")}" /></label>
                <label>Revision <input name="revision" value="${esc(d.revision || "")}" /></label>
                <label><input type="checkbox" name="is_current_revision" ${d.is_current_revision ? "checked" : ""} /> Current revision (preferred in search)</label>
                <label>Machine association(s) — retrieval only ever returns a document for a machine linked here
                  <input type="text" class="machine-filter" data-doc="${d.id}" placeholder="Filter machines…" />
                  <div class="machine-picker" data-doc="${d.id}">
                    ${state.allMachines.map((m) => `
                      <label class="machine-option" data-search="${esc(`${m.manufacturer} ${m.model_name} ${m.family || ""}`).toLowerCase()}">
                        <input type="checkbox" name="machine_ids" value="${m.id}" ${d.machine_ids.includes(m.id) ? "checked" : ""} />
                        ${esc(m.manufacturer)} — ${esc(m.model_name)}${m.family ? ` <span style="color:var(--text-dim);">(${esc(m.family)})</span>` : ""}
                      </label>
                    `).join("")}
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
    <p style="color:var(--text-dim)">${state.duplicates.length} duplicate match(es) detected during ingestion.</p>
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

function renderIngestion() {
  return `
    <h1>Ingestion reports</h1>
    <div class="card">
      <button id="reindex-btn" class="primary">Run re-index now</button>
      <span style="color:var(--text-dim); margin-left:10px;">Add manuals to the shared Google Drive folder first, then run this. Runs in the background; refresh this tab to see progress.</span>
    </div>
    <table class="admin-table">
      <thead><tr><th>Run</th><th>Started</th><th>Finished</th><th>Status</th><th>Event counts</th><th></th></tr></thead>
      <tbody>
        ${state.runs.map((r) => `
          <tr>
            <td>#${r.id}</td><td>${esc(r.started_at)}</td><td>${esc(r.finished_at || "—")}</td>
            <td>${esc(r.status)}</td>
            <td>${Object.entries(r.counts).map(([k, v]) => `${k}: ${v}`).join(", ")}</td>
            <td><button class="ghost view-report-btn" data-run="${r.id}">View report</button></td>
          </tr>
          <tr class="report-row" data-report-for="${r.id}" style="display:none;"><td colspan="6"></td></tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

// --- Query tester ---

function renderQuery() {
  return `
    <h1>Query tester</h1>
    <p style="color:var(--text-dim)">Run a question through hybrid retrieval and inspect the exact passages, before any answer is generated.</p>
    <form id="query-form" class="edit-form" style="max-width:600px;">
      <label>Question <textarea name="question" rows="2" required>Why is this brewer not heating?</textarea></label>
      <label>Machine (optional filter)
        <select name="machine_id">
          <option value="">— no filter —</option>
          ${state.machines.map((m) => `<option value="${m.id}">${esc(m.manufacturer)} — ${esc(m.model_name)}</option>`).join("")}
        </select>
      </label>
      <div><button type="submit" class="primary">Run retrieval</button></div>
    </form>
    <div id="query-results" style="margin-top:16px;">
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
    <h2 style="font-size:1.05rem;">Technician feedback</h2>
    <table class="admin-table">
      <thead><tr><th>When</th><th>User</th><th>Rating</th><th>Comment</th></tr></thead>
      <tbody>${state.feedback.map((f) => `
        <tr><td>${esc(f.created_at)}</td><td>${esc(f.user_email)}</td><td>${esc(f.rating)}</td><td>${esc(f.comment || "—")}</td></tr>
      `).join("") || `<tr><td colspan="4">No feedback yet.</td></tr>`}</tbody>
    </table>
    <h2 style="font-size:1.05rem; margin-top:24px;">Frequently unanswered questions</h2>
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
  root.querySelectorAll(".approve-doc-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/admin/documents/${btn.dataset.doc}/review`, { method: "POST", body: JSON.stringify({ decision: "approved" }) });
      state.reviewQueue = await api("/api/admin/review-queue");
      render();
    });
  });
  root.querySelectorAll(".reject-doc-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Reject this document? It will never be used to answer technician questions.")) return;
      await api(`/api/admin/documents/${btn.dataset.doc}/review`, { method: "POST", body: JSON.stringify({ decision: "rejected" }) });
      state.reviewQueue = await api("/api/admin/review-queue");
      render();
    });
  });
  root.querySelectorAll(".approve-link-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/admin/documents/${btn.dataset.doc}/machines/${btn.dataset.machine}/review`,
        { method: "POST", body: JSON.stringify({ decision: "approved" }) });
      state.reviewQueue = await api("/api/admin/review-queue");
      render();
    });
  });
  root.querySelectorAll(".reject-link-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/admin/documents/${btn.dataset.doc}/machines/${btn.dataset.machine}/review`,
        { method: "POST", body: JSON.stringify({ decision: "rejected" }) });
      state.reviewQueue = await api("/api/admin/review-queue");
      render();
    });
  });

  const inviteForm = document.getElementById("invite-form");
  if (inviteForm) inviteForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(inviteForm);
    try {
      const invite = await api("/api/admin/invitations", {
        method: "POST",
        body: JSON.stringify({
          email: fd.get("email"),
          role: fd.get("role"),
          expires_in_hours: parseInt(fd.get("expires_in_hours"), 10) || 72,
        }),
      });
      const link = `${window.location.origin}/?invite=${encodeURIComponent(invite.token)}&email=${encodeURIComponent(invite.email)}`;
      state.lastInvite = { email: invite.email, link };
      state.invitations = await api("/api/admin/invitations");
      render();
    } catch (err) {
      alert(err.message);
    }
  });
  root.querySelectorAll(".revoke-invite-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Revoke this invitation? The link will stop working.")) return;
      await api(`/api/admin/invitations/${btn.dataset.id}/revoke`, { method: "POST" });
      state.invitations = await api("/api/admin/invitations");
      render();
    });
  });

  root.querySelectorAll(".edit-doc-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const row = root.querySelector(`.edit-row[data-edit-for="${btn.dataset.id}"]`);
      row.style.display = row.style.display === "none" ? "table-row" : "none";
    });
  });
  root.querySelectorAll(".deactivate-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Deactivate this manual? It will be removed from search but kept for audit.")) return;
      await api(`/api/admin/documents/${btn.dataset.id}/deactivate`, { method: "POST" });
      state.documents = await api("/api/admin/documents");
      render();
    });
  });
  root.querySelectorAll(".edit-form[data-id]").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(form);
      const payload = {
        manufacturer_name: fd.get("manufacturer_name") || null,
        doc_type: fd.get("doc_type") || null,
        title: fd.get("title") || null,
        revision: fd.get("revision") || null,
        is_current_revision: fd.get("is_current_revision") === "on",
        machine_ids: fd.getAll("machine_ids").map((v) => parseInt(v, 10)),
        reason: fd.get("reason"),
      };
      await api(`/api/admin/documents/${form.dataset.id}`, { method: "PATCH", body: JSON.stringify(payload) });
      state.documents = await api("/api/admin/documents");
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
  if (reindexBtn) reindexBtn.addEventListener("click", async () => {
    await api("/api/admin/ingestion/reindex", { method: "POST" });
    alert("Re-index started in the background.");
  });
  root.querySelectorAll(".view-report-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const row = root.querySelector(`.report-row[data-report-for="${btn.dataset.run}"]`);
      const cell = row.querySelector("td");
      if (row.style.display !== "none") { row.style.display = "none"; return; }
      const report = await api(`/api/admin/ingestion/runs/${btn.dataset.run}/report`);
      cell.innerHTML = `<table class="admin-table"><thead><tr><th>File</th><th>Event</th><th>Detail</th></tr></thead><tbody>
        ${report.files.map((f) => `<tr><td>${esc(f.original_filename)}</td><td><span class="status-badge ${f.event}">${esc(f.event)}</span></td><td>${esc(f.detail || "")}</td></tr>`).join("")}
      </tbody></table>`;
      row.style.display = "table-row";
    });
  });

  const queryForm = document.getElementById("query-form");
  if (queryForm) queryForm.addEventListener("submit", async (e) => {
    e.preventDefault();
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

boot();
