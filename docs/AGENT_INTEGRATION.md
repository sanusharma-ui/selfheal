# Embed a heal bot in your application

SelfHeal is application infrastructure: install a package, register code and
trusted health checks, then let a background caretaker inspect the running app.
The calculator under `examples/` is only a manual test fixture.

## Installation and package boundaries

The Python distribution is `selfheal-runtime`; imports use `selfheal`. From this
checkout, a developer can install it with `python -m pip install -e .` or build a
wheel with `maturin build --release` and install that wheel in another app.
The wheel contains the Rust extension, Python SDK, agent and `selfheal` command.
Publishing to PyPI is a separate release step; this change does not publish it.
Windows source builds need the C++/Rust toolchain described in `MANUAL_TESTING.md`.

Node/npm is a future adapter, **not implemented or published**. It needs its own
JS source discovery, dynamic dispatch, verification and activation adapter, plus
a Rust binding. The shared Rust policy/snapshot/patch/audit responsibilities stay
in place; wrapping Python reload in an npm command would not provide JS healing.
Python modules are the only supported live targets in this release.

## Code integration

Health checks belong to the developer and return a nonempty `HealthReport`.
They must check intended results, not just absence of exceptions. They receive
the current module and must be pure, bounded, safe to repeat, and independent of
editable function bodies. Avoid saved `from module import function` references.

```python
from pathlib import Path
from selfheal import (
    AgentConfig, HealthCheckResult, HealthReport, LiveControlServer, LiveTarget,
    SelfHealAgent, SelfHealConfig, SelfHealRuntime, TerminalReporter,
)

def pricing_health(module):
    try:
        value = module.quote(100, 0.10)
        return HealthReport((HealthCheckResult(
            "ten percent discount", value == 90, f"expected 90, got {value!r}"
        ),))
    except Exception as exc:
        return HealthReport((HealthCheckResult("quote", False, type(exc).__name__),))

runtime = SelfHealRuntime(
    SelfHealConfig(project_root=Path(".")),  # copied-workspace pytest enabled
    event_sink=TerminalReporter(),  # every candidate diff and activation outcome
)
target = LiveTarget(
    name="pricing", module_name="myapp.pricing", symbol_name="quote",
    health_check=pricing_health, editable_symbols=("quote",), retry_safe=True,
    instructions="Preserve the pricing API and discount rules.",
)
agent = SelfHealAgent(runtime, (target,), config=AgentConfig(
    interval_seconds=300,
    auto_apply=True,  # developer explicitly enables scheduled mutation
    max_attempts_per_hour=6,
))
quote = agent.bind("pricing")

with agent, LiveControlServer(runtime, (target,), agent=agent):
    run_your_application(quote=quote)
```

The last line represents your application's existing entry point. Keep these
context managers alive for the application's entire lifetime. Start them once
in the owning process, not once per request or once in every server worker.
The agent installs a terminal reporter if the runtime has no event sink; supply
your own callback to route events into an application's logger instead.

`agent.bind()` returns a callable that resolves the latest code on every call
and participates in the activation lock. Exceptions are preserved for the
caller and queue a background scan; SelfHeal does not silently retry that
original request. Future calls use the committed repair. `runtime.call()` retains
the earlier synchronous exception/retry API for explicitly safe retries.

## TOML configuration

Copy `selfheal.example.toml` into your application as `selfheal.toml`, replace its
module/callback names, then initialize inside your application's entry point:

```python
agent = SelfHealAgent.from_config(Path("selfheal.toml"), event_sink=TerminalReporter())
quote = agent.bind("pricing")
with agent, LiveControlServer(
    agent.runtime, tuple(agent.targets.values()), agent=agent,
):
    run_your_application(quote=quote)
```

`project_root` resolves relative to the TOML file. Modules and callbacks must be
importable by your app. Config files and callback imports are trusted developer
code, not an untrusted user input interface. Credentials are read from process
environment: `GROQ_API_KEY` + `GROQ_MODEL`, or `OPENAI_API_KEY` + `OPENAI_MODEL`.
`provider_from_env()` loads `.env` from the current working directory, preserving
existing process environment variables. Set `SELFHEAL_USE_GROQ=true` for Groq or
`false` for OpenAI; only the selected provider's credentials are required.
Without this boolean, the existing provider priority behavior applies.

## Periodic behavior and improvements

- The default interval is 300 seconds; the first scheduled check waits one interval.
- Passing local checks cost no AI request by default. `scan_when_healthy=true`
  enables periodic AI inspection for bugs outside known checks.
- `agent.bind()` failures and `agent scan` request an inspection even when the
  registered checks pass. Argument values are not sent to built-in providers.
- `auto_apply=false` produces verified previews. `auto_apply=true` allows scheduled
  activation only for targets explicitly marked `retry_safe=true`.
- `improvement_instructions` opts into a specific recurring improvement when
  health checks pass. The agent does not invent new product requirements. Existing
  checks validate regressions; add checks for a requested new behavior yourself.
- The hourly budget counts attempted AI transactions by this agent, including
  previews. Provider HTTP retries can produce multiple requests per transaction.
  Explicit `heal`/`improve` commands are outside the periodic agent's budget.
  Budgets and suspension are in memory and reset with a new agent process.
- Failed attempts back off exponentially. After three consecutive failures by
  default, a target is suspended until `agent reset`. Reset keeps the hourly budget.
  Notifications are coalesced so a burst of exceptions does not become a burst of requests.

## Terminal control

Run commands in the app's working directory, or pass `--session` with the path
printed by the app. `python -m selfheal` works without reinstalling the console
entry point when the editable package is already available.

```powershell
python -m selfheal status
python -m selfheal heal --target pricing
python -m selfheal heal --target pricing --preview
python -m selfheal improve "Make invalid discount errors clearer" --target pricing
python -m selfheal agent pause
python -m selfheal agent resume
python -m selfheal agent scan --target pricing
python -m selfheal agent reset --target pricing
python -m selfheal agent status
```

Explicit `heal`/`improve` commands authorize activation of a fresh verified
candidate. `--preview` leaves source unchanged. Pausing only affects background
work; explicit manual commands remain available. An accepted repair can finish
after the command client disconnects. Check status before repeating it.

The control server binds only `127.0.0.1`. Its random credential lives in the
ignored `.selfheal/live.json` session file; do not expose that file. Browser
origins, redirects, proxies and unregistered target names cannot be used by the
command client to redirect credential-bearing requests. File permissions use
the current OS/user; the file is not a Windows ACL security boundary. A stale
session file after a crash must be removed only after confirming its app exited.

## Live transaction and current limits

```text
periodic health check / observed exception / explicit command
    -> AI inspects registered module + bounded health context
    -> complete candidate diff in terminal
    -> editable-function policy + Rust verification in copied workspace
    -> pause routed calls; recheck original source; final Rust verification
    -> snapshot; write source; reload module in the same PID
    -> run retained developer checks
    -> commit, or restore source + reload on failure
```

Normal routed calls continue during AI generation and initial verification.
They wait during final verification, activation and health checks. This is a
single-process development runtime, not zero-downtime multi-worker deployment.
Verification compiles syntax and optionally runs the project's existing pytest
suite. It does not generate trusted tests from the model. Function policies
freeze signatures, decorators and other module definitions; editable functions
remain privileged code and may still have side effects.

The agent updates one registered Python module per transaction. It does not
attach to arbitrary unintegrated processes, repair native/async functions,
replace existing class instances or change dependencies. Calls must go through
`agent.bind()`, `agent.call()`, `runtime.invoke()` or `runtime.call()` for dispatch
and activation coordination. Multiple processes/other runtimes are unsupported.

Rollback restores source, not external writes or arbitrary in-memory state.
Checks and activated functions run with app permissions and currently have no
in-process execution deadline. Native pytest verification also has no deadline.
Graceful shutdown waits for accepted transactions; a forced process kill does
not provide automatic crash recovery. If restoration fails, healing is disabled
and the app must restart. Use complete health contracts and trusted development
environments; a successful bounded health report cannot prove every bug is fixed.

Future production work includes language adapters/npm, durable crash recovery,
bounded isolated execution, worker coordination and supervised canary activation.
