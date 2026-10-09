# SelfHeal Runtime

SelfHeal is a Python SDK that helps an integrated running application repair
registered functions using a developer-configured AI API. It observes exceptions
and developer health checks, requests a minimal patch, verifies it, updates the
running function bodies, and restores the previous source and function code if
activation fails. The application's process stays running.

This is a **beta, single-process SDK**. Register the functions you permit it to
edit, define the behavior your checks must protect, and route calls through the
SDK. SelfHeal cannot attach to an arbitrary process or prove every AI patch correct.
Only Python is supported. The Rust core handles source policy, copied-workspace
verification, snapshots, file replacement and audit records.

## Install and configure your own API

After the release is published, install the SDK into your application's environment:

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

## Operational boundaries

- Supported targets are synchronous module-level Python functions in writable UTF-8
  source under the configured project root. Async/generator functions, native
  callables, wrapper closures, class methods and dependency changes are rejected.
- Register one runtime/agent per owning process. Multi-worker coordination,
  automatic crash recovery after a forced kill and remote-process attachment
  are not implemented. All participating calls need the runtime's dispatch lock.
- Health checks must be pure, bounded and safe to repeat. Candidate checks use a
  separate function/module view with shared application objects; this is not a
  sandbox. Checks and patched functions execute with the app's permissions.
- Rust verification subprocesses have a 120 second deadline per invocation and
  bounded captured diagnostics. Child process trees, in-process health checks
  and HTTP streaming do not have a whole-transaction deadline.
- Rollback restores source and function code. It cannot undo external writes,
  arbitrary shared-state changes or side effects. If restoration fails, healing
  is disabled and the app must restart.
- Source and sanitized failure/health context go to the selected provider. Known
  secrets are redacted or rejected; arbitrary personal data needs the developer's
  own policy. Budget, backoff and suspension bound background AI attempts.

Use only code, accounts and data you are authorized to modify or transmit.
Review [SECURITY.md](SECURITY.md) and the AI provider's applicable terms before
enabling automatic patches. Passing checks is evidence about the registered
contracts, not a guarantee of correctness or legal compliance.

## Development and release

See [integration](docs/AGENT_INTEGRATION.md), [TOML configuration](selfheal.example.toml),
[manual testing](MANUAL_TESTING.md), [release commands](docs/RELEASING.md), and
[changes](CHANGELOG.md). `examples/live_calculator.py` is a deliberately broken
manual fixture; it is not the SDK. The release workflow is manually started by
the maintainer and only produces artifacts. No PyPI upload is automatic.

Original source is [MIT licensed](LICENSE). Bundled dependency terms and retained
copyright notices are in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
