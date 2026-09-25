"""Anthropic-backed provider. Not active until ANTHROPIC_API_KEY is set and
AI_PROVIDER=anthropic (see app/providers/factory.py) -- the `anthropic`
package itself is already an unconditional requirements.txt dependency, no
separate install step needed. Kept separate from extractive.py so switching
AI_PROVIDER is a one-line .env change, never a code change.
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
    failing_items,
    parse_and_validate,
)

# The SDK's own max_retries is layered UNDER generate()'s application-level
# "repair" retry (a second full _call() when the first response fails
# parse_and_validate) -- not a substitute for it, since a transport-level
# exception from _call() propagates straight out of generate() with no
# repair attempt. Worst-case wall time is 2 calls x (1 + MAX_RETRIES)
# attempts x REQUEST_TIMEOUT_SECONDS, which must stay comfortably under
# Android's own read timeout (ApiClient.kt) or the technician sees
# "connection lost" while the server is still working. Kept at 1 retry, not
# 0, so a single transient blip still self-heals instead of failing the
# whole request on the first hiccup. These numbers are a reasoned bound, not
# measured against live traffic -- revisit under real latency data before
# assuming they're generous rather than tight.
REQUEST_TIMEOUT_SECONDS = 20
MAX_RETRIES = 1

_JSON_SHAPE_INSTRUCTION = (
    "Respond in this exact JSON shape (no markdown fence): "
    '{"is_no_answer": false, "no_answer_explanation": null, "confidence": "high", '
    '"claims": [{"text": "...", "cited_excerpt_numbers": [1]}], '
    '"steps": [{"text": "...", "cited_excerpt_numbers": [1]}], '
    '"warnings": [{"text": "...", "cited_excerpt_numbers": [3]}]}. '
    "If the excerpts don't support an answer, set is_no_answer to true, set "
    "no_answer_explanation to a short reason (it is not shown to the technician), and leave "
    "claims/steps/warnings empty. "
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
        self._model = settings.anthropic_model
        self._log_rejected_output = settings.log_rejected_model_output
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
        # output instead of silently trusting a malformed/unsupported response.
        # Never widen citations on a parse failure.
        failed = failing_items(raw_text, passages, machine_label)
        detail = (
            " These items were not found in the excerpt numbers you cited for them: "
            + " | ".join(f'"{text[:160]}"' for text in failed[:6]) + "."
            if failed else ""
        )
        repair_messages = messages + [
            {"role": "assistant", "content": raw_text},
            {
                "role": "user",
                "content": (
                    "That response did not match the required JSON shape, cited an excerpt "
                    "number that doesn't exist, or included a claim/step/warning whose number, "
                    "identifier, or wording is not actually present verbatim in the excerpt(s) "
                    "it cited. Reply again with ONLY valid JSON in the exact shape requested, "
                    "copying each claim, step and warning from its cited excerpt's own wording "
                    "(dropping words only, keeping every negation, in the excerpt's order), or "
                    "set is_no_answer to true if the excerpts don't support an answer." + detail
                ),
            },
        ]
        raw_text_2 = self._call(repair_messages)
        result = parse_and_validate(raw_text_2, passages, self.name, machine_label)
        if result is not None:
            return result

        if self._log_rejected_output:
            logger.warning(
                "Both attempts failed validation; provider=%s\n--- attempt 1 ---\n%s\n--- attempt 2 ---\n%s",
                self.name, raw_text, raw_text_2,
            )
        else:
            logger.warning(
                "Both attempts failed validation; provider=%s response_chars=%d,%d "
                "(set LOG_REJECTED_MODEL_OUTPUT=true to log the responses)",
                self.name, len(raw_text), len(raw_text_2),
            )
        return GeneratedAnswer(answer=UNVERIFIED_ANSWER, is_no_answer=True, provider=self.name)

    def _call(self, messages: list[dict]) -> str:
        try:
            import anthropic

            response = self._client.messages.create(
                model=self._model,
                max_tokens=1200,
                system=SYSTEM_PROMPT,
                # Without this, the API can enable extended thinking on its own
                # even though no `thinking` param was requested. Thinking tokens
                # draw from the same max_tokens budget as the answer, so a large
                # thinking block can leave stop_reason="max_tokens" with a
                # truncated (invalid) or missing JSON text block, failing
                # parse_and_validate on an otherwise fully-answerable question.
                # This task is grounded extraction against excerpts already
                # handed to the model, not open reasoning, so thinking isn't
                # needed here; disabling it guarantees the whole budget goes to
                # the actual JSON answer.
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
