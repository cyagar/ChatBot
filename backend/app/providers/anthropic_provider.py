"""Anthropic-backed provider. Not active until ANTHROPIC_API_KEY is set and
AI_PROVIDER=anthropic (see app/providers/factory.py) -- the `anthropic`
package itself is already an unconditional requirements.txt dependency, no
separate install step needed. Kept separate from extractive.py so switching
AI_PROVIDER is a one-line .env change, never a code change.

P2-04 (external review, 2026-09-21): this docstring used to tell readers to
"uncomment [anthropic] in requirements.txt" -- stale advice from before it
became an always-installed dependency; corrected rather than left to mislead
the next person setting this up.
"""

from __future__ import annotations

import logging

from app.config import get_settings

logger = logging.getLogger(__name__)
from app.providers.base import (
    AIProvider,
    GeneratedAnswer,
    ProviderError,
    SYSTEM_PROMPT,
    UNVERIFIED_ANSWER,
    build_context_block,
    build_history_messages,
    parse_and_validate,
)

MODEL = "claude-sonnet-5"
# P1-19 (external review, 2026-09-21): the SDK's own default max_retries is
# 2, layered UNDER generate()'s own application-level "repair" retry (a
# second full _call() when the first response fails parse_and_validate --
# not a substitute for this, since a transport-level exception from _call()
# propagates straight out of generate() with no repair attempt). Worst case
# was 2 calls x 3 attempts (1 original + 2 SDK retries) x 30s = up to 180s
# against Android's 90-second read timeout (ApiClient.kt) -- the technician
# could see "connection lost" while the server was still working, or the
# app's own timeout could fire mid-provider-call. Tightened so the true
# worst case fits with margin: 2 calls x 2 attempts (1 original + 1 SDK
# retry) x 20s = 80s. Kept at 1 retry, not 0 -- a single transient
# blip (the common case a retry actually helps with) still self-heals
# instead of failing the whole request on the first hiccup. These exact
# numbers are a reasoned bound, not measured against live traffic (the
# review flagged this item as "conditional, verify with actual
# model/network" -- no live Anthropic call was exercised here either);
# revisit under real latency data before assuming 20s is generous rather
# than tight.
REQUEST_TIMEOUT_SECONDS = 20
MAX_RETRIES = 1

_JSON_SHAPE_INSTRUCTION = (
    "Respond in this exact JSON shape (no markdown fence): "
    '{"is_no_answer": false, "no_answer_explanation": null, "confidence": "high", '
    '"claims": [{"text": "...", "cited_excerpt_numbers": [1]}], '
    '"steps": [{"text": "...", "cited_excerpt_numbers": [1]}], '
    '"warnings": [{"text": "...", "cited_excerpt_numbers": [3]}]}. '
    "If the excerpts don't support an answer, set is_no_answer to true and put your "
    "explanation in no_answer_explanation; leave claims/steps/warnings empty. "
    "Otherwise leave no_answer_explanation null and put every material fact in its own "
    "claims entry, every repair/check action in its own steps entry, and every warning "
    "(quoted verbatim from its excerpt) in its own warnings entry -- each entry's "
    "cited_excerpt_numbers must be a non-empty list referencing only the excerpt numbers "
    "shown above, and must be the excerpt(s) that actually contain that entry's number(s) "
    "or wording, not just the general topic. Set confidence to \"low\" when the excerpts "
    "only partially or indirectly address the question even though what you found is still "
    "verified (see the system prompt); set it to \"high\" when the excerpts directly and "
    "completely answer it. Do not include a conflict_note field -- revision conflicts are "
    "detected separately from excerpt metadata."
)


class AnthropicProvider(AIProvider):
    name = "anthropic"

    def __init__(self):
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "AI_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set in .env. "
                "Either add the key or set AI_PROVIDER=local_extractive."
            )
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError(
                "AI_PROVIDER=anthropic but the 'anthropic' package is not installed. "
                "It's an unconditional requirements.txt dependency -- run "
                "'pip install -r requirements.txt' to install it."
            ) from e
        self._client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES,
        )

    def generate(self, question, machine_label, passages, history=None) -> GeneratedAnswer:
        if not passages:
            return GeneratedAnswer(
                answer="No relevant manual passages were found for this question.",
                is_no_answer=True,
                provider=self.name,
            )

        context = build_context_block(passages)
        machine_line = f"Selected machine: {machine_label}\n" if machine_label else ""
        user_message = (
            f"{machine_line}Technician question: {question}\n\n"
            f"Manual excerpts:\n\n{context}\n\n{_JSON_SHAPE_INSTRUCTION}"
        )
        messages = build_history_messages(history) + [{"role": "user", "content": user_message}]

        raw_text = self._call(messages)
        result = parse_and_validate(raw_text, passages, self.name, machine_label)
        if result is not None:
            return result

        # One repair attempt: tell the model exactly what was wrong with its own
        # output instead of silently trusting a malformed/unsupported response
        # (concern #8 -- never widen citations on a parse failure).
        repair_messages = messages + [
            {"role": "assistant", "content": raw_text},
            {
                "role": "user",
                "content": (
                    "That response did not match the required JSON shape, cited an excerpt "
                    "number that doesn't exist, or included a claim/step/warning whose number, "
                    "identifier, or wording is not actually present verbatim in the excerpt(s) "
                    "it cited. Reply again with ONLY valid JSON in the exact shape requested, "
                    "double-checking that every number and every warning you write is copied "
                    "exactly from its cited excerpt, or set is_no_answer to true if the "
                    "excerpts don't support an answer."
                ),
            },
        ]
        raw_text_2 = self._call(repair_messages)
        result = parse_and_validate(raw_text_2, passages, self.name, machine_label)
        if result is not None:
            return result

        # Neither attempt survived parse_and_validate -- log both raw
        # responses server-side (never shown to the technician) so an
        # administrator investigating a run of UNVERIFIED_ANSWER replies (the
        # message's own advice) has something to look at instead of a dead
        # end. Added 2026-08-25 after exactly that: a technician hit this
        # fallback for several honestly-unanswerable questions in a row, and
        # diagnosing it required temporarily adding this logging and
        # reproducing live -- it turned out to be a validator false positive
        # (see parse_and_validate's docstring), not a provider problem, but
        # nothing before this let anyone tell the difference without
        # instrumenting the code by hand.
        logger.warning(
            "Both attempts failed validation for conversation; provider=%s\n--- attempt 1 ---\n%s\n--- attempt 2 ---\n%s",
            self.name, raw_text, raw_text_2,
        )
        return GeneratedAnswer(answer=UNVERIFIED_ANSWER, is_no_answer=True, provider=self.name)

    def _call(self, messages: list[dict]) -> str:
        try:
            import anthropic

            response = self._client.messages.create(
                model=MODEL,
                max_tokens=1200,
                system=SYSTEM_PROMPT,
                # Found live 2026-09-16: without this, the API enables
                # extended thinking on its own for claude-sonnet-5 -- no
                # `thinking` param was ever requested here. For a genuinely
                # answerable question ("How do I replace the burrs on this
                # grinder?") thinking consumed 1006 of the 1200-token budget,
                # so stop_reason came back "max_tokens" with either a
                # truncated (invalid) JSON text block or, worse, zero text
                # block at all -- both attempts (the original call and the
                # repair retry) failed parse_and_validate identically and
                # produced the generic UNVERIFIED_ANSWER for a question the
                # excerpts fully supported. This task is grounded extraction
                # against excerpts already handed to the model, not open
                # reasoning, so thinking isn't needed here; disabling it
                # guarantees the whole budget goes to the actual JSON answer.
                thinking={"type": "disabled"},
                messages=messages,
            )
        except anthropic.APITimeoutError as e:
            raise ProviderError("The AI provider timed out.") from e
        except anthropic.RateLimitError as e:
            raise ProviderError("The AI provider is rate-limited; try again shortly.") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"The AI provider returned an error (status {e.status_code}).") from e
        except anthropic.APIError as e:
            raise ProviderError("The AI provider request failed.") from e
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
