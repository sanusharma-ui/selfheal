"""Supplied for developer execution; providers are deterministic and offline."""
import importlib
import sys
from threading import Event, Thread
import uuid

import pytest

from selfheal import (AgentConfig, HealthCheckResult, HealthReport, LiveTarget,
                      SelfHealAgent, SelfHealConfig, SelfHealRuntime)
from selfheal.models import RepairProposal
from selfheal.providers import AIProvider
from selfheal import agent as agent_module

BROKEN = "KEEP = 'unchanged'\ndef run():\n    return 1\n"
FIXED = BROKEN.replace("return 1", "return 2")


class CandidateProvider(AIProvider):
    def __init__(self, source=FIXED):
        self.source = source
        self.requests = []

    def propose_repair(self, request):
        self.requests.append(request)
        return RepairProposal(request.failure.target_file, self.source, "Repair incorrect result")


@pytest.fixture
def sample(tmp_path, monkeypatch):
    name = "heal_app_" + uuid.uuid4().hex
    path = tmp_path / (name + ".py")
    path.write_text(BROKEN, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    provider = CandidateProvider()
    events = []
    runtime = SelfHealRuntime(SelfHealConfig(tmp_path, run_tests=False),
                              provider=provider, event_sink=events.append)

    def health(module):
        value = module.run()
        return HealthReport((HealthCheckResult("run result", value == 2, f"expected 2, got {value}"),))

    target = LiveTarget("sample", name, "run", health, retry_safe=True)
    yield runtime, target, path, provider, events
    sys.modules.pop(name, None)


def inspect(sample, **options):
    runtime, target, *_ = sample
    return runtime.inspect(module_name=target.module_name, symbol_name=target.symbol_name,
                           health_check=target.health_check, retry_safe=True,
                           editable_symbols=target.editable_symbols, **options)


def test_silent_logic_bug_is_patched_and_health_confirmed(sample):
    runtime, target, path, provider, events = sample
    assert runtime.invoke(module_name=target.module_name, symbol_name="run") == 1
    result = inspect(sample, apply=True)
    assert result.healed and result.status == "applied"
    assert path.read_text() == FIXED
    assert runtime.invoke(module_name=target.module_name, symbol_name="run") == 2
    assert provider.requests[0].operation == "heal"
    assert "expected 2, got 1" in provider.requests[0].health_report
    assert any(event.diff for event in events)
    assert any(event.phase == "activation_health" for event in events)


def test_preview_does_not_mutate_live_module(sample):
    runtime, target, path, _, _ = sample
    result = inspect(sample)
    assert result.status == "preview" and not result.healed
    assert path.read_text() == BROKEN
    assert importlib.import_module(target.module_name).run() == 1


def test_empty_health_report_cannot_authorize_a_patch(sample):
    runtime, target, path, _, _ = sample
    result = runtime.inspect(module_name=target.module_name, symbol_name="run",
                             health_check=lambda module: HealthReport(()), retry_safe=True,
                             editable_symbols=("run",), apply=True)
    assert result.status == "rolled_back"
    assert path.read_text() == BROKEN


def test_stale_provider_result_preserves_a_developer_edit(sample):
    _, _, path, provider, _ = sample
    propose = provider.propose_repair
    manual = BROKEN.replace("return 1", "return 7")

    def edit_while_generating(request):
        path.write_text(manual)
        return propose(request)

    provider.propose_repair = edit_while_generating
    result = inspect(sample, apply=True)
    assert not result.healed and "Source changed" in result.message
    assert path.read_text() == manual


def test_health_failure_rolls_back_a_syntactically_valid_patch(sample):
    runtime, target, path, provider, events = sample
    provider.source = BROKEN.replace("return 1", "return 3")
    result = inspect(sample, apply=True)
    assert result.status == "rolled_back" and not result.healed
    assert path.read_bytes() == BROKEN.encode()
    assert importlib.import_module(target.module_name).run() == 1
    assert any(event.phase == "rollback_finished" for event in events)


@pytest.mark.parametrize("source", [FIXED.replace("unchanged", "tampered"),
                                    FIXED.replace("def run():", "def run(value=0):"),
                                    FIXED + "def side_effect():\n    return 0\n"])
def test_edit_policy_rejects_unrelated_changes_and_signature_changes(sample, source):
    _, _, path, provider, events = sample
    provider.source = source
    result = inspect(sample, apply=True)
    assert result.status == "failed" and not result.healed
    assert path.read_text() == BROKEN
    assert any(event.phase == "scope_rejected" for event in events)
    assert any(event.diff for event in events)  # rejected candidates remain visible


def test_other_calls_continue_while_ai_request_is_pending(sample):
    runtime, target, path, provider, _ = sample
    entered, release = Event(), Event()
    propose = provider.propose_repair

    def wait_for_ai(request):
        entered.set()
        assert release.wait(10)
        return propose(request)

    provider.propose_repair = wait_for_ai
    result = []
    thread = Thread(target=lambda: result.append(inspect(sample, apply=True)))
    thread.start()
    try:
        assert entered.wait(10)
        assert runtime.invoke(module_name=target.module_name, symbol_name="run") == 1
        assert path.read_text() == BROKEN
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and result[0].healed


def test_periodic_attempts_obey_budget_and_failure_backoff(sample, monkeypatch):
    runtime, target, _, provider, _ = sample
    clock = [100.0]
    monkeypatch.setattr(agent_module.time, "monotonic", lambda: clock[0])
    provider.source = BROKEN.replace("return 1", "return 3")
    agent = SelfHealAgent(runtime, (target,), config=AgentConfig(
        auto_apply=True, failure_backoff_seconds=10, max_consecutive_failures=2,
        max_attempts_per_hour=2))
    agent._scan("sample")
    assert len(provider.requests) == 1
    agent._scan("sample")
    assert len(provider.requests) == 1  # no retry within backoff
    clock[0] += 11
    agent._scan("sample")
    assert agent.status()["suspended"] == ["sample"]
    clock[0] += 100
    agent._scan("sample")
    assert len(provider.requests) == 2
    agent.reset("sample")
    agent._scan("sample")
    assert len(provider.requests) == 2  # reset does not erase hourly budget
    clock[0] += 3601
    provider.source = FIXED
    agent._scan("sample")
    assert len(provider.requests) == 3
    assert agent.status()["last_results"]["sample"]["status"] == "applied"


def test_background_worker_performs_a_scheduled_repair(sample):
    runtime, target, path, provider, events = sample
    finished = Event()

    def observe(event):
        events.append(event)
        if event.phase == "complete":
            finished.set()

    runtime.event_sink = observe
    agent = SelfHealAgent(runtime, (target,), config=AgentConfig(auto_apply=True, run_on_start=True))
    agent.start()
    try:
        assert finished.wait(15)
    finally:
        assert agent.stop(timeout=15)
    assert path.read_text() == FIXED and len(provider.requests) == 1
    assert agent.status()["last_results"]["sample"]["status"] == "applied"


def test_healthy_background_checks_skip_ai_but_forced_scan_inspects(sample):
    runtime, target, _, provider, _ = sample
    assert inspect(sample, apply=True).healed
    provider.requests.clear()
    agent = SelfHealAgent(runtime, (target,))
    agent._scan("sample")
    assert not provider.requests
    agent._scan("sample", force=True)
    assert len(provider.requests) == 1


def test_bound_failure_is_preserved_and_queued_even_when_probes_pass(sample):
    runtime, target, path, provider, _ = sample
    path.write_text("KEEP = 'unchanged'\ndef run():\n    raise ZeroDivisionError('observed')\n")
    permissive = LiveTarget("sample", target.module_name, "run",
                           lambda module: HealthReport((HealthCheckResult("other inputs", True),)), retry_safe=True)
    agent = SelfHealAgent(runtime, (permissive,))
    with pytest.raises(ZeroDivisionError):
        agent.bind("sample")()
    assert agent.status()["pending"] == ["sample"]
    agent._scan("sample", force=True)
    assert "ZeroDivisionError" in provider.requests[0].health_report
