"""Pure scoring logic for eval_retrieval.py's ground-truth cases -- no
database, no network, no module-level side effects, so it can be
imported and unit-tested without tripping eval_retrieval.py's own
production-safety guard, which runs as a side effect at import time.

Five separate check dimensions, deliberately not conflated:

- retrieval: was the expected document even fetched as a candidate at
  all, cited or not (from message_sources, not citations).
- citation_selected: of what WAS retrieved, did the model choose to cite
  the right one. A retrieval hit with no citation hit is a genuine
  model/citation-selection failure, not a retrieval gap -- a single
  combined check cannot tell the two failure modes apart.
- cited_support: do the CITED EXCERPTS (verbatim chunk content, not the
  model's free-form answer) contain the expected facts.
- answer_correctness (optional, via expected_answer_contains /
  answer_must_not_contain): does the ANSWER TEXT itself state the right
  values. A case can have the right citation, with the right keywords
  present in that excerpt, while the answer still pairs the wrong value
  with a model or skips a prerequisite -- cited_support alone cannot
  catch that, since it never looks at the answer text.
- procedure_safety (optional, via expected_step_order / required_warnings):
  does the answer present steps in the right order and include every
  required warning.

A case that doesn't define a given optional dimension's fields is NOT
CHECKED for it, not silently passed -- the caller distinguishes
"0 cases checked" from "all checked cases passed" when aggregating.
"""
from __future__ import annotations

_CHUNK_TYPE_GROUPS = {
    "table_or_error_code": {"table", "error_code"},
}


def score_case(case: dict, body: dict, retrieved_filenames: list[str], chunk_types: dict[int, str]) -> dict:
    """`body` is the raw POST .../messages JSON response. `retrieved_filenames`
    is every retrieved passage's filename (cited or not). `chunk_types` maps
    a cited chunk_id to its chunk_type."""
    result = {"id": case["id"], "category": case["category"], "question": case["question"], "ok": True, "notes": []}

    if case.get("expect_clarifying_question"):
        result["ok"] = bool(body.get("is_clarifying_question"))
        if not result["ok"]:
            result["notes"].append("expected a clarifying question, did not get one")
        return result

    if case.get("expect_no_answer"):
        # answer_status=='completed' is required alongside is_no_answer: a
        # real provider outage or persistence failure (answer_status ==
        # 'failed') must never read as "the system correctly abstained" just
        # because it also produced no citations.
        result["ok"] = bool(body.get("is_no_answer")) and body.get("answer_status") == "completed"
        if not result["ok"]:
            if body.get("answer_status") != "completed":
                result["notes"].append(
                    f"expected an honest no-answer, but answer_status was {body.get('answer_status')!r} "
                    "(a system failure, not an honest abstention)"
                )
            else:
                result["notes"].append("expected an honest no-answer, got a confident-looking answer instead")
        return result

    if body.get("answer_status") != "completed":
        result["ok"] = False
        result["notes"].append(
            f"answer_status was {body.get('answer_status')!r}, not 'completed' -- a system "
            "failure, not a content mismatch, must never be scored as a retrieval/citation miss"
        )
        return result

    citations = body.get("citations", [])
    cited_filenames = [c["filename"] for c in citations]
    answer_text = body.get("answer", "")
    answer_lower = answer_text.lower()
    result["cited_filenames"] = cited_filenames

    expected_sub = case.get("expected_filename_contains")
    if expected_sub:
        retrieval_hit = any(expected_sub.lower() in f.lower() for f in retrieved_filenames)
        result["retrieval_hit"] = retrieval_hit
        if not retrieval_hit:
            result["ok"] = False
            result["notes"].append(
                f"expected document never retrieved at all (checked message_sources, not just "
                f"citations): '{expected_sub}' not among {retrieved_filenames}"
            )

        citation_selected = any(expected_sub.lower() in f.lower() for f in cited_filenames)
        result["citation_selected"] = citation_selected
        if not citation_selected:
            result["ok"] = False
            if retrieval_hit:
                result["notes"].append(
                    f"expected document WAS retrieved but the model did not cite it -- a citation-"
                    f"selection failure, not a retrieval failure: got {cited_filenames}"
                )
            else:
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
        actual = {chunk_types.get(c["chunk_id"]) for c in citations}
        result["chunk_type_match"] = bool(actual & allowed_types)
        if not result["chunk_type_match"]:
            result["ok"] = False
            result["notes"].append(
                f"expected a citation of chunk_type in {sorted(allowed_types)}, "
                f"cited types were {sorted(t for t in actual if t)}"
            )

    keywords = case.get("expected_keywords", [])
    if keywords:
        # Checked against cited excerpts, not the model's free-form answer
        # text -- excerpts are verbatim chunk content returned by the API,
        # not model-generated text, so requiring the keyword there is a real
        # claim-to-evidence check, not just a claim-to-output one. A keyword
        # appearing only in generated prose, never actually quoted from a
        # cited chunk, must not pass.
        excerpt_haystack = " ".join(c.get("excerpt", "") for c in citations).lower()
        found = [k for k in keywords if k.lower() in excerpt_haystack]
        result["cited_support"] = len(found) == len(keywords)
        if len(found) != len(keywords):
            result["ok"] = False
            missing = [k for k in keywords if k not in found]
            result["notes"].append(
                f"expected keywords not found in any CITED EXCERPT (checked excerpts only, not "
                f"answer text): {missing}"
            )

    # Answer correctness: does the ANSWER TEXT itself (not just a cited
    # excerpt somewhere) state the right value, in the right pairing. A
    # case with the right citation and the right keyword in that excerpt
    # can still fail here if the answer actually states a DIFFERENT
    # model's value, or omits the value entirely -- cited_support above
    # cannot catch that, since it never reads the answer text.
    expected_answer = case.get("expected_answer_contains", [])
    forbidden_answer = case.get("answer_must_not_contain", [])
    if expected_answer or forbidden_answer:
        missing = [v for v in expected_answer if v.lower() not in answer_lower]
        leaked = [v for v in forbidden_answer if v.lower() in answer_lower]
        result["answer_correctness"] = not missing and not leaked
        if missing:
            result["ok"] = False
            result["notes"].append(f"expected value(s) not found in the ANSWER TEXT itself: {missing}")
        if leaked:
            result["ok"] = False
            result["notes"].append(f"answer text contains a value it must not (likely a sibling model's): {leaked}")

    # Procedure safety: are the numbered steps in the right order, and is
    # every required warning present. Checked against the answer's own
    # numbered STEPS section specifically, not the whole answer text --
    # a claim restating part of a step can legitimately appear ABOVE
    # "**Steps:**" (see parse_and_validate's answer assembly), and
    # searching the whole text would find a step's own wording there
    # first, at the wrong position relative to an actual earlier step.
    expected_order = case.get("expected_step_order", [])
    required_warnings = case.get("required_warnings", [])
    if expected_order or required_warnings:
        ok = True
        notes: list[str] = []
        if expected_order:
            steps_marker = answer_lower.find("**steps:**")
            if steps_marker == -1:
                ok = False
                notes.append("expected numbered steps, but the answer has no '**Steps:**' section at all")
            else:
                steps_text = answer_lower[steps_marker:]
                cursor = 0
                bad: list[str] = []
                for step in expected_order:
                    idx = steps_text.find(step.lower(), cursor)
                    if idx == -1:
                        bad.append(step)
                        continue
                    cursor = idx + len(step)
                if bad:
                    ok = False
                    notes.append(
                        f"expected step(s) not found, in order, within the numbered STEPS "
                        f"section (missing entirely, or out of order relative to an earlier "
                        f"expected step): {bad}"
                    )
        if required_warnings:
            warnings_text = " ".join(body.get("safety_warnings", [])).lower()
            missing_warnings = [w for w in required_warnings if w.lower() not in warnings_text]
            if missing_warnings:
                ok = False
                notes.append(f"required warning(s) not present in safety_warnings: {missing_warnings}")
        result["procedure_safety"] = ok
        if not ok:
            result["ok"] = False
            result["notes"].extend(notes)

    return result
