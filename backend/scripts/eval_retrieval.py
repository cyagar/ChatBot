"""Ground-truth retrieval/citation evaluation.

Runs each case in data/eval/ground_truth.json through the REAL chat API
(FastAPI TestClient, real retrieval, real embeddings, the configured
AI_PROVIDER -- nothing mocked) so the eval fidelity matches production.

This script REQUIRES a disposable, point-in-time Postgres clone -- never
production, never the shared Neon "test" branch tests/ uses (that branch is
schema-only, with no real corpus to evaluate retrieval against). Create one
with the Neon CLI before running this script:

    neon branches create --parent production --name eval-$(date +%Y%m%d)
    neon connection-string eval-<date> --pooled    # -> EVAL_DATABASE_URL
    neon connection-string eval-<date>              # -> EVAL_DATABASE_URL_UNPOOLED
    neon branches delete eval-<date>                # after this script finishes

EVAL_DATABASE_URL and EVAL_DATABASE_URL_UNPOOLED must both be set in the
environment -- this script refuses to run if either is missing, or if either
is identical to backend/.env's real production value (a hard guard, checked
before any migration or query runs; see _refuse_unless_disposable_clone
below). It never reads DATABASE_URL/DATABASE_URL_UNPOOLED itself, so an
ambient production .env can't leak in by accident.

Usage (from backend/):
    EVAL_DATABASE_URL=... EVAL_DATABASE_URL_UNPOOLED=... py scripts/eval_retrieval.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402

# Overridable only for this script's own regression test (subprocess-invoked
# against a fake "production" file, never touching the real backend/.env) --
# every real invocation uses the default.
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROD_ENV_FILE = Path(os.environ.get("TMA_EVAL_PROD_ENV_FILE_FOR_TESTS") or (BACKEND_DIR / ".env"))

# Below the overall pass rate, or if retrieval / citation-selection /
# cited-support rate falls below its own threshold, the process exits
# nonzero -- a report full of FAILs that still exits 0 is worse than no
# report at all. retrieval and citation_selection are deliberately
# separate: a passage can be genuinely retrieved (present in
# message_sources) without the model choosing to cite it, and a single
# combined check cannot tell those two failure modes apart.
# Answer-correctness and procedure-safety failures are a separate,
# zero-tolerance gate below (like cross-model leakage), not part of
# these aggregate thresholds -- see main().
MIN_OVERALL_PASS_RATE = 0.80
MIN_RETRIEVAL_HIT_RATE = 0.80
MIN_CITATION_SELECTION_RATE = 0.80
MIN_CITATION_SUPPORT_RATE = 0.80


def _normalize_dsn(url: str) -> tuple:
    """Host/port/path compared case-insensitively on the host (DNS names
    aren't case-sensitive) and with no trailing slash -- a raw string
    == would miss two DSNs that are the SAME production database typed with
    a different host case or a trailing slash, and wrongly let this script
    run against it."""
    parsed = urlparse(url)
    return (parsed.scheme, (parsed.hostname or "").lower(), parsed.port, parsed.path.rstrip("/"))


def _refuse_unless_disposable_clone() -> tuple[str, str]:
    """Returns (database_url, database_url_unpooled) for the eval clone, or
    raises. Two failure modes, both loud: the vars are simply unset (the
    original bug this replaces silently fell back to whatever DATABASE_URL
    happened to be ambient), or they're set but identical to backend/.env's
    real production value (a copy-pasted .env, a misconfigured shell)."""
    url = os.environ.get("EVAL_DATABASE_URL")
    url_unpooled = os.environ.get("EVAL_DATABASE_URL_UNPOOLED")
    if not url or not url_unpooled:
        raise RuntimeError(
            "EVAL_DATABASE_URL and EVAL_DATABASE_URL_UNPOOLED must both be set, pointing at a "
            "disposable Neon branch created from production for this run -- see this script's "
            "module docstring for the exact commands. This script refuses to guess or fall back "
            "to any other configured database."
        )
    if PROD_ENV_FILE.is_file():
        prod_vars = dotenv_values(PROD_ENV_FILE)
        for name, value in (("EVAL_DATABASE_URL", url), ("EVAL_DATABASE_URL_UNPOOLED", url_unpooled)):
            for prod_key in ("DATABASE_URL", "DATABASE_URL_UNPOOLED"):
                prod_value = prod_vars.get(prod_key)
                if prod_value and _normalize_dsn(value) == _normalize_dsn(prod_value):
                    raise RuntimeError(
                        f"Refusing to run: {name} is IDENTICAL to backend/.env's production "
                        f"{prod_key}. This script registers an eval user and writes eval "
                        f"conversations -- pointing it at production would write eval traffic "
                        f"into the real database technicians and admins use."
                    )
    return url, url_unpooled


_eval_db_url, _eval_db_url_unpooled = _refuse_unless_disposable_clone()
os.environ["DATABASE_URL"] = _eval_db_url
os.environ["DATABASE_URL_UNPOOLED"] = _eval_db_url_unpooled
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(32))
# The disposable clone's backend/.env (if any) is a clone of production's,
# so APP_ENV there reads 'production' -- which makes the login cookie
# Secure (app/auth/routes.py). TestClient talks to http://testserver, not
# https, so a Secure cookie is silently never replayed on the next request:
# every call after login would look logged-out, with nothing here checking
# for it. Forced unconditionally (not setdefault), since the whole point is
# to override whatever the cloned .env says.
os.environ["APP_ENV"] = "development"

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

from app.auth.security import hash_password  # noqa: E402
from app.db import get_conn, run_migrations  # noqa: E402
from app.main import app  # noqa: E402
from scripts.eval_scoring import score_case  # noqa: E402

run_migrations()

GROUND_TRUTH_PATH = BACKEND_DIR.parent / "data" / "eval" / "ground_truth.json"
REPORT_PATH = BACKEND_DIR.parent / "data" / "reports" / "retrieval_eval_report.md"

EVAL_EMAIL = "eval-runner@qa-eval-account.com"

client = TestClient(app)


def get_or_create_eval_user() -> None:
    """Public self-registration is closed and this clone already has
    production's real users on it (it's a branch OF production), so
    app.auth.bootstrap.bootstrap_admin (which refuses outright when any user
    exists) isn't usable here either. Inserted directly with the same shape
    /api/auth/register's own INSERT uses -- this is the account state a real
    invite-registration would produce, without needing a production admin's
    real (unknown to this script) password to issue that invitation through
    the API first."""
    password = secrets.token_urlsafe(18)
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM users WHERE email = %s", (EVAL_EMAIL,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO users (email, password_hash, role, display_name) VALUES (%s, %s, %s, %s)",
                (EVAL_EMAIL, hash_password(password), "technician", "Eval Runner"),
            )
        else:
            conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(password), existing["id"]))

    resp = client.post("/api/auth/login", json={"email": EVAL_EMAIL, "password": password})
    assert resp.status_code == 200, f"eval user login failed: {resp.status_code} {resp.text}"


def resolve_machine_id(model_name: str | None) -> int | None:
    if model_name is None:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM machines WHERE model_name = %s", (model_name,)).fetchone()
    if row is None:
        raise ValueError(f"No machine found with model_name={model_name!r} -- check the ground truth file.")
    return row["id"]


def _chunk_types_for(chunk_ids: list[int]) -> dict[int, str]:
    if not chunk_ids:
        return {}
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, chunk_type FROM chunks WHERE id = ANY(%s)", (chunk_ids,)
        ).fetchall()
    return {r["id"]: r["chunk_type"] for r in rows}


def _retrieved_filenames_for(message_id: int) -> list[str]:
    """Every passage retrieval actually fetched for this message, cited or
    not (message_sources records all of them) -- distinct from the
    citations the model chose to put in its answer. See
    eval_scoring.score_case's "retrieval" vs "citation_selected" split."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT d.original_filename FROM message_sources ms "
            "JOIN chunks c ON c.id = ms.chunk_id JOIN documents d ON d.id = c.document_id "
            "WHERE ms.message_id = %s",
            (message_id,),
        ).fetchall()
    return [r["original_filename"] for r in rows]


def run_case(case: dict) -> dict:
    """The network/DB layer: POST the question, fetch what retrieval
    actually surfaced for it, then hand off to score_case (app/-free, pure,
    independently unit-tested) for every pass/fail judgment."""
    machine_id = resolve_machine_id(case.get("machine"))
    conv_resp = client.post("/api/conversations", json={"machine_id": machine_id})
    if conv_resp.status_code != 201:
        raise RuntimeError(f"POST /api/conversations failed: {conv_resp.status_code} {conv_resp.text}")
    conv = conv_resp.json()
    resp = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": case["question"]}
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"POST .../messages failed for case {case['id']!r}: {resp.status_code} {resp.text} -- "
            "a gateway/provider error must never be silently read as any kind of answer outcome."
        )
    body = resp.json()

    retrieved_filenames: list[str] = []
    chunk_types: dict[int, str] = {}
    message_id = body.get("id")
    if message_id is not None:
        retrieved_filenames = _retrieved_filenames_for(message_id)
        chunk_types = _chunk_types_for([c["chunk_id"] for c in body.get("citations", [])])

    return score_case(case, body, retrieved_filenames, chunk_types)


def run_source_withdrawal_check() -> dict:
    """Rather than inventing a new ground-truth case with a hand-picked
    expected answer (the file's own header insists nothing in it is
    invented), this deactivates the document an ALREADY-PASSING case depends
    on -- directly on the disposable clone, never production -- and re-runs
    that exact case, asserting its citation disappears. The expectation here
    is mechanically derived from the first run, not authored."""
    case = next((c for c in json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))["cases"]
                 if c["id"] == "troubleshoot-axiom-heating"), None)
    if case is None:
        return {"id": "source-withdrawal-check", "category": "source_withdrawal", "ok": False,
                "notes": ["troubleshoot-axiom-heating case not found -- cannot run this check"]}

    before = run_case(case)
    # Must have been CITED (not just retrieved) before withdrawal, or the
    # "no longer cited after withdrawal" check below is meaningless.
    if not before.get("citation_selected"):
        return {"id": "source-withdrawal-check", "category": "source_withdrawal", "ok": False,
                "notes": [f"base case did not cite the expected document before withdrawal: {before.get('notes')}"]}

    with get_conn() as conn:
        doc_ids = [
            row["id"] for row in conn.execute(
                "SELECT DISTINCT d.id FROM documents d JOIN chunks c ON c.document_id = d.id "
                "JOIN message_sources ms ON ms.chunk_id = c.id "
                "WHERE d.original_filename ILIKE %s ORDER BY d.id",
                (f"%{case['expected_filename_contains']}%",),
            ).fetchall()
        ]
        if not doc_ids:
            return {"id": "source-withdrawal-check", "category": "source_withdrawal", "ok": False,
                     "notes": ["could not resolve the cited document's id to deactivate"]}
        conn.execute("UPDATE documents SET deactivated_at = now() WHERE id = ANY(%s)", (doc_ids,))

    after = run_case(case)
    # An empty cited_filenames list is ambiguous on its own -- it's what a
    # genuine "no longer cited" response looks like, but it's ALSO exactly
    # what run_case's own answer_status!='completed' short-circuit above
    # returns (no 'cited_filenames' key at all) on a provider/system error.
    # The latter must never pass this check: an outage is not proof the
    # withdrawn source was actually excluded from retrieval.
    if "cited_filenames" not in after:
        return {"id": "source-withdrawal-check", "category": "source_withdrawal", "ok": False,
                 "notes": [f"re-run after withdrawal did not complete normally: {after.get('notes')}"]}
    still_cited = any(case["expected_filename_contains"].lower() in f.lower() for f in after["cited_filenames"])
    ok = not still_cited
    notes = [] if ok else [f"withdrawn document's content is STILL being cited: {after['cited_filenames']}"]
    return {"id": "source-withdrawal-check", "category": "source_withdrawal", "ok": ok, "notes": notes}


def main() -> int:
    with open(GROUND_TRUTH_PATH, encoding="utf-8") as f:
        ground_truth = json.load(f)

    get_or_create_eval_user()

    results = []
    start = time.time()
    for case in ground_truth["cases"]:
        try:
            results.append(run_case(case))
        except Exception as e:
            results.append({"id": case["id"], "category": case["category"], "ok": False, "notes": [f"EXCEPTION: {e}"]})

    try:
        results.append(run_source_withdrawal_check())
    except Exception as e:
        results.append({"id": "source-withdrawal-check", "category": "source_withdrawal", "ok": False, "notes": [f"EXCEPTION: {e}"]})

    elapsed = time.time() - start

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    pass_rate = passed / total if total else 0.0
    retrieval_cases = [r for r in results if "retrieval_hit" in r]
    retrieval_hits = sum(1 for r in retrieval_cases if r["retrieval_hit"])
    retrieval_rate = retrieval_hits / len(retrieval_cases) if retrieval_cases else 1.0
    citation_selection_cases = [r for r in results if "citation_selected" in r]
    citation_selection_hits = sum(1 for r in citation_selection_cases if r["citation_selected"])
    citation_selection_rate = (
        citation_selection_hits / len(citation_selection_cases) if citation_selection_cases else 1.0
    )
    citation_cases = [r for r in results if "cited_support" in r]
    citation_hits = sum(1 for r in citation_cases if r["cited_support"])
    citation_rate = citation_hits / len(citation_cases) if citation_cases else 1.0
    cross_model_cases = [r for r in results if "cross_model_clean" in r]
    cross_model_clean = sum(1 for r in cross_model_cases if r["cross_model_clean"])
    cross_model_leaks = len(cross_model_cases) - cross_model_clean
    # Zero-tolerance, like cross-model leakage: a case that DEFINES an
    # answer-correctness or procedure-safety check and fails it is a real
    # wrong-value-paired-with-a-model or out-of-order-steps problem,
    # regardless of how well everything else scored. No authored case
    # currently defines either (see data/eval/ground_truth.json's schema
    # note) -- these print "0 cases checked", not a passing rate.
    answer_correctness_cases = [r for r in results if "answer_correctness" in r]
    answer_correctness_failures = sum(1 for r in answer_correctness_cases if not r["answer_correctness"])
    procedure_safety_cases = [r for r in results if "procedure_safety" in r]
    procedure_safety_failures = sum(1 for r in procedure_safety_cases if not r["procedure_safety"])

    lines = []
    lines.append("# Retrieval & Citation Evaluation Report")
    lines.append("")
    lines.append(
        f"Run against a disposable, point-in-time Neon clone of production (real corpus, real "
        f"embeddings, nothing mocked) and the `AI_PROVIDER` configured in `.env`, {elapsed:.1f}s, {total} cases."
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Overall pass rate: **{passed}/{total}** ({100*pass_rate:.0f}%), threshold {100*MIN_OVERALL_PASS_RATE:.0f}%")
    if retrieval_cases:
        lines.append(f"- Retrieval rate (expected document actually fetched, cited or not): **{retrieval_hits}/{len(retrieval_cases)}** ({100*retrieval_rate:.0f}%), threshold {100*MIN_RETRIEVAL_HIT_RATE:.0f}%")
    if citation_selection_cases:
        lines.append(f"- Citation-selection rate (model chose to cite the expected document): **{citation_selection_hits}/{len(citation_selection_cases)}** ({100*citation_selection_rate:.0f}%), threshold {100*MIN_CITATION_SELECTION_RATE:.0f}%")
    if citation_cases:
        lines.append(f"- Cited-support rate (expected facts present in a CITED EXCERPT, not just the answer text): **{citation_hits}/{len(citation_cases)}** ({100*citation_rate:.0f}%), threshold {100*MIN_CITATION_SUPPORT_RATE:.0f}%")
    if cross_model_cases:
        lines.append(f"- Cross-model isolation (no leak from a forbidden document): **{cross_model_clean}/{len(cross_model_cases)}**")
    lines.append(
        f"- Answer correctness (value stated correctly in the answer TEXT, zero-tolerance): "
        f"**{len(answer_correctness_cases) - answer_correctness_failures}/{len(answer_correctness_cases)}** cases checked"
        if answer_correctness_cases else
        "- Answer correctness: 0 cases checked (no authored case defines expected_answer_contains/answer_must_not_contain yet)"
    )
    lines.append(
        f"- Procedure safety (step order / required warnings, zero-tolerance): "
        f"**{len(procedure_safety_cases) - procedure_safety_failures}/{len(procedure_safety_cases)}** cases checked"
        if procedure_safety_cases else
        "- Procedure safety: 0 cases checked (no authored case defines expected_step_order/required_warnings yet)"
    )
    lines.append("")
    lines.append(
        "**Calibration disclosure:** `MIN_VECTOR_SIMILARITY_FOR_ANSWER` in "
        "`app/providers/extractive.py` was tuned against this same case set (2 "
        "absent-answer cases, 2 relevant cases used for the threshold gap) -- this pass rate "
        "is not independent validation of that threshold, only confirmation the tuned value "
        "still passes the cases it was tuned on. Treat it as a regression check, not "
        "generalization evidence, until it is re-measured against held-out, technician-written "
        "questions (not yet added to data/eval/ground_truth.json)."
    )
    lines.append("")
    lines.append("## Per-case results")
    lines.append("")
    lines.append("| ID | Category | Result | Notes |")
    lines.append("|---|---|---|---|")
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        notes = "; ".join(r.get("notes", [])) or "—"
        lines.append(f"| {r['id']} | {r['category']} | {status} | {notes} |")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n{passed}/{total} cases passed. Report written to {REPORT_PATH}")
    for r in results:
        if not r["ok"]:
            print(f"FAIL {r['id']}: {r.get('notes')}")

    below_threshold = (
        pass_rate < MIN_OVERALL_PASS_RATE
        or retrieval_rate < MIN_RETRIEVAL_HIT_RATE
        or citation_selection_rate < MIN_CITATION_SELECTION_RATE
        or citation_rate < MIN_CITATION_SUPPORT_RATE
    )
    if below_threshold:
        print(
            f"\nBelow acceptance threshold (overall {100*pass_rate:.0f}%/{100*MIN_OVERALL_PASS_RATE:.0f}%, "
            f"retrieval {100*retrieval_rate:.0f}%/{100*MIN_RETRIEVAL_HIT_RATE:.0f}%, "
            f"citation selection {100*citation_selection_rate:.0f}%/{100*MIN_CITATION_SELECTION_RATE:.0f}%, "
            f"cited support {100*citation_rate:.0f}%/{100*MIN_CITATION_SUPPORT_RATE:.0f}%) -- exiting nonzero."
        )
        return 1
    # Cross-model leakage, answer-correctness failures, and procedure-safety
    # failures are all zero-tolerance gates, not part of the 80% aggregate
    # thresholds above -- a technician shown one citation from the wrong
    # machine's manual, a wrong value paired with a model, or steps out of
    # order is a real problem regardless of how well every OTHER case
    # scored, so even a single one fails the whole run.
    if cross_model_leaks > 0:
        print(f"\n{cross_model_leaks} cross-model leak(s) detected -- exiting nonzero regardless of pass rate.")
        return 1
    if answer_correctness_failures > 0:
        print(f"\n{answer_correctness_failures} answer-correctness failure(s) detected -- exiting nonzero.")
        return 1
    if procedure_safety_failures > 0:
        print(f"\n{procedure_safety_failures} procedure-safety failure(s) detected -- exiting nonzero.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
