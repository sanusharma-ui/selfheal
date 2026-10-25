# SelfHeal Runtime

> AI-assisted repair for running Python applications, with verification and developer control.

**Python 3.11+** &nbsp;·&nbsp; **Rust core** &nbsp;·&nbsp; **Beta** &nbsp;·&nbsp; **Review by default**

SelfHeal embeds a repair agent in your application. When a registered function
raises an exception or fails a developer-defined health check, it asks your AI
provider for a scoped patch, verifies the candidate, and presents it for review.
Authorized patches update live function bodies without re-running module
initialization. If activation fails, SelfHeal attempts to restore the previous
source and function code.

You choose **what can change, what correctness means, and when a patch may apply**.
Successful live repairs keep the application's process running; an
uninterruptible timeout can stop it for recovery.

[Get started](#install-and-configure-your-own-api) ·
[Embed the agent](#embed-it-in-your-running-app) ·
[Async support](https://github.com/sanusharma-ui/selfheal/blob/main/docs/ASYNC.md) ·
[Terminal controls](#inspect-and-patch-from-the-terminal) ·
[Time budgets](#configure-the-repair-time-budget) ·
[Try the calculator](#try-the-isolated-calculator) ·
[Security](https://github.com/sanusharma-ui/selfheal/blob/main/SECURITY.md)

## What you get

| Capability | How it works |
| --- | --- |
| **Detect exceptions and wrong results** | Routed calls report failures; your health checks define expected behavior. |
| **Keep edits scoped** | Only registered function bodies may change. Imports, signatures and other definitions stay protected. |
| **Verify before activation** | The Rust core checks syntax and optional pytest in a copied workspace; candidate health checks run in a separate process. |
| **See every proposed change** | Terminal diffs, verification results, snapshots and audit records make repairs inspectable. |
| **Update running functions** | Guarded activation updates function code while preserving module initialization and existing function objects. |
| **Await async functions** | Coroutine calls and safe retries use the app's owning event loop; repair workers coordinate activation with in-flight calls. |
| **Control time and API usage** | A shared repair deadline, direct-call limits and cooldown, background budgets, backoff and suspension bound repair activity. |

This is a **single-process Python SDK**. Integrate it into your app, register
editable functions, and route participating calls through its dispatch lock.
It cannot attach to an arbitrary process or prove every AI patch correct.
Synchronous and `async def` functions, directly defined methods and property
accessors are supported in the source implementation. Async apps use
`async with agent` and `await agent.bind("target")(...)`; see the
[async integration guide](https://github.com/sanusharma-ui/selfheal/blob/main/docs/ASYNC.md) for loop ownership and cancellation.

## Install and configure your own API

From a repository checkout, install into your application's Python environment:

```shell
python -m pip install -e ".[dev]"
```

Source installations require Rust and platform C/C++ build prerequisites.
See [Windows setup and manual testing](https://github.com/sanusharma-ui/selfheal/blob/main/MANUAL_TESTING.md) for toolchain guidance.

Once a release is published to PyPI, the package installation command is:

```shell
python -m pip install "selfheal-runtime[verification]"
```

The `verification` extra installs pytest for project regression checks. The base
package is `selfheal-runtime`; the Python import is `selfheal`. Normal CPython
3.11+ is required. Wheels include the native extension; source installations
require Rust and the platform C/C++ build prerequisites.

Create `.env` in your app's working directory:

```dotenv
SELFHEAL_USE_GROQ=true
GROQ_API_KEY=your-groq-key
GROQ_MODEL=your-enabled-json-capable-model
OPENAI_API_KEY=
OPENAI_MODEL=
```

Replace the placeholders with **your own key and enabled model**. Set
`SELFHEAL_USE_GROQ=false` to use `OPENAI_API_KEY` and `OPENAI_MODEL`. Built-in
providers require the selected vendor's key/model and raise `ConfigurationError`
when configuration is missing, invalid or still a known placeholder. No account,
API credit, key or model is bundled. Authentication and model access are checked
by the provider on the first request; the API provider may charge for requests.

`provider_from_env()` loads the working directory's `.env`. Existing process
variables take precedence. Restart after editing credentials or selection. Keep
`.env` out of source control. Validate local configuration without an API request:

```shell
python -m selfheal check-config
```

## Embed it in your running app

Suppose your app's `myapp/pricing.py` defines `quote(price, discount)`. Its expected
result for `quote(100, 0.10)` is `90`. Put the health contract in trusted integration
code, outside the functions the AI may edit:

```python
from pathlib import Path
from selfheal import (
    AgentConfig, HealthCheckResult, HealthReport, LiveControlServer, LiveTarget,
    SelfHealAgent, SelfHealConfig, SelfHealRuntime, TerminalReporter,
)


def pricing_health(module):
    try:
        value = module.quote(100, 0.10)
        return HealthReport((HealthCheckResult("discount result", value == 90),))
    except Exception as exc:
        return HealthReport((HealthCheckResult("quote", False, type(exc).__name__),))


runtime = SelfHealRuntime(
    SelfHealConfig(project_root=Path(".")),  # project pytest verification enabled
    event_sink=TerminalReporter(),
)
target = LiveTarget(
    name="pricing", module_name="myapp.pricing", symbol_name="quote",
    health_check=pricing_health, editable_symbols=("quote",), retry_safe=True,
    instructions="Preserve the pricing API and discount contracts.",
)
agent = SelfHealAgent(runtime, (target,), config=AgentConfig(
    interval_seconds=60, auto_apply=False,
))
quote = agent.bind("pricing")

with agent, LiveControlServer(runtime, (target,), agent=agent):
    run_your_application(quote=quote)  # your existing blocking app entry point
```

Start the agent once in the owning app process. The context managers must stay
alive for the application's lifetime. `quote` resolves the current function on
each call; failures are preserved for the caller and queue a background inspection.
Healthy checks skip AI requests by default. Failing checks catch wrong-result
bugs even when no exception is raised.

`auto_apply=False` produces verified previews. Explicitly set
`AgentConfig(auto_apply=True)` to permit scheduled verified patches. Pure,
nonempty developer health checks and `retry_safe=True` are required for live
activation. For production-shaped apps, maintain regression tests as well as
health checks. `SelfHealConfig(run_tests=False)` is an explicit choice to rely on
syntax and registered health contracts rather than pytest.

## Inspect and patch from the terminal

In another terminal, use the same working directory or pass `--session`:

```shell
python -m selfheal status
python -m selfheal heal --target pricing --preview
python -m selfheal heal --target pricing
python -m selfheal improve "Handle invalid discounts clearly" --target pricing
python -m selfheal agent pause
python -m selfheal agent resume
python -m selfheal agent status
```

`heal` and `improve` without `--preview` authorize a fresh verified live patch.
Diffs, verification, health results, snapshots and rollback are visible in the
app terminal and command client. The local control server is authenticated and
loopback-only; protect its `.selfheal/live.json` credential.

```text
registered health failure / exception / developer request
  -> your AI API proposes a complete candidate module
  -> allowed function bodies and unchanged signatures are checked
  -> Rust verifies syntax and optional pytest in a copied workspace
  -> developer authorization and candidate health checks
  -> source snapshot and guarded source replacement
  -> function code updated without re-running module initialization
  -> running health checks pass: commit; otherwise: restore and report
```

Module globals, existing function objects, defaults, decorators and connections
are retained. Active routed calls finish before activation; subsequent routed
calls use the new code. Calls continue during API generation and initial native
verification, and wait during final verification and activation.

## Deeper checks and method targets

For generated edge cases, workflow invariants, dotted method targets and optional
read-only helper context, see [deeper checks and method targets](https://github.com/sanusharma-ui/selfheal/blob/main/docs/DEEP_HEALING.md).
The optional `selfheal-runtime[exploration]` extra supplies Hypothesis for bounded
property checks. Expected behavior remains developer-owned; generated tests do
not establish that every bug has been found.

Run `python -m selfheal doctor` to inspect SDK/native compatibility without an API
request. A stale editable native extension is rejected before repair starts.

## Provider selection

```python
from selfheal.providers import provider_from_env, GroqProvider, OpenAIProvider

provider = provider_from_env()  # .env boolean selects Groq or OpenAI
provider = GroqProvider(api_key="your-real-key", model="your-enabled-model")
```

An explicit `priority` argument overrides the boolean. If the boolean is absent,
`SELFHEAL_PROVIDER_PRIORITY` controls the default `groq,openai` order and missing
keys are skipped. Cross-provider fallback is opt-in:
`provider_from_env(priority=("groq", "openai"), fallback=True)`. With the boolean
alone, selection stays pinned to that vendor, even if `fallback=True` is passed.

Built-in providers use HTTPS, refuse redirects, bound response/context sizes and
HTTP retries, and validate the JSON proposal. Model output cannot directly write
files or issue shell commands. A custom `AIProvider` can be explicitly injected;
the deterministic mock exists for offline tests and is never an automatic fallback.

## Configure the repair time budget

Set the complete repair budget in your application's working-directory `.env`:

```dotenv
SELFHEAL_REPAIR_TIMEOUT_SECONDS=180
SELFHEAL_TIMEOUT_CLEANUP_SECONDS=5
SELFHEAL_HARD_TIMEOUT=true
```

One deadline covers provider retries, verification, review, activation and live
retry. Each stage uses the time remaining; it does not start a fresh budget.
Cleanup is reserved **inside** the total, capped at 10% of that total.
With the defaults, work has 175 seconds and cleanup has the remaining 5 seconds.

Explicit SDK/TOML settings override process environment variables, which override
`.env`. Recreate the runtime or restart your app after changing settings.

The enabled hard-timeout watchdog can **terminate the owning application** if
native code cannot unwind. A supervisor or maintainer must restart it after
recovery finishes. Disabling it removes that bound on native stalls.
See [timing behavior and recovery](https://github.com/sanusharma-ui/selfheal/blob/main/docs/TIMING.md) for details.

## Operational boundaries

- Targets can be synchronous/coroutine module functions, directly defined instance,
  static/class methods and property accessors in writable UTF-8 project source.
  Class layout, descriptors and signatures stay fixed. Regular/async generators,
  native callables, arbitrary wrapper closures, custom metaclasses, inherited
  target definitions and dependency changes are rejected.
- Register one runtime/agent per owning process. Multi-worker coordination,
  automatic crash recovery after a forced kill and remote-process attachment
  are not implemented. All participating calls need the runtime's dispatch
  coordination; async targets share one owning event loop per runtime.
- Health checks must be pure and safe to repeat. Candidate checks use a disposable
  process and a serializable snapshot of app state. Baseline and activation checks
  remain in-process to preserve live state/identity checks. Neither is a permission
  sandbox; external side effects remain possible.
- Timeouts interrupt guarded Python loops and terminate candidate/verification
  process trees. The hard watchdog can stop the owning app on a native stall.
  Deadlines are not a real-time OS guarantee; recovery I/O can take additional time.
- Rollback restores source and function code. It cannot undo external writes,
  arbitrary shared-state changes or side effects. If restoration fails, healing
  is disabled and the app must restart.
- Source and sanitized failure/health context go to the selected provider. Known
  secrets are redacted or rejected; arbitrary personal data needs the developer's
  own policy. Direct exception repairs have an hourly budget and failure cooldown;
  the agent separately bounds background AI attempts with budgets, backoff and suspension.

Use only code, accounts and data you are authorized to modify or transmit.
Review [SECURITY.md](https://github.com/sanusharma-ui/selfheal/blob/main/SECURITY.md) and the AI provider's applicable terms before
enabling automatic patches. Passing checks is evidence about the registered
contracts, not a guarantee of correctness or legal compliance.

## Try the isolated calculator

After installing the current checkout, run this from the repository root:

```shell
python scripts/run_timing_calculator.py --scenario all --timeout 10
```

The fixture starts with broken subtraction and zero division, then exercises
repair, candidate timeout, live-retry rollback, a shared-budget timeout and a hard
timeout. It uses a deterministic offline provider by default, with **no API
request**. Disposable calculator files stay under ignored `.selfheal/manual-timing/`;
the SDK never imports the fixture.

Expected final output: `MANUAL CALCULATOR: PASS`. This is a maintainer-run
validation command; the current timing changes still require runtime and native
build validation. See [the walkthrough and expected results](https://github.com/sanusharma-ui/selfheal/blob/main/docs/TIMING.md#commands-for-you-to-run).

## Development and release

| Resource | Purpose |
| --- | --- |
| [Integration guide](https://github.com/sanusharma-ui/selfheal/blob/main/docs/AGENT_INTEGRATION.md) | Register targets, define health contracts and embed the background agent. |
| [Async guide](https://github.com/sanusharma-ui/selfheal/blob/main/docs/ASYNC.md) | Coroutine dispatch, live health, cancellation, concurrency and manual regression commands. |
| [TOML template](https://github.com/sanusharma-ui/selfheal/blob/main/selfheal.example.toml) | Configure runtime, scheduling and editable targets. |
| [Manual testing](https://github.com/sanusharma-ui/selfheal/blob/main/MANUAL_TESTING.md) | Set up Windows tooling and walk through the live calculator. |
| [Timing guide](https://github.com/sanusharma-ui/selfheal/blob/main/docs/TIMING.md) | Configure deadlines and exercise the isolated timeout scenarios. |
| [Release guide](https://github.com/sanusharma-ui/selfheal/blob/main/docs/RELEASING.md) | Build and validate distribution artifacts before publication. |
| [Changelog](https://github.com/sanusharma-ui/selfheal/blob/main/CHANGELOG.md) | Review version history. |

`examples/live_calculator.py` is a deliberately broken manual fixture. The release
workflow is manually started by the maintainer and only produces artifacts.
No PyPI upload is automatic.

Original source is [MIT licensed](https://github.com/sanusharma-ui/selfheal/blob/main/LICENSE). Bundled dependency terms and retained
copyright notices are in [THIRD_PARTY_NOTICES.md](https://github.com/sanusharma-ui/selfheal/blob/main/THIRD_PARTY_NOTICES.md).
