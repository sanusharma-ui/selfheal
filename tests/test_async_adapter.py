"""Developer-run coroutine activation and isolated health-check regressions."""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
from pathlib import Path
import sys
import uuid

import pytest

from selfheal import HealthCheckResult, HealthReport
from selfheal.languages import PythonRuntimeAdapter
from selfheal.scope import validate_edit_scope


@pytest.fixture
def module_factory(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    names = []

    def create(source):
        name = "async_adapter_" + uuid.uuid4().hex
        path = tmp_path / (name + ".py")
        path.write_text(source, encoding="utf-8")
        names.append(name)
        return importlib.import_module(name), path

    yield create
    for name in names:
        sys.modules.pop(name, None)


def test_coroutine_activation_and_restore_preserve_function_identity(module_factory):
    source = ("import asyncio\nCACHE = {}\n"
              "async def run(value, /, *, offset=1):\n"
              "    await asyncio.sleep(0)\n    return value + offset\n")
    module, path = module_factory(source)
    adapter = PythonRuntimeAdapter()
    saved, cache = module.run, module.CACHE
    defaults, annotations = saved.__kwdefaults__, saved.__annotations__
    replacement = source.replace("value + offset", "value * offset")
    validate_edit_scope(source, replacement, ("run",))
    candidate = adapter.candidate_module(module.__name__, adapter.prepare(module.__name__, replacement, ("run",)))
    assert asyncio.run(candidate.run(3, offset=2)) == 6
    assert asyncio.run(saved(3, offset=2)) == 5
    path.write_text(replacement, encoding="utf-8")
    adapter.activate(module.__name__, ("run",))
    assert module.run is saved and module.CACHE is cache
    assert saved.__kwdefaults__ is defaults and saved.__annotations__ is annotations
    assert asyncio.iscoroutinefunction(saved)
    assert asyncio.run(saved(3, offset=2)) == 6
    path.write_text(source, encoding="utf-8")
    adapter.activate(module.__name__, ("run",))
    assert module.run is saved and asyncio.run(saved(3, offset=2)) == 5


def test_coroutine_candidate_helpers_and_aliases_use_candidate_globals(module_factory):
    source = ("async def run():\n    return 1\nALIAS = run\n"
              "async def helper():\n    return await ALIAS()\n")
    module, _ = module_factory(source)
    adapter = PythonRuntimeAdapter()
    candidate = adapter.candidate_module(
        module.__name__, adapter.prepare(module.__name__, source.replace("return 1", "return 2"), ("run",)))
    assert candidate.run is candidate.ALIAS
    assert asyncio.run(candidate.helper()) == 2
    assert asyncio.run(module.helper()) == 1


def test_existing_coroutine_retains_its_code_during_activation(module_factory):
    source = "async def run(started, release):\n    started.set()\n    await release.wait()\n    return 1\n"
    module, path = module_factory(source)
    saved = module.run
    adapter = PythonRuntimeAdapter()

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        pending = asyncio.create_task(saved(started, release))
        await started.wait()
        try:
            path.write_text(source.replace("return 1", "return 2"), encoding="utf-8")
            adapter.activate(module.__name__, ("run",))
        finally:
            release.set()
        assert await pending == 1
        assert await saved(started, release) == 2
        assert module.run is saved

    asyncio.run(scenario())


@pytest.mark.parametrize("original,replacement", [
    ("def run():\n    return 1\n", "async def run():\n    return 2\n"),
    ("async def run():\n    return 1\n", "def run():\n    return 2\n"),
])
def test_sync_async_kind_changes_fail_both_scope_and_adapter(module_factory, original, replacement):
    module, _ = module_factory(original)
    saved = module.run.__code__
    with pytest.raises(ValueError, match="outside the allowed function bodies"):
        validate_edit_scope(original, replacement, ("run",))
    with pytest.raises(ValueError, match="preserve whether.*async"):
        PythonRuntimeAdapter().prepare(module.__name__, replacement, ("run",))
    assert module.run.__code__ is saved


@pytest.mark.parametrize("original,replacement", [
    ("def run():\n    yield 1\n", "def run():\n    yield 2\n"),
    ("async def run():\n    yield 1\n", "async def run():\n    yield 2\n"),
    ("async def run():\n    return 1\n", "async def run():\n    yield 2\n"),
    ("def run():\n    return 1\n", "def run():\n    yield 2\n"),
])
def test_generator_targets_and_candidates_stay_rejected(module_factory, original, replacement):
    module, _ = module_factory(original)
    with pytest.raises(ValueError, match="[Gg]enerator"):
        PythonRuntimeAdapter().prepare(module.__name__, replacement, ("run",))


@pytest.mark.parametrize("signature", ["renamed, /, *, offset=1", "value, offset=1", "value, /, *, extra, offset=1"])
def test_coroutine_argument_contract_stays_frozen(module_factory, signature):
    source = "async def run(value, /, *, offset=1):\n    return value + offset\n"
    replacement = source.replace("value, /, *, offset=1", signature)
    module, _ = module_factory(source)
    with pytest.raises(ValueError, match="signature|argument names"):
        PythonRuntimeAdapter().prepare(module.__name__, replacement, ("run",))


@pytest.fixture
def health_worker(monkeypatch):
    # The entry point imports process containment from its sibling script.
    path = Path(__file__).parents[1] / "python/selfheal/_health_worker.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("async_health_worker_test", path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    return worker


def test_worker_health_supports_sync_async_and_awaitable_callbacks(health_worker):
    expected = HealthReport((HealthCheckResult("result", True),))

    async def async_health(module):
        await asyncio.sleep(0)
        return expected

    class HealthAwaitable:
        def __await__(self):
            return async_health(None).__await__()

    assert health_worker._health_report(lambda module: expected, None) is expected
    assert health_worker._health_report(async_health, None) is expected
    assert health_worker._health_report(lambda module: HealthAwaitable(), None) is expected
    assert health_worker._health_report(lambda module: asyncio.run(async_health(module)), None) is expected


def test_worker_health_drains_tasks_generators_and_closes_loop(health_worker):
    state = {}

    async def background():
        try:
            await asyncio.Event().wait()
        finally:
            state["task_closed"] = True

    async def values():
        try:
            yield 1
        finally:
            state["generator_closed"] = True

    async def health(module):
        state["loop"] = asyncio.get_running_loop()
        state["task"] = asyncio.create_task(background())
        state["generator"] = values()
        await anext(state["generator"])
        await asyncio.sleep(0)
        return HealthReport((HealthCheckResult("ready", True),))

    assert health_worker._health_report(health, None).ok
    assert state["task_closed"] and state["generator_closed"]
    assert state["task"].cancelled() and state["loop"].is_closed()


@pytest.mark.parametrize("report", [None, True, HealthReport((True,))])
def test_worker_async_health_rejects_invalid_reports(health_worker, report):
    async def health(module):
        await asyncio.sleep(0)
        return report

    with pytest.raises(TypeError, match="must return a HealthReport"):
        health_worker._health_report(health, None)


def test_worker_async_health_preserves_callback_failure(health_worker):
    async def health(module):
        await asyncio.sleep(0)
        raise ValueError("probe failed")

    with pytest.raises(ValueError, match="probe failed"):
        health_worker._health_report(health, None)
