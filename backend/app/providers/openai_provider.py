"""OpenAI-backed provider. Not active until OPENAI_API_KEY is set and the
`openai` package is installed (uncomment it in requirements.txt then
`pip install -r requirements.txt`)."""

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

MODEL = "gpt-5.1"
REQUEST_TIMEOUT_SECONDS = 30
# Independent follow-up review 2026-08-24 P1-7 ("token budgets"): unlike
# AnthropicProvider, this request set no cap at all -- an unbounded response
# is both a cost risk and, for this app's use case (a few claims/steps plus
# citations), never actually needed. Matches AnthropicProvider's own cap.
MAX_OUTPUT_TOKENS = 1200

_JSON_SHAPE_INSTRUCTION = (
    "Respond with ONLY this JSON shape: "
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


class OpenAIProvider(AIProvider):
    name = "openai"

    def __init__(self):
        settings = get_settings()
        if not settings.openai_api_key:
            raise RuntimeError(
                "AI_PROVIDER=openai but OPENAI_API_KEY is not set in .env. "
                "Either add the key or set AI_PROVIDER=local_extractive."
            )
        try:
            import openai
        except ImportError as e:
            raise RuntimeError(
                "AI_PROVIDER=openai but the 'openai' package is not installed. "
                "Uncomment it in requirements.txt and reinstall."
            ) from e
        self._client = openai.OpenAI(api_key=settings.openai_api_key, timeout=REQUEST_TIMEOUT_SECONDS)

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
            f"{machine_line}Technician question: {question}\n\nManual excerpts:\n\n{context}\n\n"
            f"{_JSON_SHAPE_INSTRUCTION}"
        )
        messages = (
            [{"role": "system", "content": SYSTEM_PROMPT}]
            + build_history_messages(history)
            + [{"role": "user", "content": user_message}]
        )

        raw_text = self._call(messages)
        result = parse_and_validate(raw_text, passages, self.name, machine_label)
        if result is not None:
            return result

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

        # See the matching comment in anthropic_provider.py -- neither
        # attempt survived parse_and_validate, so log both raw responses
        # server-side to give an administrator something to investigate
        # instead of a dead end.
        logger.warning(
            "Both attempts failed validation for conversation; provider=%s\n--- attempt 1 ---\n%s\n--- attempt 2 ---\n%s",
            self.name, raw_text, raw_text_2,
        )
        return GeneratedAnswer(answer=UNVERIFIED_ANSWER, is_no_answer=True, provider=self.name)

    def _call(self, messages: list[dict]) -> str:
        try:
            import openai

            response = self._client.chat.completions.create(
                model=MODEL,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=MAX_OUTPUT_TOKENS,
            )
        except openai.APITimeoutError as e:
            raise ProviderError("The AI provider timed out.") from e
        except openai.RateLimitError as e:
            raise ProviderError("The AI provider is rate-limited; try again shortly.") from e
        except openai.APIStatusError as e:
            raise ProviderError(f"The AI provider returned an error (status {e.status_code}).") from e
        except openai.APIError as e:
            raise ProviderError("The AI provider request failed.") from e
        return response.choices[0].message.content or ""
