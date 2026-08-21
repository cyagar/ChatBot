"""Anthropic-backed provider. Not active until ANTHROPIC_API_KEY is set and the
`anthropic` package is installed (uncomment it in requirements.txt then
`pip install -r requirements.txt`). Kept separate from extractive.py so switching
AI_PROVIDER is a one-line .env change, never a code change.
"""

from __future__ import annotations

from app.config import get_settings
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
REQUEST_TIMEOUT_SECONDS = 30

_JSON_SHAPE_INSTRUCTION = (
    "Respond in this exact JSON shape (no markdown fence): "
    '{"is_no_answer": false, "no_answer_explanation": null, '
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
    "or wording, not just the general topic. Do not include a conflict_note field -- "
    "revision conflicts are detected separately from excerpt metadata."
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
                "Uncomment it in requirements.txt and reinstall."
            ) from e
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=REQUEST_TIMEOUT_SECONDS)

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
        result = parse_and_validate(raw_text, passages, self.name)
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
        result = parse_and_validate(raw_text_2, passages, self.name)
        if result is not None:
            return result

        return GeneratedAnswer(answer=UNVERIFIED_ANSWER, is_no_answer=True, provider=self.name)

    def _call(self, messages: list[dict]) -> str:
        try:
            import anthropic

            response = self._client.messages.create(
                model=MODEL,
                max_tokens=1200,
                system=SYSTEM_PROMPT,
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
