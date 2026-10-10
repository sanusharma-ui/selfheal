# Async calls and live repair

SelfHeal supports module-level `async def` functions on one owning `asyncio`
event loop per runtime. Calls and repaired retries await on that loop, preserving
access to its futures, queues and application clients. Provider generation,
native verification and source transactions run in repair workers. Async
generators, regular generators, class methods and wrapper closures remain
unsupported.

These changes have regression tests supplied for maintainer execution. They have
not been built or tested as part of this implementation; source review is not
evidence of runtime or production readiness.

## Embed the background agent

Example application code in `myapp/pricing.py`:

```python
async def quote(price, discount):
    return price * (1 - discount)
```

Put the health contract in trusted integration code outside the editable
functions:

```python
import asyncio
from pathlib import Path
from selfheal import (
    AgentConfig, HealthCheckResult, HealthReport, LiveTarget,
    SelfHealAgent, SelfHealConfig, SelfHealRuntime,
)


async def pricing_health(module):
    actual = await module.quote(100, 0.10)
    return HealthReport((
        HealthCheckResult("discount result", actual == 90,
                          f"expected 90, got {actual!r}"),
    ))


async def main():
    runtime = SelfHealRuntime(SelfHealConfig(project_root=Path(".")))
    target = LiveTarget(
        name="pricing", module_name="myapp.pricing", symbol_name="quote",
        health_check=pricing_health, editable_symbols=("quote",), retry_safe=True,
        instructions="Preserve pricing contracts and reject invalid discounts.",
    )
    agent = SelfHealAgent(runtime, (target,), config=AgentConfig(
        interval_seconds=60, auto_apply=False, max_attempts_per_hour=6,
    ))
    quote = agent.bind("pricing")  # an async callable for an async target
    async with agent:
        print(await quote(100, 0.10))
        # In a real app, await its entire service lifetime inside this context.


if __name__ == "__main__":
    asyncio.run(main())
```

`async with agent` attaches the current loop, starts the background worker and
awaits graceful shutdown. For an existing service, enter it in the service's
async lifespan and exit before the event loop closes. Use `await agent.astop()`
for explicit shutdown. Keep the owning loop running until accepted transactions
finish. Do not block it with the synchronous `agent.stop()` method.

`await agent.acall("pricing", 100, 0.10)` has the same behavior as the bound
function: it preserves the application's exception and queues an inspection.
The failing request is not automatically replayed; future calls use a committed
repair. Cancellation of an ordinary application call does not queue AI work.
Healthy scheduled checks skip the provider unless inspection/improvement was
explicitly enabled. `auto_apply=False` retains the review/preview default;
`AgentConfig(auto_apply=True)` authorizes background activation subject to health
checks and the target's explicit retry safety declaration.

## Runtime APIs

| API | Behavior |
| --- | --- |
| `await runtime.ainvoke(module_name=..., symbol_name=..., args=(), kwargs=None)` | Await current async code under dispatch coordination, with no automatic repair. |
| `await runtime.acall(..., retry_safe=True)` | Capture an async exception, attempt at most one candidate and one authorized retry, preserving the original exception on repair failure. |
| `await runtime.acheck_health(module_name, check)` | Run developer health checks without provider access or source mutation. |
| `await runtime.ainspect(..., health_check=check, apply=False)` | Async entry point for an explicit inspection; arguments match `inspect`. |
| `runtime.attach_async_loop()` | Attach the current running event loop before starting external/background inspection workers. Async entry points also attach automatically. |

An exception-triggered retry is opt-in, including for coroutine functions:

```python
runtime = SelfHealRuntime(SelfHealConfig(Path("."), auto_apply=True))
value = await runtime.acall(
    module_name="myapp.pricing", symbol_name="quote",
    args=(100, 0.10), retry_safe=True,
)
```

`retry_safe=True` is the application's promise that replay is safe. A coroutine
may have performed side effects before raising; `async def` alone does not make
retries safe. Keep mutation APIs behind explicit idempotency contracts.

`ainvoke` and `acall` accept coroutine targets. Synchronous APIs remain available
for synchronous code; use them from a worker thread when integrating such code
into an async service. Blocking repair methods reject use on a running event
loop and identify their async counterpart. A runtime cannot move between event
loops, including separate `asyncio.run(...)` calls: create and shut down one
runtime within its application's loop lifetime.

## Detection and verification

Awaited exceptions become failure context. Wrong results require developer
health contracts: check boundary inputs, invariants, expected output and
regressions, rather than only whether the function returns. Register related
module-level helpers in `editable_symbols` when a repair legitimately spans
several bodies in one module. Imports, signatures, decorators and unrelated
definitions remain protected.

Baseline and activation async health checks run on the attached application
loop. Candidate health checks run with `asyncio.run` in the disposable candidate
process, using a serializable snapshot. Health checks must be pure, bounded,
repeatable, and compatible with that separate process. Live sockets, clients,
locks and event loops cannot be serialized into the candidate snapshot. Define
offline contracts for the computation behind network operations instead.

Health checks call the supplied module directly, for example
`await module.quote(...)`. A repair callback must not re-enter routed runtime
methods or agent bindings: that would wait on the repair's own admission gate.
SelfHeal rejects such async reentry. Nested synchronous dispatch on the same
repair thread retains its existing reentrant behavior. Providers, reviewers and
event callbacks remain synchronous.

The system can attempt repairs for complex failures whose cause lies within the
registered code and whose correctness can be checked. It cannot discover every
logical error, infer missing requirements or guarantee a correct AI candidate.
Maintain project regression tests and developer contracts; model-generated claims
do not substitute for either.

## In-flight work and cancellation

Concurrent routed async calls are tracked across their full awaited lifetime.
During background/explicit inspection's provider generation and initial
verification they can continue. Direct exception-triggered `call`/`acall` repair
holds exclusive admission throughout its transaction, including generation.
When a
transaction needs a source/state snapshot or activation, it stops admitting new
calls and waits for active calls to finish before updating source or function
code. Subsequent calls see committed code; rejected candidates restore the
previous source and code. Existing function objects and module globals are
retained. Direct unwrapped calls and other processes are outside this protection.

Cancellation during ordinary execution propagates without starting repair.
Cancellation after repair begins requests a transaction abort and waits for its
worker to reach a safe checkpoint. An in-progress provider call may finish before
that checkpoint. If code was already activated, coroutine retry cleanup finishes
before rollback releases waiting calls. Repeated caller cancellation does not
interrupt that cleanup. A transaction already committed when cancellation arrives
stays committed; cancellation cannot undo external side effects.

This means cancellation of a repair can take longer than cancellation of an
ordinary task. Use the shared repair deadline to bound supported work. The hard
watchdog can terminate the owning process if a callback blocks or refuses to
unwind. Async callback timeouts are cooperative: disabling the watchdog also
removes the bound for CPU loops and cancellation-resistant coroutines. See
[timing and recovery](TIMING.md) and Python's
[task cancellation semantics](https://docs.python.org/3/library/asyncio-task.html#task-cancellation).

## Keep automatic attempts bounded

Each direct `call`/`acall` attempt produces at most one candidate and one live
retry. A failed retry does not recursively start another repair. Direct automatic
repairs also share a per-runtime limit and failure cooldown:

```python
config = SelfHealConfig(
    project_root=Path("."),
    max_auto_repairs_per_hour=6,
    repair_failure_cooldown_seconds=60,
)
```

The defaults are six automatic repair transactions per hour and a 60-second
failure cooldown. Repeated application failures still reach callers while
provider attempts are suppressed. A zero cooldown disables only that delay;
the hourly limit still applies. Counts live in memory and reset with a new
runtime. Provider HTTP retries remain separately bounded and can issue more
than one HTTP request for a transaction.

The background agent keeps its separate hourly attempt budget, exponential
failure backoff, notification coalescing and suspension after repeated failures.
Explicit `inspect`/`ainspect` and terminal heal/improve requests are developer
actions outside the automatic direct-call budget. These limits do not create a
shared quota across runtimes, processes or API accounts.

## Commands for you to run

After rebuilding/installing the current checkout using the existing
[release instructions](RELEASING.md), run these from the repository root in
PowerShell. They use deterministic offline providers and disposable source
modules, with no live API request:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_async_runtime.py tests/test_async_adapter.py tests/test_async_agent_edges.py tests/test_coordination.py tests/test_runtime.py tests/test_agent.py -q --basetemp=.selfheal/async-tests -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.selfheal/full-tests -p no:cacheprovider
```

The supplied cases cover same-loop retry, async health, preview and safety gates,
candidate rejection, rollback, concurrent call draining, loop ownership, repeated
failure limits, agent lifecycle and cancellation before/during repair. These
commands and expected behaviors are provided for manual validation; no passing
result is claimed here.
