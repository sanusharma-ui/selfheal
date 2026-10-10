"""Developer-run async regressions with deterministic providers; no API calls.

These use asyncio.run so pytest-asyncio is not required. Run against a locally
built/installed checkout. No tests or builds were run while authoring this file.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from threading import Event, get_ident
import uuid

import pytest

from selfheal import (AgentConfig, HealthCheckResult, HealthReport, LiveTarget,
                      ConfigurationError, RepairTimeout, SelfHealAgent, SelfHealConfig, SelfHealRuntime)
from selfheal.models import RepairProposal
from selfheal.providers import AIProvider


BROKEN = "import asyncio\nasync def run():\n    await asyncio.sleep(0)\n    return 1 / 0\n"
FIXED = BROKEN.replace("1 / 0", "6 * 7")


class CandidateProvider(AIProvider):
    def __init__(self, source=FIXED):
        self.source = source
        self.requests = []
        self.threads = []
        self.entered = Event()
        self.release = None

    def propose_repair(self, request):
        self.requests.append(request)
        self.threads.append(get_ident())
        self.entered.set()
        if self.release is not None:
            assert self.release.wait(15), "Test did not release the provider"
        return RepairProposal(request.failure.target_file, self.source, "Fix async behavior")


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    names = []

    def create(source=BROKEN, replacement=FIXED, **config):
        name = "async_app_" + uuid.uuid4().hex
        names.append(name)
        path = tmp_path / (name + ".py")
        path.write_bytes(source.encode("utf-8"))
        provider = CandidateProvider(replacement)
        events = []
        options = dict(run_tests=False, auto_apply=True, hard_timeout=False,
                       repair_timeout_seconds=30, timeout_cleanup_seconds=2)
        options.update(config)
        runtime = SelfHealRuntime(SelfHealConfig(tmp_path, **options),
                                  provider=provider, event_sink=events.append)
        return runtime, name, path, provider, events

    yield create
    for name in names:
        sys.modules.pop(name, None)


async def until(predicate):
    async with asyncio.timeout(15):
        while not predicate():
            await asyncio.sleep(0.005)


async def health_42(module):
    value = await module.run()
    return HealthReport((HealthCheckResult("async result", value == 42,
                                          f"expected 42, got {value}"),))


def test_acall_awaits_original_and_retry_on_the_owning_loop(app_factory):
    source = ("import asyncio\nINITIALIZED = object()\n"
              "async def run(future, expected_loop):\n"
              "    assert asyncio.get_running_loop() is expected_loop\n"
              "    return (await future) / 0\n")
    fixed = source.replace(" / 0", " / 2")
    runtime, name, path, provider, _ = app_factory(source, fixed)
    module = importlib.import_module(name)
    function, initialized = module.run, module.INITIALIZED

    async def scenario():
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.set_result(84)
        result = await runtime.acall(module_name=name, symbol_name="run",
                                     args=(future, loop), retry_safe=True)
        assert result == 42
        assert provider.threads == [provider.threads[0]]
        assert provider.threads[0] != get_ident()
        assert module.run is function and module.INITIALIZED is initialized
        assert await function(future, loop) == 42

    asyncio.run(scenario())
    assert len(provider.requests) == 1
    assert runtime.last_result.healed and path.read_text() == fixed


def test_ainvoke_preserves_error_without_requesting_repair(app_factory):
    runtime, name, path, provider, _ = app_factory()

    async def scenario():
        with pytest.raises(ZeroDivisionError):
            await runtime.ainvoke(module_name=name, symbol_name="run")

    asyncio.run(scenario())
    assert not provider.requests and path.read_text() == BROKEN


@pytest.mark.parametrize("options", [{"auto_apply": False}, {"retry_after_repair": False}])
def test_async_preview_and_disabled_retry_never_activate(app_factory, options):
    runtime, name, path, _, _ = app_factory(**options)

    async def scenario():
        with pytest.raises(ZeroDivisionError):
            await runtime.acall(module_name=name, symbol_name="run", retry_safe=True)

    asyncio.run(scenario())
    assert not runtime.last_result.healed and path.read_text() == BROKEN


def test_async_retry_requires_explicit_safety_opt_in(app_factory):
    runtime, name, path, _, _ = app_factory()

    async def scenario():
        with pytest.raises(ZeroDivisionError):
            await runtime.acall(module_name=name, symbol_name="run")

    asyncio.run(scenario())
    assert path.read_text() == BROKEN
    assert "retry_safe=True" in runtime.last_result.message


def test_cancelling_original_call_never_requests_ai(app_factory):
    source = ("import asyncio\nasync def run(started):\n"
              "    started.set()\n    await asyncio.Event().wait()\n")
    runtime, name, path, provider, _ = app_factory(source, source)

    async def scenario():
        started = asyncio.Event()
        task = asyncio.create_task(runtime.acall(module_name=name, symbol_name="run",
                                                args=(started,), retry_safe=True))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not provider.requests and path.read_text() == source


def test_pending_cancellation_and_application_error_do_not_start_repair(app_factory):
    source = ("import asyncio\nasync def run():\n"
              "    asyncio.current_task().cancel()\n    raise ValueError('failed while cancelling')\n")
    runtime, name, path, provider, _ = app_factory(source, source)

    async def scenario():
        task = asyncio.create_task(runtime.acall(module_name=name, symbol_name="run", retry_safe=True))
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not provider.requests and path.read_text() == source


def test_cancel_during_provider_drains_without_activating(app_factory):
    runtime, name, path, provider, events = app_factory()
    provider.release = Event()

    async def scenario():
        task = asyncio.create_task(runtime.acall(module_name=name, symbol_name="run", retry_safe=True))
        try:
            await until(provider.entered.is_set)
            task.cancel()
            await asyncio.sleep(0.03)
            assert not task.done(), "Cancellation must drain the accepted repair worker"
            assert path.read_text() == BROKEN
            provider.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 15)
            assert not runtime.healing_disabled
            with pytest.raises(ZeroDivisionError):
                await runtime.ainvoke(module_name=name, symbol_name="run")
        finally:
            provider.release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(provider.requests) == 1 and path.read_text() == BROKEN
    assert not any(event.phase == "patch_applied" for event in events)


def test_cancel_during_retry_waits_finally_then_restores_source_and_code(app_factory):
    source = ("import asyncio\nasync def run(started, cleaning, release):\n"
              "    await asyncio.sleep(0)\n    raise ValueError('original failure')\n")
    fixed = ("import asyncio\nasync def run(started, cleaning, release):\n"
             "    try:\n        started.set()\n        await asyncio.Event().wait()\n"
             "    finally:\n        cleaning.set()\n        await release.wait()\n")
    runtime, name, path, provider, events = app_factory(source, fixed)
    module = importlib.import_module(name)
    function, original_code = module.run, module.run.__code__

    async def scenario():
        started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(runtime.acall(module_name=name, symbol_name="run",
                                                args=(started, cleaning, release), retry_safe=True))
        try:
            await asyncio.wait_for(started.wait(), 15)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 5)
            task.cancel()  # A second request must not interrupt callback cleanup.
            await asyncio.sleep(0.03)
            assert not task.done()
            assert path.read_text() == fixed
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 15)
            assert path.read_text() == source
            assert module.run is function and function.__code__ is original_code
            with pytest.raises(ValueError, match="original failure"):
                await runtime.ainvoke(module_name=name, symbol_name="run",
                                      args=(started, cleaning, release))
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert len(provider.requests) == 1
    assert not runtime.healing_disabled
    assert any(event.phase == "rollback_finished" for event in events)
    assert not any(event.phase == "committed" for event in events)


def test_failed_async_retry_rolls_back_once_without_recursive_repair(app_factory):
    fixed = BROKEN.replace("return 1 / 0", "raise RuntimeError('candidate failed')")
    runtime, name, path, provider, events = app_factory(replacement=fixed)
    module = importlib.import_module(name)
    original_code = module.run.__code__

    async def scenario():
        with pytest.raises(ZeroDivisionError):
            await runtime.acall(module_name=name, symbol_name="run", retry_safe=True)

    asyncio.run(scenario())
    assert path.read_text() == BROKEN and module.run.__code__ is original_code
    assert len(provider.requests) == 1 and not runtime.last_result.healed
    assert any(event.phase == "rollback_finished" for event in events)


def test_async_health_catches_silent_wrong_result_and_confirms_repair(app_factory):
    source = BROKEN.replace("1 / 0", "1")
    runtime, name, path, provider, events = app_factory(source, FIXED)

    async def scenario():
        before = await runtime.acheck_health(name, health_42)
        assert not before.ok and not provider.requests
        result = await runtime.ainspect(module_name=name, symbol_name="run",
                                       health_check=health_42, retry_safe=True, apply=True)
        assert result.healed
        assert (await runtime.acheck_health(name, health_42)).ok
        assert await runtime.ainvoke(module_name=name, symbol_name="run") == 42

    asyncio.run(scenario())
    assert path.read_text() == FIXED
    assert len(provider.requests) == 1
    assert "expected 42, got 1" in provider.requests[0].health_report
    assert any(event.phase == "candidate_health" for event in events)
    assert any(event.phase == "activation_health" for event in events)


def test_failed_async_candidate_health_never_activates(app_factory):
    source = BROKEN.replace("1 / 0", "1")
    runtime, name, path, provider, events = app_factory(source, source.replace("return 1", "return 3"))

    async def scenario():
        result = await runtime.ainspect(module_name=name, symbol_name="run",
                                       health_check=health_42, retry_safe=True, apply=True)
        assert not result.healed
        assert await runtime.ainvoke(module_name=name, symbol_name="run") == 1

    asyncio.run(scenario())
    assert len(provider.requests) == 1 and path.read_text() == source
    assert not any(event.phase == "patch_applied" for event in events)


def test_calls_continue_during_generation_and_activation_drains_inflight(app_factory):
    source = ("import asyncio\nasync def run(started=None, release=None):\n"
              "    if started is not None:\n        started.set()\n        await release.wait()\n"
              "    return 1\n")
    fixed = source.replace("return 1", "return 42")
    runtime, name, path, provider, events = app_factory(source, fixed)
    provider.release = Event()

    async def scenario():
        inspection = asyncio.create_task(runtime.ainspect(module_name=name, symbol_name="run",
                                                         health_check=health_42, retry_safe=True, apply=True))
        started, release = asyncio.Event(), asyncio.Event()
        active = late = None
        try:
            await until(provider.entered.is_set)
            assert await runtime.ainvoke(module_name=name, symbol_name="run") == 1
            active = asyncio.create_task(runtime.ainvoke(module_name=name, symbol_name="run",
                                                        args=(started, release)))
            await asyncio.wait_for(started.wait(), 5)
            provider.release.set()
            await until(lambda: runtime._gate._waiting_writers > 0)
            late = asyncio.create_task(runtime.ainvoke(module_name=name, symbol_name="run"))
            await asyncio.sleep(0.03)
            assert not late.done() and not inspection.done()
            assert path.read_text() == source
            assert not any(event.phase == "patch_applied" for event in events)
            release.set()
            assert await asyncio.wait_for(active, 5) == 1
            assert (await asyncio.wait_for(inspection, 15)).healed
            assert await asyncio.wait_for(late, 5) == 42
        finally:
            provider.release.set()
            release.set()
            await asyncio.gather(*(task for task in (active, late, inspection) if task is not None),
                                 return_exceptions=True)

    asyncio.run(scenario())
    assert path.read_text() == fixed


def test_failed_automatic_repairs_obey_cooldown(app_factory):
    invalid = BROKEN.replace("return 1 / 0", "raise RuntimeError('still broken')")
    runtime, name, path, provider, _ = app_factory(replacement=invalid,
                                                 repair_failure_cooldown_seconds=60)

    async def scenario():
        for _ in range(4):
            with pytest.raises(ZeroDivisionError):
                await runtime.acall(module_name=name, symbol_name="run", retry_safe=True)

    asyncio.run(scenario())
    assert len(provider.requests) == 1 and path.read_text() == BROKEN
    assert runtime.last_result.status == "cooldown"


def test_automatic_repair_hourly_budget_bounds_repeated_failures(app_factory):
    invalid = BROKEN.replace("return 1 / 0", "raise RuntimeError('still broken')")
    runtime, name, path, provider, _ = app_factory(replacement=invalid,
                                                 max_auto_repairs_per_hour=2,
                                                 repair_failure_cooldown_seconds=0)

    async def scenario():
        for _ in range(5):
            with pytest.raises(ZeroDivisionError):
                await runtime.acall(module_name=name, symbol_name="run", retry_safe=True)

    asyncio.run(scenario())
    assert len(provider.requests) == 2 and path.read_text() == BROKEN
    assert runtime.last_result.status == "budget_limited"


def test_runtime_refuses_a_different_event_loop(app_factory):
    runtime, name, _, provider, _ = app_factory(FIXED, FIXED)

    async def first():
        assert await runtime.ainvoke(module_name=name, symbol_name="run") == 42

    async def second():
        with pytest.raises(RuntimeError, match="loop"):
            await runtime.ainvoke(module_name=name, symbol_name="run")

    asyncio.run(first())
    asyncio.run(second())
    assert not provider.requests


def test_agent_async_binding_preserves_failures_and_queues_inspection(app_factory):
    runtime, name, _, provider, _ = app_factory()
    target = LiveTarget("sample", name, "run", health_42, retry_safe=True)
    agent = SelfHealAgent(runtime, (target,))
    protected = agent.bind("sample")
    assert inspect.iscoroutinefunction(protected)

    async def scenario():
        with pytest.raises(ZeroDivisionError):
            await protected()
        assert agent.status()["pending"] == ["sample"]
        assert not provider.requests

    asyncio.run(scenario())


def test_async_agent_context_repairs_and_stops_without_blocking_loop(app_factory):
    source = BROKEN.replace("1 / 0", "1")
    runtime, name, path, provider, _ = app_factory(source, FIXED)
    target = LiveTarget("sample", name, "run", health_42, retry_safe=True)
    agent = SelfHealAgent(runtime, (target,), config=AgentConfig(auto_apply=True, run_on_start=True))

    async def scenario():
        async with agent:
            await until(lambda: agent.status()["last_results"].get("sample", {}).get("status") == "applied")
            assert await agent.acall("sample") == 42
        assert not agent.status()["running"]

    asyncio.run(scenario())
    assert len(provider.requests) == 1 and path.read_text() == FIXED


def test_cancellation_while_draining_active_calls_never_contacts_provider(app_factory):
    source = BROKEN + ("\nasync def waiting(started, release):\n"
                       "    started.set()\n    await release.wait()\n    return 7\n")
    runtime, name, path, provider, _ = app_factory(source, source.replace("1 / 0", "42"))

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        active = asyncio.create_task(runtime.ainvoke(module_name=name, symbol_name="waiting",
                                                    args=(started, release)))
        repairing = None
        try:
            await started.wait()
            repairing = asyncio.create_task(runtime.acall(module_name=name, symbol_name="run", retry_safe=True))
            await until(lambda: runtime._gate._waiting_writers > 0)
            repairing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(repairing, 5)
            assert not provider.requests and not active.done()
        finally:
            release.set()
            await asyncio.gather(*(t for t in (active, repairing) if t is not None), return_exceptions=True)
        assert runtime._gate._readers == 0 and runtime._gate._waiting_writers == 0

    asyncio.run(scenario())
    assert path.read_text() == source


def test_async_health_timeout_releases_gate_without_changing_loop_trace(app_factory):
    runtime, name, _, provider, _ = app_factory(FIXED, FIXED, repair_timeout_seconds=1)

    async def never_finishes(module):
        await asyncio.Event().wait()

    async def scenario():
        previous = sys.gettrace()
        with pytest.raises(RepairTimeout):
            await runtime.acheck_health(name, never_finishes)
        assert sys.gettrace() is previous
        assert await runtime.ainvoke(module_name=name, symbol_name="run") == 42
        assert not provider.requests

    asyncio.run(scenario())


def test_async_health_without_attached_loop_does_not_request_ai(app_factory):
    source = BROKEN.replace("1 / 0", "1")
    runtime, name, path, provider, _ = app_factory(source, FIXED)
    result = runtime.inspect(module_name=name, symbol_name="run", health_check=health_42,
                             retry_safe=True, apply=True)
    assert not result.healed and not provider.requests and path.read_text() == source


def test_sync_retry_can_use_existing_nested_sync_dispatch(app_factory):
    source = ("RUNTIME = None\ndef helper():\n    return 42\n"
              "def run():\n    return RUNTIME.invoke(module_name=__name__, symbol_name='helper') / 0\n")
    fixed = source.replace(" / 0", " / 1")
    runtime, name, path, provider, _ = app_factory(source, fixed)
    importlib.import_module(name).RUNTIME = runtime
    assert runtime.call(module_name=name, symbol_name="run", retry_safe=True) == 42
    assert len(provider.requests) == 1 and path.read_text() == fixed


def test_nested_async_failure_leaves_only_outer_call_to_request_repair(app_factory):
    source = ("async def child():\n    return 1 / 0\n"
              "async def run(runtime):\n"
              "    return await runtime.acall(module_name=__name__, symbol_name='child', retry_safe=True)\n")
    fixed = source.replace("return await runtime.acall(module_name=__name__, symbol_name='child', retry_safe=True)",
                           "return 42")
    runtime, name, path, provider, _ = app_factory(source, fixed)

    async def scenario():
        assert await runtime.acall(module_name=name, symbol_name="run", args=(runtime,), retry_safe=True) == 42

    asyncio.run(scenario())
    assert len(provider.requests) == 1 and provider.requests[0].failure.symbol_name == "run"
    assert path.read_text() == fixed


def test_rebound_target_during_failed_call_preserves_original_exception(app_factory):
    source = ("async def run(started, release):\n"
              "    started.set()\n    await release.wait()\n    return 1 / 0\n")
    runtime, name, _, provider, _ = app_factory(source, source.replace("1 / 0", "42"))
    module = importlib.import_module(name)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(runtime.acall(module_name=name, symbol_name="run",
                                                args=(started, release), retry_safe=True))
        await started.wait()
        module.run = int  # Callable but has no Python __code__.
        release.set()
        with pytest.raises(ZeroDivisionError):
            await task

    asyncio.run(scenario())
    assert not provider.requests


def test_async_commit_failure_restores_original_code(app_factory):
    runtime, name, path, provider, _ = app_factory()
    module = importlib.import_module(name)
    original_code = module.run.__code__
    actual = runtime.core

    class FailingCommit:
        def __getattr__(self, attr):
            return getattr(actual, attr)

        def commit(self, *args):
            raise OSError("commit failed")

    runtime.core = FailingCommit()

    async def scenario():
        with pytest.raises(ZeroDivisionError):
            await runtime.acall(module_name=name, symbol_name="run", retry_safe=True)

    asyncio.run(scenario())
    assert runtime.last_result.status == "rolled_back"
    assert path.read_text() == BROKEN and module.run.__code__ is original_code
    assert len(provider.requests) == 1


@pytest.mark.parametrize("options", [
    {"max_auto_repairs_per_hour": 0}, {"max_auto_repairs_per_hour": True},
    {"repair_failure_cooldown_seconds": -1}, {"repair_failure_cooldown_seconds": float("nan")},
    {"repair_failure_cooldown_seconds": float("inf")}, {"repair_failure_cooldown_seconds": True},
])
def test_invalid_automatic_repair_limits_fail_early(tmp_path, options):
    with pytest.raises(ConfigurationError):
        SelfHealConfig(tmp_path, **options)


def test_deadline_after_coroutine_creation_closes_unawaited_value(app_factory, monkeypatch):
    from selfheal.timing import RepairDeadline
    runtime, _, _, _, _ = app_factory()
    created = []

    async def value():
        return 42

    def callback():
        coroutine = value()
        created.append(coroutine)
        return coroutine

    with RepairDeadline(runtime.config) as budget:
        def expire_after_return(fn, stage):
            fn()
            raise RepairTimeout("deadline expired after callback return")

        monkeypatch.setattr(budget, "run", expire_after_return)
        with pytest.raises(RepairTimeout):
            runtime._guard(callback, "live health checks", allow_async=True)
    assert len(created) == 1 and inspect.getcoroutinestate(created[0]) == inspect.CORO_CLOSED
