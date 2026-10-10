"""Developer-run regressions. Do not run without the maintainer's permission."""
from __future__ import annotations

import importlib
from pathlib import Path
import sys
from threading import Lock
import time
import uuid

import pytest

from selfheal import (ConfigurationError, HealthCheckResult, HealthReport, RepairTimeout,
                      SelfHealConfig, SelfHealRuntime)
from selfheal.models import RepairProposal
from selfheal.providers import AIProvider
from selfheal.timing import RepairDeadline, current_deadline


@pytest.fixture
def clean_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("SELFHEAL_REPAIR_TIMEOUT_SECONDS", "SELFHEAL_TIMEOUT_CLEANUP_SECONDS", "SELFHEAL_HARD_TIMEOUT"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_dotenv_budget_and_explicit_precedence(clean_env, monkeypatch):
    (clean_env / ".env").write_text("SELFHEAL_REPAIR_TIMEOUT_SECONDS=24\nSELFHEAL_HARD_TIMEOUT=false\n")
    config = SelfHealConfig(clean_env)
    assert config.repair_timeout_seconds == 24 and config.hard_timeout is False
    monkeypatch.setenv("SELFHEAL_REPAIR_TIMEOUT_SECONDS", "30")
    assert SelfHealConfig(clean_env).repair_timeout_seconds == 30
    assert SelfHealConfig(clean_env, repair_timeout_seconds=12).repair_timeout_seconds == 12


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "3601", "private-invalid-value", ""])
def test_invalid_env_deadline_fails_without_echoing_value(clean_env, monkeypatch, value):
    monkeypatch.setenv("SELFHEAL_REPAIR_TIMEOUT_SECONDS", value)
    with pytest.raises(ConfigurationError) as exc:
        SelfHealConfig(clean_env)
    assert "SELFHEAL_REPAIR_TIMEOUT_SECONDS" in str(exc.value)
    assert "private-invalid-value" not in str(exc.value)


def test_nested_operations_share_deadline_and_cleanup_is_inside_total(clean_env):
    config = SelfHealConfig(clean_env, repair_timeout_seconds=10, timeout_cleanup_seconds=5, hard_timeout=False)
    assert current_deadline() is None
    with RepairDeadline(config) as budget:
        assert budget.hard_at - budget.work_at == pytest.approx(1)
        with RepairDeadline(config) as nested:
            assert nested is budget and current_deadline() is budget
        assert current_deadline() is budget
    assert current_deadline() is None


def test_application_scheduling_clock_cannot_desynchronize_watchdog_budget(clean_env, monkeypatch):
    from selfheal import agent as agent_module
    config = SelfHealConfig(clean_env, repair_timeout_seconds=10, hard_timeout=False)
    actual = time.monotonic()
    monkeypatch.setattr(agent_module.time, "monotonic", lambda: 100)
    budget = RepairDeadline(config)
    assert actual <= budget.started < actual + 2
    assert 8 <= budget.remaining() <= 9


def test_python_loop_interrupt_does_not_leave_trace_hook_installed(clean_env):
    previous = sys.gettrace()
    config = SelfHealConfig(clean_env, repair_timeout_seconds=1, hard_timeout=False)
    with RepairDeadline(config) as budget:
        def loop():
            while True:
                pass
        with pytest.raises(RepairTimeout):
            budget.run(loop, "live retry")
        assert sys.gettrace() is previous


class Candidate(AIProvider):
    def __init__(self, source):
        self.source = source

    def propose_repair(self, request):
        return RepairProposal(request.failure.target_file, self.source, "Timing regression")


@pytest.fixture
def sample(clean_env, monkeypatch):
    name = "deadline_app_" + uuid.uuid4().hex
    source = "CACHE = {}\ndef run():\n    return 1\n"
    path = clean_env / (name + ".py")
    path.write_text(source)
    monkeypatch.syspath_prepend(str(clean_env))
    module = importlib.import_module(name)
    config = SelfHealConfig(clean_env, run_tests=False, repair_timeout_seconds=4, hard_timeout=False)
    runtime = SelfHealRuntime(config, provider=Candidate(source.replace("return 1", "return 2")))
    yield runtime, module, path, source
    sys.modules.pop(name, None)


def healthy(module):
    return HealthReport((HealthCheckResult("run", module.run() == 2),))


def test_hung_candidate_is_killed_without_source_or_live_state_mutation(sample):
    runtime, module, path, source = sample
    runtime.provider.source = source.replace("return 1", "while True:\n        pass")
    saved = module.run
    started = time.monotonic()
    result = runtime.inspect(module_name=module.__name__, symbol_name="run", health_check=healthy,
                             retry_safe=True, apply=True)
    assert result.status == "timed_out" and not result.healed
    assert time.monotonic() - started < 7
    assert path.read_text() == source and module.run is saved and saved() == 1
    assert not list((path.parent / ".selfheal/health-workers").glob("candidate-*"))
    assert runtime.invoke(module_name=module.__name__, symbol_name="run") == 1


def test_candidate_mutation_cannot_touch_live_cache(sample):
    runtime, module, path, source = sample
    runtime.provider.source = source.replace("return 1", "CACHE['candidate'] = 1\n    return 2")
    def check(candidate):
        # Worker sees the mutation in its own copy. Live check intentionally
        # fails before invoking the candidate, demonstrating source rollback.
        if candidate is module:
            return HealthReport((HealthCheckResult("live gate", False),))
        return healthy(candidate)
    result = runtime.inspect(module_name=module.__name__, symbol_name="run", health_check=check,
                             retry_safe=True, apply=True)
    assert result.status == "rolled_back"
    assert module.CACHE == {} and path.read_text() == source


def test_nonserializable_candidate_state_fails_closed(sample):
    runtime, module, path, source = sample
    module.LIVE_LOCK = Lock()
    result = runtime.inspect(module_name=module.__name__, symbol_name="run", health_check=healthy,
                             retry_safe=True, apply=True)
    assert result.status == "failed" and "cannot be serialized" in result.message
    assert path.read_text() == source and module.run() == 1


def test_live_retry_loop_rolls_back_and_preserves_original_exception(sample):
    runtime, module, path, _ = sample
    original = "CACHE = {}\ndef run():\n    return 1 / 0\n"
    path.write_text(original)
    exec(compile(original, str(path), "exec"), module.__dict__)
    saved, saved_code = module.run, module.run.__code__
    runtime.provider.source = original.replace("return 1 / 0", "while True:\n        pass")
    runtime.config.auto_apply = True
    with pytest.raises(ZeroDivisionError):
        runtime.call(module_name=module.__name__, symbol_name="run", retry_safe=True)
    assert runtime.last_result.status == "timed_out"
    assert path.read_text() == original and saved.__code__ is saved_code
    assert not runtime.healing_disabled
    with pytest.raises(ZeroDivisionError):
        runtime.invoke(module_name=module.__name__, symbol_name="run")


def test_provider_and_review_consume_one_budget(sample):
    runtime, module, path, _ = sample
    original = "CACHE = {}\ndef run():\n    return 1 / 0\n"
    path.write_text(original)
    exec(compile(original, str(path), "exec"), module.__dict__)
    runtime.config.repair_timeout_seconds = 2
    runtime.provider.source = original.replace("return 1 / 0", "return 42")
    propose = runtime.provider.propose_repair
    def delayed(request):
        time.sleep(1.1)
        return propose(request)
    def reviewer(_):
        time.sleep(1.1)
        return True
    runtime.provider.propose_repair = delayed
    runtime.reviewer = reviewer
    with pytest.raises(ZeroDivisionError):
        runtime.call(module_name=module.__name__, symbol_name="run", retry_safe=True)
    assert runtime.last_result.status == "timed_out" and path.read_text() == original


def test_native_verification_timeout_is_one_budget_including_pytest(clean_env):
    from selfheal._core import CoreEngine
    path = clean_env / "app.py"
    path.write_text("VALUE = 1\n")
    (clean_env / "test_slow.py").write_text("import time\ndef test_slow():\n    time.sleep(30)\n")
    core = CoreEngine(str(clean_env), sys.executable, True)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="deadline"):
        core.verify_and_apply(str(path), "VALUE = 2\n", timeout_seconds=1)
    assert time.monotonic() - started < 4 and path.read_text() == "VALUE = 1\n"


def test_watchdog_recovery_never_overwrites_a_manual_edit(clean_env):
    # Import the standalone helper by path without starting its executable entry.
    import importlib.util
    helper = Path(__file__).parents[1] / "python/selfheal/_watchdog.py"
    spec = importlib.util.spec_from_file_location("deadline_watchdog_test", helper)
    watchdog = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(watchdog)
    import hashlib
    target, snapshot = clean_env / "app.py", clean_env / ".selfheal/snapshots/id/app.py"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"original")
    record = {"root": str(clean_env), "target": str(target), "snapshot": str(snapshot),
              "snapshot_root": str(clean_env / ".selfheal/snapshots"),
              "original_hash": hashlib.sha256(b"original").hexdigest(),
              "candidate_hash": hashlib.sha256(b"candidate").hexdigest()}
    target.write_bytes(b"developer edit")
    watchdog._restore(record)
    assert target.read_bytes() == b"developer edit"
    target.write_bytes(b"candidate")
    watchdog._restore(record)
    assert target.read_bytes() == b"original"
