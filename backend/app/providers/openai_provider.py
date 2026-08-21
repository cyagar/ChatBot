"""OpenAI-backed provider. Not active until OPENAI_API_KEY is set and the
`openai` package is installed (uncomment it in requirements.txt then
`pip install -r requirements.txt`)."""

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

MODEL = "gpt-5.1"
REQUEST_TIMEOUT_SECONDS = 30

_JSON_SHAPE_INSTRUCTION = (
    "Respond with ONLY this JSON shape: "
    '{"answer": "...", "is_no_answer": false, "conflict_note": null, '
    '"safety_warnings": ["..."], "cited_excerpt_numbers": [1,2]}. '
    'cited_excerpt_numbers must reference only the excerpt numbers shown above, '
    "and must be non-empty unless is_no_answer is true."
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
        result = parse_and_validate(raw_text, passages, self.name)
        if result is not None:
            return result

        repair_messages = messages + [
            {"role": "assistant", "content": raw_text},
            {
                "role": "user",
                "content": (
                    "That response did not match the required JSON shape, or claimed an "
                    "answer without citing any of the numbered excerpts. Reply again with "
                    "ONLY valid JSON in the exact shape requested, citing real excerpt "
                    "numbers, or set is_no_answer to true if the excerpts don't support an answer."
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
            import openai

            response = self._client.chat.completions.create(
                model=MODEL,
                messages=messages,
                response_format={"type": "json_object"},
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
