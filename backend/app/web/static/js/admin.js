"use strict";

const state = { tab: "documents", user: null, documents: [], duplicates: [], runs: [], feedback: [], unanswered: [], queryResult: null, machines: [], allMachines: [] };
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
  { id: "documents", label: "Manuals & metadata" },
  { id: "duplicates", label: "Duplicates" },
  { id: "ingestion", label: "Ingestion reports" },
  { id: "query", label: "Query tester" },
  { id: "feedback", label: "Feedback & gaps" },
];

async function loadTab() {
  if (state.tab === "documents") {
    state.documents = await api("/api/admin/documents");
    if (state.allMachines.length === 0) state.allMachines = await api("/api/admin/machines");
  }
  if (state.tab === "duplicates") state.duplicates = await api("/api/admin/duplicates");
  if (state.tab === "ingestion") state.runs = await api("/api/admin/ingestion/runs");
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
  if (state.tab === "documents") return renderDocuments();
  if (state.tab === "duplicates") return renderDuplicates();
  if (state.tab === "ingestion") return renderIngestion();
  if (state.tab === "query") return renderQuery();
  if (state.tab === "feedback") return renderFeedback();
  return "";
}

// --- Documents / metadata correction ---

function renderDocuments() {
  return `
    <h1>Manuals &amp; metadata</h1>
    <p style="color:var(--text-dim)">${state.documents.length} active document(s). Correct auto-detected metadata below — every edit is logged for audit.</p>
    <table class="admin-table">
      <thead><tr><th>File</th><th>Status</th><th>Manufacturer</th><th>Doc type</th><th>Title</th><th>Revision</th><th>Machines</th><th></th></tr></thead>
      <tbody>
        ${state.documents.map((d) => `
          <tr>
            <td>${esc(d.original_filename)}<br><span style="color:var(--text-dim);font-size:0.8rem;">${d.file_type} · ${d.page_count ?? "?"} pages${d.is_current_revision ? "" : " · SUPERSEDED"}</span></td>
            <td><span class="status-badge ${d.status}">${d.status}</span>${d.status_reason ? `<div style="font-size:0.78rem;color:var(--text-dim);max-width:220px;">${esc(d.status_reason)}</div>` : ""}</td>
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
            <td colspan="8">
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
