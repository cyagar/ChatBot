"""OpenAI-backed provider. Not active until OPENAI_API_KEY is set and the
`openai` package is installed (uncomment it in requirements.txt then
`pip install -r requirements.txt`)."""

from __future__ import annotations

import json
import re

from app.config import get_settings
from app.providers.base import AIProvider, Citation, GeneratedAnswer, SYSTEM_PROMPT, build_context_block

MODEL = "gpt-5.1"


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
        self._client = openai.OpenAI(api_key=settings.openai_api_key)

    def generate(self, question, machine_label, passages) -> GeneratedAnswer:
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
            "Respond with ONLY this JSON shape:\n"
            '{"answer": "...", "is_no_answer": false, "conflict_note": null, '
            '"safety_warnings": ["..."], "cited_excerpt_numbers": [1,2]}'
        )

        response = self._client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
        )
        raw_text = response.choices[0].message.content or ""
        return self._parse(raw_text, passages)

    def _parse(self, raw_text: str, passages) -> GeneratedAnswer:
        try:
            match = re.search(r"\{.*\}", raw_text, re.DOTALL)
            data = json.loads(match.group(0)) if match else {}
        except Exception:
            data = {}

        cited_numbers = data.get("cited_excerpt_numbers") or list(range(1, len(passages) + 1))
        citations = []
        for n in cited_numbers:
            if 1 <= n <= len(passages):
                p = passages[n - 1]
                citations.append(
                    Citation(
                        chunk_id=p.chunk_id, document_id=p.document_id, filename=p.original_filename,
                        title=p.title, page_number=p.page_number, section_heading=p.section_heading,
                        revision=p.revision, excerpt=p.content[:500],
                    )
                )

        return GeneratedAnswer(
            answer=data.get("answer") or raw_text or "The model returned an unparseable response.",
            citations=citations,
            is_no_answer=bool(data.get("is_no_answer", False)),
            conflict_note=data.get("conflict_note"),
            safety_warnings=data.get("safety_warnings") or [],
            provider=self.name,
        )
