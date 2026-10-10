"""Developer-run async agent lifecycle and misuse regressions; no provider I/O."""
from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
import uuid

import pytest

from selfheal import (AgentConfig, HealthCheckResult, HealthReport, LiveTarget,
                      SelfHealAgent, SelfHealConfig, SelfHealRuntime)
from selfheal.providers import AIProvider


ASYNC_SOURCE = ("import asyncio\nasync def run(started=None, release=None):\n"
                "    if started is not None:\n        started.set()\n        await release.wait()\n"
                "    return 42\n")


class NoProvider(AIProvider):
    def __init__(self):
        self.requests = []

    def propose_repair(self, request):
        self.requests.append(request)
        raise AssertionError("These cases must not request an AI repair")


@pytest.fixture
def agent_factory(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    names = []

    def create(source=ASYNC_SOURCE, check=None, *, run_on_start=False):
        name = "async_agent_" + uuid.uuid4().hex
        names.append(name)
        (tmp_path / (name + ".py")).write_text(source, encoding="utf-8")
        module = importlib.import_module(name)
        if check is None:
            if inspect.iscoroutinefunction(module.run):
                async def check(candidate):
                    return HealthReport((HealthCheckResult("result", await candidate.run() == 42),))
            else:
                def check(candidate):
                    return HealthReport((HealthCheckResult("result", candidate.run() == 42),))
        provider = NoProvider()
        runtime = SelfHealRuntime(SelfHealConfig(tmp_path, run_tests=False, hard_timeout=False,
                                                 repair_timeout_seconds=15),
                                  provider=provider, event_sink=lambda event: None)
        target = LiveTarget("sample", name, "run", check, retry_safe=True)
        agent = SelfHealAgent(runtime, (target,), config=AgentConfig(run_on_start=run_on_start))
        return agent, module, provider

    yield create
    for name in names:
        sys.modules.pop(name, None)


def test_sync_call_to_coroutine_rejects_without_queuing_ai(agent_factory):
    agent, _, provider = agent_factory()
    with pytest.raises(TypeError, match="async"):
        agent.call("sample")
    assert agent.status()["pending"] == [] and agent._notifications == {}
    assert not provider.requests


def test_sync_call_on_event_loop_rejects_without_queuing_ai(agent_factory):
    agent, _, provider = agent_factory()

    async def scenario():
        with pytest.raises(RuntimeError, match="agent.acall"):
            agent.call("sample")
        assert agent.status()["pending"] == [] and agent._notifications == {}

    asyncio.run(scenario())
    assert not provider.requests


def test_async_call_to_sync_target_rejects_without_queuing_ai(agent_factory):
    agent, _, provider = agent_factory("def run():\n    return 42\n")

    async def scenario():
        with pytest.raises(TypeError, match="coroutine target"):
            await agent.acall("sample")
        assert agent.status()["pending"] == [] and agent._notifications == {}

    asyncio.run(scenario())
    assert not provider.requests


def test_sync_binding_and_lifecycle_preserve_existing_api(agent_factory):
    agent, _, provider = agent_factory("def run():\n    return 42\n")
    protected = agent.bind("sample")
    assert not inspect.iscoroutinefunction(protected) and protected() == 42
    agent.start()
    assert agent.stop(timeout=5)
    assert not provider.requests


def test_cancelled_binding_does_not_notify_or_request_repair(agent_factory):
    agent, _, provider = agent_factory()
    protected = agent.bind("sample")
    assert inspect.iscoroutinefunction(protected)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        task = asyncio.create_task(protected(started, release))
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.status()["pending"] == [] and agent._notifications == {}

    asyncio.run(scenario())
    assert not provider.requests


def test_health_callback_cannot_reenter_agent_and_queue_recursive_work(agent_factory):
    agent, module, provider = agent_factory()

    async def recursive_health(candidate):
        await agent.acall("sample")
        return HealthReport((HealthCheckResult("unreachable", True),))

    async def scenario():
        report = await agent.runtime.acheck_health(module.__name__, recursive_health)
        assert not report.ok and "reenter" in report.describe()
        assert agent.status()["pending"] == [] and agent._notifications == {}

    asyncio.run(scenario())
    assert not provider.requests


def test_starting_async_agent_without_loop_fails_before_worker_starts(agent_factory):
    agent, _, provider = agent_factory(run_on_start=True)
    with pytest.raises(RuntimeError, match="running application loop"):
        agent.start()
    assert agent._thread is None and not provider.requests


def test_sync_stop_on_loop_requires_astop_without_stopping_early(agent_factory):
    agent, _, provider = agent_factory()

    async def scenario():
        async with agent:
            with pytest.raises(RuntimeError, match="agent.astop"):
                agent.stop()
            assert not agent._stop.is_set()
        assert not agent.status()["running"]

    asyncio.run(scenario())
    assert not provider.requests


def test_cancelled_astop_keeps_loop_alive_until_running_health_finishes(agent_factory):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        owning_loop = asyncio.get_running_loop()

        async def health(module):
            assert asyncio.get_running_loop() is owning_loop
            started.set()
            await release.wait()
            return HealthReport((HealthCheckResult("result", await module.run() == 42),))

        agent, _, provider = agent_factory(check=health, run_on_start=True)
        stopper = None
        agent.start()
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert agent.runtime._async_loop is owning_loop
            stopper = asyncio.create_task(agent.astop())
            await asyncio.sleep(0.03)
            stopper.cancel()
            await asyncio.sleep(0.03)
            stopper.cancel()
            await asyncio.sleep(0.03)
            assert not stopper.done() and agent.status()["running"]
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stopper, 5)
            assert not agent.status()["running"] and not provider.requests
        finally:
            release.set()
            if stopper is not None:
                await asyncio.gather(stopper, return_exceptions=True)
            await agent.astop()

    asyncio.run(scenario())


def test_astop_timeout_reports_active_worker_then_can_finish(agent_factory):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def health(module):
            started.set()
            await release.wait()
            return HealthReport((HealthCheckResult("result", True),))

        agent, _, provider = agent_factory(check=health, run_on_start=True)
        agent.start()
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert await agent.astop(timeout=0) is False
            assert agent.status()["running"]
            release.set()
            assert await agent.astop() is True
            assert not provider.requests
        finally:
            release.set()
            await agent.astop()

    asyncio.run(scenario())


def test_health_callback_cannot_stop_its_own_agent_worker(agent_factory):
    agent, module, provider = agent_factory()

    async def health(candidate):
        await agent.astop()
        return HealthReport((HealthCheckResult("unreachable", True),))

    async def scenario():
        report = await agent.runtime.acheck_health(module.__name__, health)
        assert not report.ok and "reenter" in report.describe()
        assert not agent._stop.is_set()

    asyncio.run(scenario())
    assert not provider.requests


def test_protected_coroutine_cannot_stop_agent_from_inherited_worker_context(agent_factory):
    source = "import asyncio\nasync def run(agent):\n    return await asyncio.to_thread(agent.stop)\n"
    agent, module, provider = agent_factory(source)

    async def scenario():
        with pytest.raises(RuntimeError, match="protected coroutine"):
            await agent.runtime.ainvoke(module_name=module.__name__, symbol_name="run", args=(agent,))
        assert not agent._stop.is_set()

    asyncio.run(scenario())
    assert not provider.requests
