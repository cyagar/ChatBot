"""Provider/model configuration and failure behavior contract tests: every
provider, timeouts, rate limits, malformed responses, retries, token
budgets, model retirement, safety fallback, and request cancellation.

Every network call here is mocked -- these test the CONTRACT between this
codebase and the anthropic SDK (which exceptions map to which
ProviderError, how many attempts happen, what shape a response must have),
not live model behavior. AI_PROVIDER stays local_extractive for the rest of
the test suite (see docs/PRODUCTION_READINESS.md); these tests construct
AnthropicProvider directly, bypassing the AI_PROVIDER-gated factory
entirely.

Also covers "no manual content may be sent to an unapproved provider":
get_provider() only ever returns one of exactly two hardcoded classes
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


def _clear_ambient_proxy_env(monkeypatch):
    """AnthropicProvider constructs a real SDK http client,
    whose httpx transport honors *_PROXY env vars from the developer's
    shell by default (trust_env). If one names a socks5:// proxy -- common
    behind a corporate VPN -- httpx raises ImportError at construction
    unless the optional `socksio` package is installed, failing these
    mocked contract tests for a reason with nothing to do with the
    provider contract they're checking. Confirmed by reproducing: setting
    ALL_PROXY=socks5://127.0.0.1:1 fails test_anthropic_timeout_becomes_
    provider_error with exactly that ImportError without this fixture."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

@pytest.fixture
def anthropic_provider(test_env, monkeypatch):
    _clear_ambient_proxy_env(monkeypatch)
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
    """The machine-name exemption above must not become a blanket exemption
    for every material token. A part number/voltage/error code that has
    nothing to do with the machine's own name is still exactly the
    fabrication risk that check exists for."""
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
    """Same exemption, second location: a *claim* (not just a no-answer
    explanation) naturally referencing the machine by name must not hit a
    false-positive rejection either, since claims go through
    _claim_supported, a separate function with its own material-token
    check. Validation is all-or-nothing per response, so even one claim out
    of six naming the machine would otherwise sink an entire genuine
    answer."""
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
    """The machine-name exemption in _claim_supported must not become a
    blanket exemption for every material token in a claim either -- a
    fabricated part number unrelated to the machine's own name is still
    exactly the risk that check exists for."""
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


def test_p1_19_worst_case_provider_latency_fits_under_the_android_read_timeout(anthropic_provider):
    """Worst-case provider latency (2 _call()s -- the original attempt plus
    generate()'s own JSON-repair retry -- times attempts-per-call times the
    request timeout) must fit under Android's read timeout (ApiClient.kt),
    or a technician could see "connection lost" while the server was still
    working. Pins the actual worst-case bound, not just the individual
    settings, so a future change to either number is caught if it pushes the
    total back over budget."""
    ANDROID_READ_TIMEOUT_SECONDS = 90
    CALLS_PER_GENERATE = 2  # original attempt + generate()'s own repair retry

    client = anthropic_provider._client
    attempts_per_call = 1 + client.max_retries
    worst_case_seconds = CALLS_PER_GENERATE * attempts_per_call * client.timeout

    assert worst_case_seconds <= ANDROID_READ_TIMEOUT_SECONDS, (
        f"worst-case provider latency ({worst_case_seconds}s: {CALLS_PER_GENERATE} calls x "
        f"{attempts_per_call} attempts x {client.timeout}s) must fit under the Android client's "
        f"{ANDROID_READ_TIMEOUT_SECONDS}s read timeout"
    )
    # A single transient blip (the common case) must still self-heal -- not
    # reduced all the way to zero SDK retries.
    assert client.max_retries >= 1


def test_anthropic_no_passages_short_circuits_without_calling_the_provider(anthropic_provider, monkeypatch):
    called = []
    monkeypatch.setattr(anthropic_provider._client.messages, "create",
                         lambda **k: called.append(1) or _AnthropicResponse(_valid_json_response()))
    result = anthropic_provider.generate("Why?", "Axiom", [])
    assert called == [], "no passages means no provider call at all -- there is nothing to answer from"
    assert result.is_no_answer is True


# ---------------------------------------------------------------------------
# Provider selection ("no manual content may be sent to an unapproved
# provider")
# ---------------------------------------------------------------------------

def test_only_two_providers_are_ever_reachable(test_env):
    from app.providers.factory import get_provider
    get_provider.cache_clear()
    provider = get_provider()
    assert provider.name in ("local_extractive", "anthropic")


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
