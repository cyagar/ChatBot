"""P1-7 (independent follow-up review, 2026-08-24): "Provider/model
configuration and failure behavior require production contracts. Add mocked
contract tests for every provider, timeouts, rate limits, malformed
responses, retries, token budgets, model retirement, safety fallback, and
request cancellation."

Every network call here is mocked -- these test the CONTRACT between this
codebase and the anthropic/openai SDKs (which exceptions map to which
ProviderError, how many attempts happen, what shape a response must have),
not live model behavior. AI_PROVIDER stays local_extractive for the rest of
the test suite (see docs/PRODUCTION_READINESS.md); these tests construct
AnthropicProvider/OpenAIProvider directly, bypassing the AI_PROVIDER-gated
factory entirely.

Also covers "no manual content may be sent to an unapproved provider":
get_provider() only ever returns one of exactly three hardcoded classes
(app/providers/factory.py), and Settings.validate_for_startup() refuses to
start on any AI_PROVIDER value outside that fixed set -- there is no
runtime path to a dynamically-configured or unapproved provider.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.providers.base import ProviderError
from app.retrieval.search import RetrievedChunk


def _passage(chunk_id=1, document_id=1, content="Replace part 81-118-31 during service."):
    return RetrievedChunk(
        chunk_id=chunk_id, document_id=document_id, content=content,
        page_number=1, section_heading=None, chunk_type="text",
        original_filename="manual.pdf", title="Manual", doc_type="service_repair",
        revision=None, manufacturer="Bunn-O-Matic Corporation", is_current_revision=True,
        lexical_score=1.0, vector_score=1.0, combined_score=1.0,
    )


def _valid_json_response(text="Replace part 81-118-31."):
    return json.dumps({
        "is_no_answer": False,
        "claims": [{"text": text, "cited_excerpt_numbers": [1]}],
        "steps": [], "warnings": [],
    })


def _req():
    return httpx.Request("POST", "https://example.invalid/v1/chat")


def _resp(status_code):
    return httpx.Response(status_code, request=_req())


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

@pytest.fixture
def anthropic_provider(test_env, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.providers.anthropic_provider import AnthropicProvider
    provider = AnthropicProvider()
    yield provider
    get_settings.cache_clear()


class _AnthropicBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _AnthropicResponse:
    def __init__(self, text):
        self.content = [_AnthropicBlock(text)]


def test_anthropic_timeout_becomes_provider_error(anthropic_provider, monkeypatch):
    import anthropic

    def raise_timeout(**kwargs):
        raise anthropic.APITimeoutError(request=_req())

    monkeypatch.setattr(anthropic_provider._client.messages, "create", raise_timeout)
    with pytest.raises(ProviderError, match="timed out"):
        anthropic_provider.generate("Why?", "Axiom", [_passage()])


def test_anthropic_rate_limit_becomes_provider_error(anthropic_provider, monkeypatch):
    import anthropic

    def raise_rl(**kwargs):
        raise anthropic.RateLimitError("rate limited", response=_resp(429), body=None)

    monkeypatch.setattr(anthropic_provider._client.messages, "create", raise_rl)
    with pytest.raises(ProviderError, match="rate-limited"):
        anthropic_provider.generate("Why?", "Axiom", [_passage()])


def test_anthropic_retired_model_status_error_becomes_provider_error(anthropic_provider, monkeypatch):
    """A retired/invalid model id surfaces from the SDK as an APIStatusError
    (e.g. 404 model_not_found) -- must degrade to ProviderError, not an
    unhandled exception that crashes the request."""
    import anthropic

    def raise_not_found(**kwargs):
        raise anthropic.NotFoundError("model not found", response=_resp(404), body=None)

    monkeypatch.setattr(anthropic_provider._client.messages, "create", raise_not_found)
    with pytest.raises(ProviderError):
        anthropic_provider.generate("Why?", "Axiom", [_passage()])


def test_anthropic_malformed_response_retries_once_then_falls_back(anthropic_provider, monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs["messages"])
        return _AnthropicResponse("not json at all")

    monkeypatch.setattr(anthropic_provider._client.messages, "create", create)
    result = anthropic_provider.generate("Why?", "Axiom", [_passage()])
    assert len(calls) == 2, "exactly one repair retry, not more, not zero"
    assert result.is_no_answer is True
    assert "could not produce a verified" in result.answer.lower()


def test_anthropic_recovers_on_the_repair_retry(anthropic_provider, monkeypatch):
    responses = [_AnthropicResponse("garbage, not JSON"), _AnthropicResponse(_valid_json_response())]

    def create(**kwargs):
        return responses.pop(0)

    monkeypatch.setattr(anthropic_provider._client.messages, "create", create)
    result = anthropic_provider.generate("Why?", "Axiom", [_passage()])
    assert result.is_no_answer is False
    assert "81-118-31" in result.answer


def test_anthropic_no_answer_explanation_mentioning_the_machine_name_is_not_rejected(
    anthropic_provider, monkeypatch
):
    """Found live 2026-08-25: a technician on the "Ultra-1/Ultra-2" got the
    generic UNVERIFIED_ANSWER fallback for several honestly-unanswerable
    questions in a row. The model's real no_answer_explanation was fine each
    time -- it just naturally referenced the machine by name, and that name
    has two digits ("1", "2") scattered in it, which _material_tokens
    flagged as an unverifiable claim even though it's prompt-given context,
    not something the model could be fabricating."""
    explanation = (
        "The provided excerpts do not contain an Electrical Setup procedure "
        "for the Ultra-1/Ultra-2. Please consult the Installation section."
    )
    response = json.dumps({
        "is_no_answer": True, "no_answer_explanation": explanation,
        "claims": [], "steps": [], "warnings": [],
    })
    monkeypatch.setattr(
        anthropic_provider._client.messages, "create", lambda **k: _AnthropicResponse(response)
    )
    result = anthropic_provider.generate("How to do electrical setup", "Ultra-1/Ultra-2", [_passage()])
    assert result.is_no_answer is True
    assert result.answer == explanation, "the model's real explanation, not the generic fallback"


def test_anthropic_no_answer_explanation_with_an_unrelated_material_token_still_falls_back(
    anthropic_provider, monkeypatch
):
    """Regression guard for P0-5 (independent review): the machine-name
    exemption above must not become a blanket exemption for every material
    token. A part number/voltage/error code that has nothing to do with the
    machine's own name is still exactly the fabrication risk that check
    exists for."""
    explanation = "Bypass the interlock at 600V to test the control board."
    response = json.dumps({
        "is_no_answer": True, "no_answer_explanation": explanation,
        "claims": [], "steps": [], "warnings": [],
    })
    monkeypatch.setattr(
        anthropic_provider._client.messages, "create", lambda **k: _AnthropicResponse(response)
    )
    result = anthropic_provider.generate("Why?", "Ultra-1/Ultra-2", [_passage()])
    assert result.is_no_answer is True
    assert result.answer != explanation
    assert "could not produce a verified" in result.answer.lower()


def test_anthropic_claim_mentioning_the_machine_name_is_not_rejected(anthropic_provider, monkeypatch):
    """Same bug, second location: found live 2026-08-25 immediately after
    the no_answer_explanation case above -- a *claim* (not just a no-answer
    explanation) naturally referencing the machine by name hit the same
    false-positive rejection, since claims go through _claim_supported, a
    separate function with its own material-token check. A technician's
    "what can I ask you" got the generic fallback because one of six claims
    said "...for the Ultra-1/Ultra-2" -- the other five were all fine, but
    validation is all-or-nothing per response."""
    passage = _passage(content="Replace hopper drum seal every 12 months.")
    response = json.dumps({
        "is_no_answer": False, "no_answer_explanation": None,
        "claims": [{
            "text": "The excerpts cover the 12 month maintenance schedule for the Ultra-1/Ultra-2.",
            "cited_excerpt_numbers": [1],
        }],
        "steps": [], "warnings": [],
    })
    monkeypatch.setattr(
        anthropic_provider._client.messages, "create", lambda **k: _AnthropicResponse(response)
    )
    result = anthropic_provider.generate("What can I ask you?", "Ultra-1/Ultra-2", [passage])
    assert result.is_no_answer is False
    assert "12 month maintenance" in result.answer.lower()


def test_anthropic_claim_with_an_unrelated_material_token_still_falls_back(anthropic_provider, monkeypatch):
    """Regression guard for P0-7 (independent review): the machine-name
    exemption in _claim_supported must not become a blanket exemption for
    every material token in a claim either -- a fabricated part number
    unrelated to the machine's own name is still exactly the risk that
    check exists for."""
    passage = _passage(content="Replace hopper drum seal every 12 months.")
    response = json.dumps({
        "is_no_answer": False, "no_answer_explanation": None,
        "claims": [{
            "text": "Replace part 99-999-99 on the Ultra-1/Ultra-2 every 12 months.",
            "cited_excerpt_numbers": [1],
        }],
        "steps": [], "warnings": [],
    })
    monkeypatch.setattr(
        anthropic_provider._client.messages, "create", lambda **k: _AnthropicResponse(response)
    )
    result = anthropic_provider.generate("What can I ask you?", "Ultra-1/Ultra-2", [passage])
    assert result.is_no_answer is True
    assert "could not produce a verified" in result.answer.lower()


def test_anthropic_empty_content_safety_refusal_shape_degrades_gracefully(anthropic_provider, monkeypatch):
    """A safety-filtered/refused response with no text blocks at all must be
    treated the same as any other malformed response -- fall back cleanly,
    never crash the request."""
    class _EmptyResponse:
        content = []

    monkeypatch.setattr(anthropic_provider._client.messages, "create", lambda **k: _EmptyResponse())
    result = anthropic_provider.generate("Why?", "Axiom", [_passage()])
    assert result.is_no_answer is True


def test_anthropic_request_sets_a_bounded_max_tokens(anthropic_provider, monkeypatch):
    captured = {}

    def create(**kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
        return _AnthropicResponse(_valid_json_response())

    monkeypatch.setattr(anthropic_provider._client.messages, "create", create)
    anthropic_provider.generate("Why?", "Axiom", [_passage()])
    assert 0 < captured["max_tokens"] <= 4096


def test_anthropic_no_passages_short_circuits_without_calling_the_provider(anthropic_provider, monkeypatch):
    called = []
    monkeypatch.setattr(anthropic_provider._client.messages, "create",
                         lambda **k: called.append(1) or _AnthropicResponse(_valid_json_response()))
    result = anthropic_provider.generate("Why?", "Axiom", [])
    assert called == [], "no passages means no provider call at all -- there is nothing to answer from"
    assert result.is_no_answer is True


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

@pytest.fixture
def openai_provider(test_env, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.providers.openai_provider import OpenAIProvider
    provider = OpenAIProvider()
    yield provider
    get_settings.cache_clear()


class _OpenAIMessage:
    def __init__(self, content):
        self.content = content


class _OpenAIChoice:
    def __init__(self, content):
        self.message = _OpenAIMessage(content)


class _OpenAIResponse:
    def __init__(self, content):
        self.choices = [_OpenAIChoice(content)]


def test_openai_timeout_becomes_provider_error(openai_provider, monkeypatch):
    import openai

    def raise_timeout(**kwargs):
        raise openai.APITimeoutError(request=_req())

    monkeypatch.setattr(openai_provider._client.chat.completions, "create", raise_timeout)
    with pytest.raises(ProviderError, match="timed out"):
        openai_provider.generate("Why?", "Axiom", [_passage()])


def test_openai_rate_limit_becomes_provider_error(openai_provider, monkeypatch):
    import openai

    def raise_rl(**kwargs):
        raise openai.RateLimitError("rate limited", response=_resp(429), body=None)

    monkeypatch.setattr(openai_provider._client.chat.completions, "create", raise_rl)
    with pytest.raises(ProviderError, match="rate-limited"):
        openai_provider.generate("Why?", "Axiom", [_passage()])


def test_openai_retired_model_status_error_becomes_provider_error(openai_provider, monkeypatch):
    import openai

    def raise_not_found(**kwargs):
        raise openai.NotFoundError("model not found", response=_resp(404), body=None)

    monkeypatch.setattr(openai_provider._client.chat.completions, "create", raise_not_found)
    with pytest.raises(ProviderError):
        openai_provider.generate("Why?", "Axiom", [_passage()])


def test_openai_malformed_response_retries_once_then_falls_back(openai_provider, monkeypatch):
    calls = []

    def create(**kwargs):
        calls.append(kwargs["messages"])
        return _OpenAIResponse("not json at all")

    monkeypatch.setattr(openai_provider._client.chat.completions, "create", create)
    result = openai_provider.generate("Why?", "Axiom", [_passage()])
    assert len(calls) == 2, "exactly one repair retry, not more, not zero"
    assert result.is_no_answer is True
    assert "could not produce a verified" in result.answer.lower()


def test_openai_recovers_on_the_repair_retry(openai_provider, monkeypatch):
    responses = [_OpenAIResponse("garbage, not JSON"), _OpenAIResponse(_valid_json_response())]

    def create(**kwargs):
        return responses.pop(0)

    monkeypatch.setattr(openai_provider._client.chat.completions, "create", create)
    result = openai_provider.generate("Why?", "Axiom", [_passage()])
    assert result.is_no_answer is False
    assert "81-118-31" in result.answer


def test_openai_no_answer_explanation_mentioning_the_machine_name_is_not_rejected(
    openai_provider, monkeypatch
):
    """Parity with the equivalent Anthropic test -- both providers share
    parse_and_validate, so both share this bug and this fix."""
    explanation = (
        "The provided excerpts do not contain an Electrical Setup procedure "
        "for the Ultra-1/Ultra-2. Please consult the Installation section."
    )
    response = json.dumps({
        "is_no_answer": True, "no_answer_explanation": explanation,
        "claims": [], "steps": [], "warnings": [],
    })
    monkeypatch.setattr(
        openai_provider._client.chat.completions, "create", lambda **k: _OpenAIResponse(response)
    )
    result = openai_provider.generate("How to do electrical setup", "Ultra-1/Ultra-2", [_passage()])
    assert result.is_no_answer is True
    assert result.answer == explanation, "the model's real explanation, not the generic fallback"


def test_openai_empty_or_none_content_safety_refusal_shape_degrades_gracefully(openai_provider, monkeypatch):
    """A content-filtered response (message.content is None, a real shape
    the OpenAI API returns for a refusal) must degrade cleanly, not crash
    with a TypeError trying to regex-search None."""
    monkeypatch.setattr(openai_provider._client.chat.completions, "create",
                         lambda **k: _OpenAIResponse(None))
    result = openai_provider.generate("Why?", "Axiom", [_passage()])
    assert result.is_no_answer is True


def test_openai_request_sets_a_bounded_token_budget(openai_provider, monkeypatch):
    captured = {}

    def create(**kwargs):
        captured["max_completion_tokens"] = kwargs.get("max_completion_tokens")
        return _OpenAIResponse(_valid_json_response())

    monkeypatch.setattr(openai_provider._client.chat.completions, "create", create)
    openai_provider.generate("Why?", "Axiom", [_passage()])
    assert captured["max_completion_tokens"] is not None, \
        "an unbounded response is both a cost risk and never needed for this app's output shape"
    assert 0 < captured["max_completion_tokens"] <= 4096


def test_openai_no_passages_short_circuits_without_calling_the_provider(openai_provider, monkeypatch):
    called = []
    monkeypatch.setattr(openai_provider._client.chat.completions, "create",
                         lambda **k: called.append(1) or _OpenAIResponse(_valid_json_response()))
    result = openai_provider.generate("Why?", "Axiom", [])
    assert called == [], "no passages means no provider call at all -- there is nothing to answer from"
    assert result.is_no_answer is True


# ---------------------------------------------------------------------------
# Provider selection ("no manual content may be sent to an unapproved
# provider")
# ---------------------------------------------------------------------------

def test_only_three_providers_are_ever_reachable(test_env):
    from app.providers.factory import get_provider
    get_provider.cache_clear()
    provider = get_provider()
    assert provider.name in ("local_extractive", "anthropic", "openai")


def test_unknown_provider_setting_refuses_to_start(test_env, monkeypatch):
    """Settings.validate_for_startup() is the actual gate -- an operator
    typo'ing AI_PROVIDER must fail loudly at startup, not silently fall
    through to whatever get_provider() does with an unrecognized value."""
    monkeypatch.setenv("AI_PROVIDER", "some_third_party_service")
    from app.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    with pytest.raises(RuntimeError, match="Unknown AI_PROVIDER"):
        settings.validate_for_startup()
    get_settings.cache_clear()
