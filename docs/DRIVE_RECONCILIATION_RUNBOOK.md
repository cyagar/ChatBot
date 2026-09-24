# Drive Reconciliation & Emergency Withdrawal/Rollback Runbook

Who this is for: an administrator who needs to withdraw a manual from
service, or undo a withdrawal, and wants to know exactly what the system
will and won't do for them.

## The core safety rule

**A missing Drive file is never auto-deactivated.** If a document's source
file disappears from the latest Drive listing (deleted, renamed, moved out
of the shared folder, or just a transient listing failure), the next
ingestion run reports it — as a `missing_from_source` event, visible in
that run's ingestion report (`GET /api/admin/ingestion/runs/{run_id}/report`)
and counted in the run's outcome totals — but the document stays active and
searchable exactly as before. Only a human decision (below) removes it from
service.

This is deliberate. There is no reliable way for the pipeline to tell "this
file was actually deleted" apart from "Drive had a bad moment" or "someone
briefly unshared the folder while reorganizing." Guessing wrong in the
auto-delete direction takes a working manual away from a technician with no
warning; guessing wrong the other way just means a stale report entry until
someone looks at it. The system is built to fail toward the second.

## Reading the missing-from-source report

After any ingestion run, check its report for `missing_from_source` entries.
Each one names the document and its `source_ref`. Before acting:

1. **Confirm it's real.** Check the shared Drive folder directly — is the
   file actually gone, renamed, or moved? A single run showing this could
   be a transient Drive API issue; if the *next* scheduled/manual run still
   reports the same document missing, treat it as real.
2. **Decide the cause**, because the correct action differs:
   - **Deleted on purpose** (obsolete manual) → withdraw it (below).
   - **Renamed or moved within the shared folder** → it will re-ingest
     under a new `source_ref` next run, landing as a new `pending` document.
     The old row is still reported as missing until someone withdraws it
     once the rename is confirmed intentional.
   - **Accidentally removed / moved out of the shared folder** → put the
     file back in the shared folder; the next run picks it up again under
     its original `source_ref` and the missing report entry stops
     recurring. No other action needed — nothing was deactivated.

## Emergency withdrawal (taking a manual out of service now)

Use this when a manual must stop being served immediately — a safety
correction, a legal hold, a confirmed-wrong document, or a confirmed real
deletion from the source of truth.

1. Admin UI → **Manuals & metadata** tab → find the document → **Deactivate**.
   Give a real reason; it's stored on the document (`status_reason`) and in
   the audit log (`document_deactivated` event, actor + timestamp).
2. Effective immediately: the document drops out of retrieval and the
   default admin listing. Its chunks, citations already given to
   technicians in past conversations, and audit history are **not**
   deleted — only new retrieval is blocked. (Reviewing a past answer that
   cited this document still works; the API marks that source as
   withdrawn, per the existing `source_withdrawn` flag technicians and
   admins already see.)
3. If this was in response to something ingestion just reported (see
   above), you're done — the report entry reflects a decision that's now
   been acted on.

Via API (equivalent, for scripting):
`POST /api/admin/documents/{document_id}/deactivate?reason=...`

## Rollback (undoing a withdrawal)

Use this when a deactivation turns out to be wrong — the wrong document was
picked, new information reverses the decision, or the underlying Drive file
came back.

1. Admin UI → **Manuals & metadata** tab → check **Show deactivated** → find
   the document → **Reactivate**.
2. This only clears the deactivation. It does **not** re-run review — the
   document keeps whatever `review_status` and `is_current_revision` it had.
   If another document has since become the active manual for the same
   machine/source, reactivating this one does not automatically resolve
   that overlap. **Check the Manuals & metadata listing afterward** for two
   active documents that both claim to be current for the same machine; if
   so, decide which should carry `is_current_revision` (Edit → that field)
   or deactivate the one that shouldn't be active, same as any other
   metadata correction.
3. Confirm via the audit log (`document_reactivated` event) that this
   matches what was intended.

Via API: `POST /api/admin/documents/{document_id}/reactivate?reason=...`

## Audit trail

Every deactivate/reactivate is an `audit_events` row: actor, timestamp,
document id, and the reason text supplied. This is the durable record of
who withdrew or restored a manual and why — check it before assuming a
document's current state was always that way.

## What this runbook does not cover

- **Bulk/scripted withdrawal** (e.g. an entire manufacturer's catalog
  pulled at once) — not built. Today's tools operate one document at a
  time; a real bulk-withdrawal need should be a deliberate follow-up, not
  a script run manually against the API in a hurry.
- **Automatic conflict detection** between an old (reactivated) and a new
  document both claiming to be current for the same machine — not built.
  Step 2 above is the manual check that stands in for it.
- **Legal/compliance holds** requiring content to be provably unrecoverable
  (not just deactivated) — deactivation is reversible by design and does
  not delete chunks/embeddings. A genuine legal-hold requirement needs a
  separate, explicit deletion path this system does not currently have.
