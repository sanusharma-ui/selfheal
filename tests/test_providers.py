import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from selfheal.models import FailureContext, RepairRequest
from selfheal.providers import (
    ContextRejected, FallbackProvider, GroqProvider, InvalidProposal,
    OpenAIProvider, ProviderUnavailable, provider_from_env,
)
from selfheal.providers import compatible


@pytest.fixture
def request_data(tmp_path):
    target = tmp_path / "app.py"
    return RepairRequest(
        FailureContext("ZeroDivisionError", "division by zero", "trace", "app", "run",
                       target, "('private argument',)", "{'password': 'private'}"),
        "def run():\n    return 1 / 0\n", tmp_path,
    )


def response(data=None, *, finish="stop", content=None):
    if content is None:
        content = json.dumps(data or {"target_file": "app.py", "replacement_source": "def run():\n    return 0\n", "explanation": "Guard zero"})
    return json.dumps({"choices": [{"finish_reason": finish, "message": {"content": content}}]}).encode()


def provider(**kwargs):
    return GroqProvider(api_key="test-key", model="test-model", **kwargs)


@pytest.mark.parametrize("env,kind", [
    ({"GROQ_API_KEY": "g", "GROQ_MODEL": "gm"}, GroqProvider),
    ({"OPENAI_API_KEY": "o", "OPENAI_MODEL": "om"}, OpenAIProvider),
    ({"GROQ_API_KEY": "g", "GROQ_MODEL": "gm", "OPENAI_API_KEY": "o", "OPENAI_MODEL": "om"}, GroqProvider),
])
def test_available_key_selection(env, kind):
    assert isinstance(provider_from_env(environ=env), kind)


def test_priority_and_explicit_fallback():
    env = {"GROQ_API_KEY": "g", "GROQ_MODEL": "gm", "OPENAI_API_KEY": "o", "OPENAI_MODEL": "om"}
    assert isinstance(provider_from_env(environ=env, priority=("openai", "groq")), OpenAIProvider)
    chain = provider_from_env(environ=env, fallback=True)
    assert isinstance(chain, FallbackProvider)
    assert [p.model for p in chain.providers] == ["gm", "om"]


@pytest.mark.parametrize("env", [{}, {"GROQ_API_KEY": "g"}, {"SELFHEAL_PROVIDER_PRIORITY": "groq,unknown"}])
def test_bad_configuration_fails_early(env):
    with pytest.raises(ValueError):
        provider_from_env(environ=env)


def test_wire_request_and_valid_proposal(monkeypatch, request_data):
    captured = {}
    class Opener:
        def open(self, req, timeout):
            captured.update(url=req.full_url, headers=req.headers, body=json.loads(req.data), timeout=timeout)
            return BytesIO(response())
    monkeypatch.setattr(compatible, "build_opener", lambda *args: Opener())
    result = provider().propose_repair(request_data)
    assert result.target_file == request_data.failure.target_file
    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["model"] == "test-model"
    assert "private argument" not in json.dumps(captured["body"])
    assert captured["timeout"] == 30


@pytest.mark.parametrize("raw", [
    b"not json", response(finish="length"), response(content="```json\n{}\n```"),
    response(content='{"target_file":"app.py","target_file":"evil.py"}'),
    response({"target_file": "../outside.py", "replacement_source": "pass", "explanation": ""}),
    response({"target_file": "app.py", "replacement_source": 42, "explanation": ""}),
    response({"target_file": "app.py", "replacement_source": "", "explanation": ""}),
    response({"target_file": "app.py", "replacement_source": "pass", "explanation": "", "command": "x"}),
    response({"target_file": "app.py", "replacement_source": "SECRET = '[REDACTED]'", "explanation": ""}),
])
def test_invalid_outputs_fail_closed(monkeypatch, request_data, raw):
    p = provider()
    monkeypatch.setattr(p, "_post", lambda payload: raw)
    with pytest.raises(InvalidProposal):
        p.propose_repair(request_data)


def test_no_proposal(monkeypatch, request_data):
    p = provider()
    monkeypatch.setattr(p, "_post", lambda payload: response(content="null"))
    assert p.propose_repair(request_data) is None


@pytest.mark.parametrize("error,retry_count", [
    (HTTPError("https://example", 429, "secret", {}, None), 2),
    (HTTPError("https://example", 503, "secret", {}, None), 2),
    (HTTPError("https://example", 401, "secret", {}, None), 1),
    (URLError("secret"), 2), (TimeoutError("secret"), 2),
])
def test_bounded_retries_and_safe_errors(monkeypatch, request_data, error, retry_count):
    p = provider(max_retries=1)
    calls = []
    def fail(payload):
        calls.append(1)
        raise error
    monkeypatch.setattr(p, "_post", fail)
    monkeypatch.setattr(compatible.time, "sleep", lambda _: None)
    with pytest.raises(ProviderUnavailable) as exc:
        p.propose_repair(request_data)
    assert len(calls) == retry_count
    assert "secret" not in str(exc.value)


def test_fallback_after_unavailable_only(monkeypatch, request_data):
    first, second = provider(max_retries=0), provider()
    calls = []
    def fail(_):
        raise ProviderUnavailable("timeout")
    monkeypatch.setattr(first, "propose_repair", fail)
    monkeypatch.setattr(second, "propose_repair", lambda req: calls.append(req) or "candidate")
    chain = FallbackProvider([first, second])
    assert chain.propose_repair(request_data) == "candidate"
    def invalid(_):
        raise InvalidProposal("invalid")
    monkeypatch.setattr(first, "propose_repair", invalid)
    with pytest.raises(InvalidProposal):
        chain.propose_repair(request_data)
    assert len(calls) == 1
    monkeypatch.setattr(first, "propose_repair", lambda req: None)
    assert chain.propose_repair(request_data) is None
    assert len(calls) == 1


def test_secrets_redacted_and_source_secrets_blocked(monkeypatch, request_data):
    monkeypatch.setenv("SERVICE_TOKEN", "sensitive-value-123")
    request_data.failure.message = "token=sensitive-value-123"
    request_data.failure.traceback_text = "Authorization: Bearer abcdef123456789"
    payload, _ = provider()._payload(request_data)
    assert b"sensitive-value-123" not in payload
    assert b"abcdef123456789" not in payload
    request_data.source_code = "API_KEY = 'gsk_abcdefghijklmnop123456'\n"
    with pytest.raises(ContextRejected):
        provider()._payload(request_data)


def test_context_and_replacement_limits(monkeypatch, request_data):
    with pytest.raises(ContextRejected):
        provider(max_context_bytes=10).propose_repair(request_data)
    p = provider(max_replacement_bytes=4)
    monkeypatch.setattr(p, "_post", lambda _: response())
    with pytest.raises(InvalidProposal):
        p.propose_repair(request_data)


@pytest.mark.parametrize("url", ["http://example.com", "https://user:pass@example.com", "https://example.com?a=b"])
def test_credentials_only_over_valid_https(url):
    with pytest.raises(ValueError):
        provider(base_url=url)


def test_redirects_refused():
    assert compatible._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil") is None
