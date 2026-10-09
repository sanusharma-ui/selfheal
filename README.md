# SelfHeal Runtime

SelfHeal Runtime is a **Rust-first, Python-first-support self-healing application runtime**.

## V0.3: an embeddable background heal bot

Install SelfHeal in your Python app, register code and developer-owned health
checks, and run `SelfHealAgent` alongside it. The agent performs periodic checks,
queues inspections on application failures, and can apply verified AI patches
without restarting the owning process. Wrong-result bugs can be detected even
when no exception is raised. Scheduled improvements require configured instructions.

Every candidate diff, verification result, snapshot, activation health report
and rollback is visible through `TerminalReporter`. AI transactions are bounded
by an hourly budget, failure backoff and suspension. Defaults produce previews;
scheduled mutation is opt-in. An explicit terminal `heal` command authorizes a
verified live patch.

```powershell
# Terminal 1: credentials must be configured in this app process.
$env:GROQ_API_KEY = "your-groq-key"
$env:GROQ_MODEL = "your-enabled-json-capable-model"
.\.venv\Scripts\python.exe examples/live_calculator.py

# Terminal 2, same working directory: inspect and patch the running app.
.\.venv\Scripts\python.exe -m selfheal heal --target calculator
.\.venv\Scripts\python.exe -m selfheal status
.\.venv\Scripts\python.exe -m selfheal improve "Make invalid-expression errors clearer" --target calculator
```

Open `http://127.0.0.1:8765` yourself and calculate again after the repair. The
calculator is a separate single-file manual test app, not the product. For
scheduled automatic healing, start it with `--interval 60 --auto-apply`.

See [developer integration](docs/AGENT_INTEGRATION.md),
[reusable configuration](selfheal.example.toml), and
[manual live testing](MANUAL_TESTING.md). The installable Python package and
console entry point are configured; PyPI publication and the future Node/npm
adapter are separate release work. Only integrated Python apps are supported now.

## V0.2: OpenAI, Groq and reviewed live repair

The Python SDK now supports OpenAI and Groq through Chat Completions, plus
custom HTTPS OpenAI-compatible endpoints. An OpenAI key is **not required** when
using Groq. Source mutation, verification, snapshots and rollback remain in Rust.

### Manual testing file

Run [scripts/manual_test.py](scripts/manual_test.py) yourself after building:

```powershell
# Offline: inspect the diff and enter y to approve.
.\.venv\Scripts\python.exe scripts/manual_test.py
# Real Groq request (set GROQ_API_KEY and GROQ_MODEL first).
.\.venv\Scripts\python.exe scripts/manual_test.py --provider groq
# Controlled failed retry: verify snapshot restoration without an API call.
.\.venv\Scripts\python.exe scripts/manual_test.py --scenario rollback --auto-apply
```

Expected: `HEALING CHECK: PASS` or `ROLLBACK CHECK: PASS`, the same process ID
before/after, and a successful next healthy call. Declining approval leaves the
original exception and source intact and prints `NOT COMPLETED`. All candidate
code is created in a temporary workspace, which is removed on exit.

See [MANUAL_TESTING.md](MANUAL_TESTING.md) for Windows build prerequisites,
setup commands and expected results.

### Quick start with Groq

Install the project using the development instructions below. Windows requires
the Rust MSVC toolchain and Visual Studio C++ Build Tools with a Windows SDK.
Set process environment variables (the package does not automatically load `.env`):

```powershell
$env:GROQ_API_KEY = "your-groq-key"
$env:GROQ_MODEL = "openai/gpt-oss-120b"
.\.venv\Scripts\python.exe examples/demo_app/run_live_demo.py
```

Use a model available to your account that supports JSON object output and
`max_completion_tokens`. Model IDs are explicit configuration, not fixed in the
library. For OpenAI, set `OPENAI_API_KEY` and `OPENAI_MODEL` instead.

The demo creates disposable Python code and regression tests, then shows the
candidate diff and verification result. Type `y` to activate it. It retries and
makes another call in the **same process**. The repository's calculator is untouched.
The printed audit journal belongs to the temporary demo workspace.

Offline demonstration (no API key or network needed):

```powershell
.\.venv\Scripts\python.exe examples/demo_app/run_live_demo.py --mock --auto-apply
```

### Provider selection and fallback

```python
from selfheal.providers import provider_from_env, GroqProvider, OpenAIProvider

provider = provider_from_env()  # default priority: groq,openai; skips absent keys
provider = provider_from_env(priority=("openai", "groq"), fallback=True)
# Or explicitly select one provider:
provider = GroqProvider(api_key="...", model="your-model", timeout=30, max_retries=1)
```

`SELFHEAL_PROVIDER_PRIORITY` overrides the default order. A present key requires
its matching model variable. Without credentials initialization fails clearly.
Fallback is **off by default**; enabling it allows the context to be sent to the
next configured provider on authentication, connection, quota or service failures.
Invalid responses and a provider declining to repair do not trigger fallback.
There are at most three attempts per provider (`max_retries` range 0–2), with
bounded backoff. `timeout` is a socket-operation timeout, not a whole-transaction
deadline. No external provider SDK is required.

### Review and automatic activation

```python
from pathlib import Path
from selfheal import SelfHealConfig, SelfHealRuntime

def review(candidate):
    print(candidate.diff)
    print(candidate.explanation)
    print(candidate.verification_details)
    return input("Apply this repair? [y/N] ").strip().lower() == "y"

runtime = SelfHealRuntime(
    SelfHealConfig(project_root=Path(".")),  # tests on, auto_apply off
    reviewer=review,  # provider selected from available environment credentials
)
result = runtime.call(
    module_name="calculator", symbol_name="divide", args=(10, 0),
    retry_safe=True,  # developer asserts this operation is safe to repeat
)
```

The callback receives an immutable `RepairReview` and must return exactly `True`.
Without a callback, the original exception is re-raised and the candidate is
available through `runtime.last_result.review`; no live source is changed.
`last_result` also exposes healing success/failure. Provider, verification and
review errors preserve the original application exception, with a safe status note.

Set `auto_apply=True` to explicitly enable automatic activation. Activation still
requires `retry_safe=True` on each call and `retry_after_repair=True`. **Migration
from V0.1:** unmarked calls and disabled retries no longer mutate source or return
a misleading successful `None`. Only a successful retry commits a repair.

The exact source is checked after provider generation, after review, and again
inside Rust around verification. A changed source discards the candidate. A
second verification occurs before activation. Snapshot IDs cannot escape storage
or overwrite a prior snapshot. Approval/rejection is recorded in the audit journal.

### Limits and privacy

- Strict local validation rejects wrong targets, extra/duplicate fields, non-string
  fields, oversized replacements, truncated responses and tool/refusal outputs.
  JSON mode requests valid JSON; **the local validator enforces the repair schema**.
- Defaults: 64,000 bytes for the serialized request, 4,096 completion tokens,
  128,000 bytes for replacement source. Oversized source/context is rejected,
  never truncated into a partial whole-file replacement.
- Argument representations are omitted from built-in provider requests. Known
  secret environment values, common key patterns and credential assignments are
  redacted from exception context. A detected secret in source blocks the request
  rather than risking a redacted placeholder being written into live code.
- Redaction is best effort, not a guarantee for arbitrary personal data. Built-in
  providers send the failed module's source and sanitized exception context;
  custom providers are trusted application code and receive the original request.
- A temporary verification workspace is **not an OS security sandbox**. Tests and
  module activation execute with the application's permissions. Use trusted,
  disposable development environments. Test subprocesses do not yet have deadlines.
- Exception repair remains synchronous and limited to one Python module/file. The
  V0.3 agent also schedules background inspection. Calls through
  one runtime instance are serialized; direct calls, other instances and workers
  are not coordinated. Existing imported function references and class instances
  are not rewritten. Reload does not restore arbitrary module state.
- Source rollback does not undo database writes, network requests or other effects.
  Only mark operations `retry_safe=True` when repetition is safe. A rollback/reload
  failure disables further healing in that instance and requires a restart.

API references: [OpenAI JSON output](https://developers.openai.com/api/docs/guides/structured-outputs),
[Groq compatibility](https://console.groq.com/docs/openai),
[Groq JSON output](https://console.groq.com/docs/structured-outputs),
[Groq model catalog](https://console.groq.com/docs/models).

The project is not intended to be another AI coding assistant. Its purpose is to become infrastructure that can sit beside a running application and perform a controlled repair loop:

```text
Running Python Application
        |
        v
Failure is raised
        |
        v
Python SDK captures runtime context
        |
        v
Developer-selected AI provider proposes a repair
        |
        v
Rust Core
  - policy checks
  - isolated verification
  - snapshot
  - controlled source replacement
  - audit journal
        |
        v
Python SDK reloads the affected module
        |
        v
Failed operation is retried
     /       \
 success    failure
    |          |
   keep     Rust rollback
```

## What makes this different from an AI coding assistant?

A coding assistant normally follows this model:

```text
Developer -> prompt/error -> AI -> code suggestion -> developer applies it
```

SelfHeal follows this model:

```text
Application -> failure -> runtime -> AI candidate -> verifier
            -> controlled activation -> health check -> keep/rollback
```

AI generates a **candidate repair**. It does not get direct authority over the application.

The runtime decides whether that repair can be verified and activated.

---

# Foundation decisions

## Core language

**Rust**

Rust owns the language-neutral, safety-critical mechanics:

- project-root policy enforcement
- candidate verification orchestration
- temporary verification workspace
- source snapshots
- controlled source replacement
- rollback
- audit journal
- repair receipts
- repair IDs
- future process supervision
- future filesystem monitoring
- future sandbox orchestration

## First supported application language

**Python**

The Python SDK owns Python-specific behavior:

- exception capture
- traceback extraction
- Python source/module discovery
- dynamic symbol resolution
- `importlib.reload`
- developer-facing runtime API
- AI provider adapters

## AI provider

Provider-independent.

The framework does not require one vendor. A provider implements:

```python
class AIProvider:
    def propose_repair(self, request: RepairRequest) -> RepairProposal | None:
        ...
```

Future adapters can target cloud APIs, local models, enterprise models or custom agents.

---

# Architecture

```text
                           PYTHON APPLICATION
                                  |
                                  |
                     runtime.call(module, symbol)
                                  |
                                  v
                  +-------------------------------+
                  |          Python SDK           |
                  |-------------------------------|
                  | Exception Observer            |
                  | Failure Context Builder       |
                  | Source / Module Discovery     |
                  | AI Provider Interface         |
                  | Dynamic Symbol Resolver       |
                  | Python Hot Reload Adapter     |
                  +---------------+---------------+
                                  |
                      structured repair proposal
                                  |
                                  v
                  +-------------------------------+
                  |           Rust Core           |
                  |-------------------------------|
                  | Policy Engine                 |
                  | Verification Orchestrator     |
                  | Temporary Workspace           |
                  | Python Compile Invocation     |
                  | Optional Test Invocation      |
                  | Snapshot Manager              |
                  | Controlled Patch Manager      |
                  | Rollback Manager              |
                  | Audit Journal                 |
                  +---------------+---------------+
                                  |
                         verified source update
                                  |
                                  v
                  +-------------------------------+
                  |          Python SDK           |
                  |-------------------------------|
                  | importlib.invalidate_caches   |
                  | importlib.reload(module)      |
                  | resolve current symbol        |
                  | retry failed operation        |
                  +---------------+---------------+
                                  |
                         +--------+--------+
                         |                 |
                      success           failure
                         |                 |
                        keep         Rust rollback
```

---

# Future multi-language architecture

The core is deliberately not implemented as Python business logic.

```text
                         +------------------+
                         |    Rust Core     |
                         +--------+---------+
                                  |
            +---------------------+----------------------+
            |                     |                      |
            v                     v                      v
       Python SDK             Node SDK               Rust SDK
          pip                   npm                   cargo
            |                     |                      |
       Python apps            JS/TS apps             Rust apps
```

Additional language adapters can later define:

```text
capture failure
locate source
validate syntax/compiler
activate repaired code
health check runtime
```

without rewriting the snapshot, policy, audit and rollback core.

---

# Repository structure

```text
selfheal-rust-first-v0.1/
|
|-- Cargo.toml
|-- pyproject.toml
|-- README.md
|-- SECURITY.md
|-- .gitignore
|
|-- src/                         # Rust Core
|   |-- lib.rs                   # PyO3 bridge
|   |-- engine.rs
|   |-- error.rs
|   |-- policy.rs
|   |-- verifier.rs
|   |-- snapshot.rs
|   |-- patch.rs
|   `-- audit.rs
|
|-- python/
|   `-- selfheal/                # Thin Python SDK
|       |-- __init__.py
|       |-- config.py
|       |-- models.py
|       |-- runtime.py
|       |-- observer.py
|       |-- registry.py
|       |-- py.typed
|       |
|       |-- providers/
|       |   |-- __init__.py
|       |   |-- base.py
|       |   `-- mock.py
|       |
|       `-- languages/
|           |-- __init__.py
|           `-- python.py
|
|-- examples/
|   `-- demo_app/
|       |-- calculator.py
|       `-- run_demo.py
|
|-- tests/
|   |-- test_registry.py
|   |-- test_observer.py
|   `-- test_core_integration.py
|
|-- scripts/
|   |-- dev.ps1
|   `-- dev.sh
|
`-- .github/
    `-- workflows/
        `-- ci.yml
```

---

# Responsibility boundary

## Rust Core owns

### Policy

A repair target must remain inside the configured project root.

Future versions can extend this with:

- protected paths
- maximum patch size
- file allowlists
- deny lists
- approval gates
- confidence thresholds

### Verification

The candidate is copied into a temporary project workspace.

The core can then run:

```text
python -m py_compile <candidate.py>
python -m pytest -q
```

before the live source is touched.

### Snapshot

Before applying a verified repair:

```text
project/file.py
        |
        v
.selfheal/snapshots/<proposal-id>/project/file.py
```

### Controlled patch replacement

The replacement is first written to a temporary sibling file and flushed to disk.

The current implementation then swaps the source into place.

### Rollback

If Python cannot activate the repaired module, or the retry fails, the SDK asks Rust to restore the exact snapshot.

### Audit

The Rust core writes JSONL events:

```text
.selfheal/audit.jsonl
```

This gives the healing engine an append-only development journal.

---

# Python SDK owns

## Failure capture

When a protected call raises an exception, Python captures:

- exception type
- message
- traceback
- module
- symbol
- source file
- argument representations

## AI request

That context becomes:

```python
RepairRequest(...)
```

The selected provider returns:

```python
RepairProposal(...)
```

## Activation

After Rust applies the verified source:

```python
importlib.invalidate_caches()
importlib.reload(module)
```

SelfHeal then dynamically resolves the symbol again.

## Retry

The failed operation is retried once only when the caller declares `retry_safe=True`.

If it fails again:

```text
Python retry failure
        |
        v
Rust restore_snapshot(...)
        |
        v
Python reload old module
```

---

# Healing transaction

```text
1. call protected function
2. function fails
3. capture FailureContext
4. ask AI provider for RepairProposal
5. Rust policy validates target
6. Rust copies project into isolated verification workspace
7. Rust replaces candidate file inside workspace
8. Rust invokes Python syntax verification
9. Rust optionally invokes project tests
10. Rust creates snapshot
11. Rust writes candidate to live source
12. Python reloads module
13. Python retries function
14a. retry succeeds -> commit repair
14b. retry fails -> Rust restores snapshot -> Python reloads original
```

---

# Current runtime limitations

This is a foundation, not a production hot-patching platform yet.

V0.2 retains the foundation's narrow transaction:

- one failed call
- one Python module
- one target source file
- one replacement file
- one retry
- one rollback snapshot

The following are planned, not implemented yet:

- multi-file patches
- AST patches
- dependency upgrades
- generated regression tests
- container sandboxing
- process-level blue/green activation
- traffic draining
- concurrency-safe multi-worker patching
- distributed locks
- persistent repair database
- filesystem watchers
- process supervision
- Node/TypeScript adapters
- Go adapters
- Java adapters

---

# Why the Rust/Python split matters

If the entire repair engine were written in Python, adding Node, Go or Java later would either force those ecosystems to depend on a Python runtime or force a major rewrite.

With this architecture:

```text
             shared mechanics
                  Rust
                   |
        +----------+----------+
        |          |          |
     Python      Node       Future
```

language-specific adapters can remain relatively thin.

---

# Build requirements

For development you need:

- Python 3.11+
- Rust toolchain with Cargo
- a C/C++ build environment appropriate for your platform
- pip

The project uses a mixed Rust/Python package built through Maturin and PyO3.

---

# Development install

Create and activate a Python virtual environment.

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Linux/macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Alternatively:

```bash
python -m pip install maturin
maturin develop
```

Run tests:

```bash
pytest
```

Rust-only tests:

```bash
cargo test
```

---

# Demo

The demo intentionally starts with broken code:

```python
def divide(a, b):
    return a / b
```

Run:

```bash
python examples/demo_app/run_demo.py
```

The demo provider recognizes `ZeroDivisionError` and proposes:

```python
def divide(a, b):
    if b == 0:
        return None
    return a / b
```

Expected conceptual sequence:

```text
calculator.divide(10, 0)
        |
ZeroDivisionError
        |
MockAIProvider
        |
Rust verify
        |
Rust snapshot
        |
Rust patch
        |
Python reload
        |
retry
        |
None
```

The demo uses a deterministic mock provider only so the repair pipeline can be tested without an external API.

---

# Developer API

```python
from pathlib import Path

from selfheal import SelfHealConfig, SelfHealRuntime
from selfheal.providers import MockAIProvider

runtime = SelfHealRuntime(
    SelfHealConfig(
        project_root=Path("."),
        run_tests=False,
        auto_apply=True,
    ),
    provider=MockAIProvider(),
)

result = runtime.call(
    module_name="calculator",
    symbol_name="divide",
    args=(10, 0),
    retry_safe=True,
)

print(result)
```

---

# AI provider example shape

```python
from selfheal.providers import AIProvider
from selfheal.models import RepairProposal, RepairRequest


class MyProvider(AIProvider):
    def propose_repair(
        self,
        request: RepairRequest,
    ) -> RepairProposal | None:

        # Call whichever AI system the developer wants.
        # Parse the model output into a controlled RepairProposal.

        return RepairProposal(
            target_file=request.failure.target_file,
            replacement_source="...",
            explanation="...",
        )
```

The provider should not directly write files.

---

# Security model

Self-modifying software needs strict boundaries.

The current foundation follows these rules:

1. AI provider does not directly mutate source files.
2. Candidate paths are constrained to the project root.
3. Candidate code is verified before activation.
4. Original source is snapshotted before replacement.
5. Failed activation/retry triggers rollback.
6. Healing events are logged.
7. `auto_apply=False` is the safe default.

For sensitive production systems, future releases should prefer process-level canary replacement over arbitrary in-process hot swapping.

---

# Roadmap

## V0.1 - Rust-first foundation

- [x] Rust/Python mixed package
- [x] PyO3 bridge
- [x] provider-neutral Python interface
- [x] failure context
- [x] Rust policy engine
- [x] Rust isolated verification workspace
- [x] Rust Python syntax-check orchestration
- [x] optional Rust test orchestration
- [x] Rust snapshots
- [x] Rust patch application
- [x] Rust rollback
- [x] Rust audit journal
- [x] Python module reload
- [x] dynamic symbol resolution
- [x] retry/rollback transaction
- [x] deterministic demo provider

## V0.2 - Real AI providers and review mode

- [x] strict local model response schema validation
- [x] OpenAI-compatible and OpenAI adapters
- [x] Groq adapter and available-key selection
- [x] immutable diff review with explicit approval
- [x] opt-in safe retry and source-change rejection
- [x] context redaction and secret-source rejection
- [ ] Gemini adapter
- [ ] Anthropic adapter
- [ ] Ollama/local adapter
- [x] provider socket timeouts and bounded retries
- [x] explicit fallback providers
- [x] byte context budgets and completion token limits

## V0.3 - Embeddable healing agent

- [x] periodic developer-owned health checks
- [x] wrong-result and command-triggered source inspection
- [x] background exception notifications and dynamic app dispatch
- [x] configured recurring improvements
- [x] bounded AI transaction budget, backoff and target suspension
- [x] function-body policy and retained activation health contracts
- [x] authenticated local control and terminal patch streaming
- [x] pip packaging, console entry point and TOML integration
- [x] separate single-file manual live calculator
- [ ] live verification by the developer
- [ ] PyPI publication and Node/npm adapter

## Further verification work

- [ ] generated regression tests
- [ ] Ruff integration
- [ ] mypy/Pyright integration
- [ ] security scanners
- [ ] patch-size limits
- [ ] protected files
- [x] terminal diff inspection
- [ ] repair scoring

## V0.4 - Runtime healing

- [ ] retry strategies
- [ ] reconnect strategies
- [x] background target failure backoff and suspension
- [ ] production service circuit breakers
- [ ] process supervisor
- [ ] health probes
- [ ] controlled restart activation

## V0.5 - Production activation

- [ ] candidate process launch
- [ ] canary health checks
- [ ] blue/green transition
- [ ] worker coordination
- [ ] distributed lock
- [ ] crash recovery journal

## V0.6 - Additional languages

- [ ] Node.js / TypeScript
- [ ] native Rust apps
- [ ] Go
- [ ] Java

---

# Non-goal

SelfHeal should not become:

```text
error -> LLM -> blindly overwrite production
```

The intended model is:

```text
observe
   |
diagnose
   |
candidate
   |
verify
   |
snapshot
   |
activate
   |
health check
   |
keep or rollback
```

That controlled repair transaction is the core of the project.
