"""Ground-truth retrieval/citation evaluation.

Runs each case in data/eval/ground_truth.json through the REAL chat API
(FastAPI TestClient, real retrieval, real embeddings, the configured
AI_PROVIDER -- nothing mocked) so the eval fidelity matches production.

P0-11 (external review, 2026-09-21) rewrote this after the Postgres
migration broke it outright (it opened a SQLite backup of a `db_path_resolved`
setting that no longer exists) and after the review found the original
design unsafe even once patched: it could touch the live, configured
PostgreSQL database directly.

This version REQUIRES a disposable, point-in-time Postgres clone -- never
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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values  # noqa: E402

# Overridable only for this script's own regression test (subprocess-invoked
# against a fake "production" file, never touching the real backend/.env) --
# every real invocation uses the default.
BACKEND_DIR = Path(__file__).resolve().parent.parent
PROD_ENV_FILE = Path(os.environ.get("TMA_EVAL_PROD_ENV_FILE_FOR_TESTS") or (BACKEND_DIR / ".env"))

# Below this overall pass rate, or if EITHER retrieval hit rate or
# citation-support rate falls below its own threshold, the process exits
# nonzero -- a report full of FAILs that still exits 0 is worse than no
# report at all (P0-11: "failed cases still leave CLI exit code zero").
MIN_OVERALL_PASS_RATE = 0.80
MIN_RETRIEVAL_HIT_RATE = 0.80
MIN_CITATION_SUPPORT_RATE = 0.80

# expected_chunk_type values in ground_truth.json are composite categories,
# not literal chunks.chunk_type values (text | table | procedure |
# error_code | warning | spec) -- mapped explicitly here rather than left to
# silently never match.
_CHUNK_TYPE_GROUPS = {
    "table_or_error_code": {"table", "error_code"},
}


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
                if value == prod_vars.get(prod_key):
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

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

from app.auth.security import hash_password  # noqa: E402
from app.db import get_conn, run_migrations  # noqa: E402
from app.main import app  # noqa: E402

run_migrations()

GROUND_TRUTH_PATH = BACKEND_DIR.parent / "data" / "eval" / "ground_truth.json"
REPORT_PATH = BACKEND_DIR.parent / "data" / "reports" / "retrieval_eval_report.md"

EVAL_EMAIL = "eval-runner@qa-eval-account.com"

client = TestClient(app)


def get_or_create_eval_user() -> None:
    """P0-5 (independent follow-up review) closed public self-registration --
    the original version of this function's `client.post("/api/auth/register",
    ...)` call would 404/422 against every real deployment since then. This
    clone already has production's real users on it (it's a branch OF
    production), so app.auth.bootstrap.bootstrap_admin (which refuses
    outright when any user exists) isn't usable here either. Inserted
    directly with the same shape /api/auth/register's own INSERT uses --
    this is the account state a real invite-registration would produce,
    without needing a production admin's real (unknown to this script)
    password to issue that invitation through the API first."""
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


def run_case(case: dict) -> dict:
    machine_id = resolve_machine_id(case.get("machine"))
    conv = client.post("/api/conversations", json={"machine_id": machine_id}).json()
    resp = client.post(
        f"/api/conversations/{conv['id']}/messages", json={"content": case["question"]}
    )
    body = resp.json()

    result = {"id": case["id"], "category": case["category"], "question": case["question"], "ok": True, "notes": []}

    if case.get("expect_clarifying_question"):
        result["ok"] = bool(body.get("is_clarifying_question"))
        if not result["ok"]:
            result["notes"].append("expected a clarifying question, did not get one")
        return result

    if case.get("expect_no_answer"):
        result["ok"] = bool(body.get("is_no_answer"))
        if not result["ok"]:
            result["notes"].append("expected an honest no-answer, got a confident-looking answer instead")
        return result

    citations = body.get("citations", [])
    cited_filenames = [c["filename"] for c in citations]
    result["cited_filenames"] = cited_filenames

    expected_sub = case.get("expected_filename_contains")
    if expected_sub:
        hit = any(expected_sub.lower() in f.lower() for f in cited_filenames)
        result["retrieval_hit"] = hit
        if not hit:
            result["ok"] = False
            result["notes"].append(f"expected a citation containing '{expected_sub}', got {cited_filenames}")

    for forbidden in case.get("must_not_come_from", []):
        leaked = [f for f in cited_filenames if forbidden.lower() in f.lower()]
        if leaked:
            result["ok"] = False
            result["notes"].append(f"cross-model leak: citation from '{forbidden}' should not appear here: {leaked}")
    result["cross_model_clean"] = not case.get("must_not_come_from") or not any(
        forbidden.lower() in f.lower() for forbidden in case.get("must_not_come_from", []) for f in cited_filenames
    )

    expected_type = case.get("expected_chunk_type")
    if expected_type:
        allowed_types = _CHUNK_TYPE_GROUPS.get(expected_type, {expected_type})
        chunk_types = _chunk_types_for([c["chunk_id"] for c in citations])
        actual = {chunk_types.get(c["chunk_id"]) for c in citations}
        result["chunk_type_match"] = bool(actual & allowed_types)
        if not result["chunk_type_match"]:
            result["ok"] = False
            result["notes"].append(f"expected a citation of chunk_type in {sorted(allowed_types)}, cited types were {sorted(t for t in actual if t)}")

    keywords = case.get("expected_keywords", [])
    if keywords:
        # P0-11: the old check searched the model's own free-form answer
        # text as well as cited excerpts -- a keyword appearing ONLY in
        # generated prose (never actually quoted from a cited chunk) passed,
        # which is exactly "an uncited invented answer word can pass" from
        # the review. Excerpts are verbatim chunk content returned by the
        # API, not model-generated text, so requiring the keyword there is a
        # real claim-to-evidence check, not just a claim-to-output one.
        excerpt_haystack = " ".join(c.get("excerpt", "") for c in citations).lower()
        found = [k for k in keywords if k.lower() in excerpt_haystack]
        result["citation_support"] = len(found) == len(keywords)
        if len(found) != len(keywords):
            result["ok"] = False
            missing = [k for k in keywords if k not in found]
            result["notes"].append(f"expected keywords not found in any CITED EXCERPT (checked excerpts only, not answer text): {missing}")

    return result


def run_source_withdrawal_check() -> dict:
    """P0-11 (\"include source withdrawal in the ground truth\") and P0-13
    (this same review, fixed earlier in this batch): rather than inventing a
    new ground-truth case with a hand-picked expected answer (the file's own
    header insists nothing in it is invented), this deactivates the document
    an ALREADY-PASSING case depends on -- directly on the disposable clone,
    never production -- and re-runs that exact case, asserting its citation
    disappears. The expectation here is mechanically derived from the first
    run, not authored."""
    case = next((c for c in json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))["cases"]
                 if c["id"] == "troubleshoot-axiom-heating"), None)
    if case is None:
        return {"id": "source-withdrawal-check", "category": "source_withdrawal", "ok": False,
                "notes": ["troubleshoot-axiom-heating case not found -- cannot run this check"]}

    before = run_case(case)
    if not before.get("retrieval_hit"):
        return {"id": "source-withdrawal-check", "category": "source_withdrawal", "ok": False,
                "notes": [f"base case did not retrieve as expected before withdrawal: {before.get('notes')}"]}

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
    still_cited = any(case["expected_filename_contains"].lower() in f.lower() for f in after.get("cited_filenames", []))
    ok = not still_cited
    notes = [] if ok else [f"withdrawn document's content is STILL being cited: {after.get('cited_filenames')}"]
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
    citation_cases = [r for r in results if "citation_support" in r]
    citation_hits = sum(1 for r in citation_cases if r["citation_support"])
    citation_rate = citation_hits / len(citation_cases) if citation_cases else 1.0
    cross_model_cases = [r for r in results if "cross_model_clean" in r]
    cross_model_clean = sum(1 for r in cross_model_cases if r["cross_model_clean"])

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
        lines.append(f"- Retrieval hit rate (expected document cited): **{retrieval_hits}/{len(retrieval_cases)}** ({100*retrieval_rate:.0f}%), threshold {100*MIN_RETRIEVAL_HIT_RATE:.0f}%")
    if citation_cases:
        lines.append(f"- Citation-support rate (expected facts present in a CITED EXCERPT, not just the answer text): **{citation_hits}/{len(citation_cases)}** ({100*citation_rate:.0f}%), threshold {100*MIN_CITATION_SUPPORT_RATE:.0f}%")
    if cross_model_cases:
        lines.append(f"- Cross-model isolation (no leak from a forbidden document): **{cross_model_clean}/{len(cross_model_cases)}**")
    lines.append("")
    lines.append(
        f"**Calibration disclosure:** `MIN_VECTOR_SIMILARITY_FOR_ANSWER` in "
        f"`app/providers/extractive.py` was tuned against this same case set (2 "
        f"absent-answer cases, 2 relevant cases used for the threshold gap) -- this pass rate "
        f"is not independent validation of that threshold, only confirmation the tuned value "
        f"still passes the cases it was tuned on. Treat it as a regression check, not "
        f"generalization evidence, until it is re-measured against held-out, technician-written "
        f"questions (not yet added to data/eval/ground_truth.json as of this rewrite -- see "
        f"the P0-11 commit message)."
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
        or citation_rate < MIN_CITATION_SUPPORT_RATE
    )
    if below_threshold:
        print(
            f"\nBelow acceptance threshold (overall {100*pass_rate:.0f}%/{100*MIN_OVERALL_PASS_RATE:.0f}%, "
            f"retrieval {100*retrieval_rate:.0f}%/{100*MIN_RETRIEVAL_HIT_RATE:.0f}%, "
            f"citation {100*citation_rate:.0f}%/{100*MIN_CITATION_SUPPORT_RATE:.0f}%) -- exiting nonzero."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
