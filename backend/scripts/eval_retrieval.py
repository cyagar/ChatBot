"""Ground-truth retrieval/citation evaluation.

Runs each case in data/eval/ground_truth.json through the REAL chat API
(FastAPI TestClient, real retrieval, real embeddings, real corpus content, the
configured AI_PROVIDER — nothing mocked) so the eval fidelity matches
production. That real corpus is read from a point-in-time snapshot of the
production DB, not the live file: this script registers an eval account and
writes eval conversations, and those must never land in the DB technicians
and admins actually use. (An earlier version of this script wrote directly to
the production DB and its eval-runner account became the real user id=1 /
first-registered-user-becomes-admin — see docs/PRODUCTION_READINESS.md.)

Usage (from backend/): py scripts/eval_retrieval.py
"""

import atexit
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402

_real_db_path = get_settings().db_path_resolved
_snapshot_dir = Path(tempfile.mkdtemp(prefix="tma_eval_"))
_snapshot_db_path = _snapshot_dir / "eval_snapshot.db"

# sqlite3's backup API (not a plain file copy) takes a consistent snapshot
# even while the source is in WAL mode with a live writer.
_src_conn = sqlite3.connect(str(_real_db_path))
_dst_conn = sqlite3.connect(str(_snapshot_db_path))
with _dst_conn:
    _src_conn.backup(_dst_conn)
_src_conn.close()
_dst_conn.close()
atexit.register(shutil.rmtree, _snapshot_dir, ignore_errors=True)

os.environ["DB_PATH"] = str(_snapshot_db_path)
get_settings.cache_clear()

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_conn  # noqa: E402
from app.main import app  # noqa: E402

GROUND_TRUTH_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "eval" / "ground_truth.json"
REPORT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "reports" / "retrieval_eval_report.md"

client = TestClient(app)


def get_or_create_eval_user():
    email = "eval-runner@qa-eval-account.com"
    password = secrets.token_urlsafe(18)  # snapshot DB is discarded on exit; no need for a stable credential
    resp = client.post("/api/auth/register", json={"email": email, "password": password})
    assert resp.status_code in (200, 201), resp.text


def resolve_machine_id(model_name: str | None) -> int | None:
    if model_name is None:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT id FROM machines WHERE model_name = ?", (model_name,)).fetchone()
    if row is None:
        raise ValueError(f"No machine found with model_name={model_name!r} — check the ground truth file.")
    return row["id"]


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

    keywords = case.get("expected_keywords", [])
    if keywords:
        haystack = (body.get("content", "") + " " + " ".join(c.get("excerpt", "") for c in citations)).lower()
        found = [k for k in keywords if k.lower() in haystack]
        result["citation_support"] = len(found) == len(keywords)
        if len(found) != len(keywords):
            result["ok"] = False
            missing = [k for k in keywords if k not in found]
            result["notes"].append(f"expected keywords not found in answer/citations: {missing}")

    return result


def main():
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
    elapsed = time.time() - start

    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    retrieval_cases = [r for r in results if "retrieval_hit" in r]
    retrieval_hits = sum(1 for r in retrieval_cases if r["retrieval_hit"])
    citation_cases = [r for r in results if "citation_support" in r]
    citation_hits = sum(1 for r in citation_cases if r["citation_support"])
    cross_model_cases = [r for r in results if "cross_model_clean" in r]
    cross_model_clean = sum(1 for r in cross_model_cases if r["cross_model_clean"])

    lines = []
    lines.append("# Retrieval & Citation Evaluation Report")
    lines.append("")
    lines.append(
        f"Run against a point-in-time snapshot of the production database (real corpus, real "
        f"embeddings, nothing mocked) and the `AI_PROVIDER` configured in `.env`, {elapsed:.1f}s, {total} cases."
    )
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Overall pass rate: **{passed}/{total}** ({100*passed/total:.0f}%)")
    if retrieval_cases:
        lines.append(f"- Retrieval hit rate (expected document cited): **{retrieval_hits}/{len(retrieval_cases)}**")
    if citation_cases:
        lines.append(f"- Citation-support rate (expected facts present in cited text): **{citation_hits}/{len(citation_cases)}**")
    if cross_model_cases:
        lines.append(f"- Cross-model isolation (no leak from a forbidden document): **{cross_model_clean}/{len(cross_model_cases)}**")
    lines.append("")
    lines.append(
        f"**Calibration disclosure:** `MIN_VECTOR_SIMILARITY_FOR_ANSWER` in "
        f"`app/providers/extractive.py` was tuned against this same {total}-case set (2 "
        f"absent-answer cases, 2 relevant cases used for the threshold gap) — this pass rate "
        f"is not independent validation of that threshold, only confirmation the tuned value "
        f"still passes the cases it was tuned on. Treat it as a regression check, not "
        f"generalization evidence, until it is re-measured against held-out questions."
    )
    lines.append("")
    lines.append(
        "Citation-support here means the expected keyword phrase appears in the cited excerpt or generated "
        "answer text — a direct, mechanical check that the citation actually backs the claim, not a "
        "self-reported confidence score."
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


if __name__ == "__main__":
    main()
