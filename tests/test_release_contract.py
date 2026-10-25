"""Release regressions for developer execution after rebuilding the native core."""
import importlib
from io import BytesIO
import json
import os
import sys
import uuid

import pytest

from selfheal import ConfigurationError, HealthCheckResult, HealthReport, SelfHealConfig, SelfHealRuntime
from selfheal.__main__ import main
from selfheal.providers import GroqProvider, provider_from_env
from selfheal.providers import compatible


def test_stale_native_interface_fails_before_provider_selection(tmp_path, monkeypatch):
    from selfheal import _core
    monkeypatch.setattr(_core, "NATIVE_API_VERSION", 0)
    with pytest.raises(ConfigurationError, match="native extension is stale"):
        SelfHealRuntime(SelfHealConfig(tmp_path, run_tests=False))


def test_sdk_initialization_without_keys_fails_before_any_api_request(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {})
    with pytest.raises(ConfigurationError, match="SelfHeal does not supply API credentials"):
        SelfHealRuntime(SelfHealConfig(tmp_path, run_tests=False))


def test_config_command_without_key_exits_with_actionable_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {"SELFHEAL_USE_GROQ": "false"})
    assert main(["check-config"]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().out


def test_config_command_uses_dotenv_without_network_or_key_disclosure(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {})
    key = "test-private-key-value"
    (tmp_path / ".env").write_text(f"SELFHEAL_USE_GROQ=true\nGROQ_API_KEY={key}\nGROQ_MODEL=test-model\n")
    def forbidden(*args, **kwargs):
        raise AssertionError("A configuration check must not make an API request")
    monkeypatch.setattr(compatible, "build_opener", forbidden)
    assert main(["check-config"]) == 0
    output = capsys.readouterr().out
    assert "GroqProvider" in output and key not in output


@pytest.mark.parametrize("key,model", [("your-groq-key", "model"), ("test-key", "your-model"), ("test-key\nheader", "model")])
def test_placeholders_and_invalid_credentials_raise_safe_configuration_errors(key, model):
    with pytest.raises(ConfigurationError) as exc:
        provider_from_env(environ={"GROQ_API_KEY": key, "GROQ_MODEL": model})
    assert key not in str(exc.value)


@pytest.mark.parametrize("field,value", [("auto_apply", "false"), ("run_tests", 1),
                                          ("snapshot_dir", "../outside"), ("audit_file", "")])
def test_invalid_runtime_configuration_fails_early(tmp_path, field, value):
    with pytest.raises(ConfigurationError):
        SelfHealConfig(tmp_path, **{field: value})


def test_real_provider_json_flows_through_verification_and_live_patch(tmp_path, monkeypatch):
    name = "api_app_" + uuid.uuid4().hex
    source = "CACHE = {}\ndef quote(price, discount):\n    return price + discount\n"
    replacement = source.replace("price + discount", "price * (1 - discount)")
    path = tmp_path / (name + ".py")
    path.write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    seen = {}
    class Opener:
        def open(self, request, timeout):
            seen.update(url=request.full_url, payload=json.loads(request.data))
            proposal = {"target_file": path.name, "replacement_source": replacement, "explanation": "Correct discount"}
            envelope = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(proposal)}}]}
            return BytesIO(json.dumps(envelope).encode())
    monkeypatch.setattr(compatible, "build_opener", lambda *args: Opener())
    events = []
    try:
        module = importlib.import_module(name)
        saved, cache = module.quote, module.CACHE
        runtime = SelfHealRuntime(SelfHealConfig(tmp_path, run_tests=False),
                                  provider=GroqProvider(api_key="test-key", model="test-model"), event_sink=events.append)
        health = lambda candidate: HealthReport((HealthCheckResult("discount", candidate.quote(100, .1) == 90),))
        result = runtime.inspect(module_name=name, symbol_name="quote", health_check=health, retry_safe=True, apply=True)
        assert result.healed and result.status == "applied"
        assert saved(100, .1) == 90 and module.CACHE is cache
        assert path.read_text() == replacement
        assert seen["url"] == "https://api.groq.com/openai/v1/chat/completions"
        context = json.loads(seen["payload"]["messages"][1]["content"])
        assert context["editable_symbols"] == ["quote"]
        assert any(event.phase == "candidate_health" for event in events)
        assert any(event.phase == "committed" for event in events)
    finally:
        sys.modules.pop(name, None)


def test_native_application_failure_disables_healing_if_source_recovery_is_uncertain(tmp_path, monkeypatch):
    name = "uncertain_app_" + uuid.uuid4().hex
    source = "def run():\n    return 1\n"
    replacement = source.replace("return 1", "return 2")
    path = tmp_path / (name + ".py")
    path.write_text(source)
    monkeypatch.syspath_prepend(str(tmp_path))
    from selfheal.models import RepairProposal
    from selfheal.providers import AIProvider
    class Candidate(AIProvider):
        def propose_repair(self, request):
            return RepairProposal(request.failure.target_file, replacement)
    runtime = SelfHealRuntime(SelfHealConfig(tmp_path, run_tests=False), provider=Candidate())
    actual = runtime.core
    class FailingCore:
        def __getattr__(self, field):
            return getattr(actual, field)
        def verify_and_apply(self, *args):
            path.write_text(replacement)
            raise OSError("simulated failed native recovery")
    runtime.core = FailingCore()
    try:
        health = lambda module: HealthReport((HealthCheckResult("result", module.run() == 2),))
        result = runtime.inspect(module_name=name, symbol_name="run", health_check=health, retry_safe=True, apply=True)
        assert result.status == "disabled" and runtime.healing_disabled
        assert importlib.import_module(name).run() == 1  # No function activation without a receipt.
    finally:
        sys.modules.pop(name, None)
