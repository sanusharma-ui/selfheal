# Repair timing and isolated manual calculator

One repair gets one monotonic deadline. Stages consume the remaining time instead
of resetting a timeout for each provider, verification command or retry. The default
is 180 seconds. The original application call, before an exception occurs, is not
part of a repair and is not timed by this SDK. An explicit inspection includes
baseline checks. Each background target scan shares its budget with any repair
that follows; standalone `runtime.check_health()` also gets a bounded budget.

## Developer control

Put these in `.env` in the directory from which you start the application:

```dotenv
SELFHEAL_REPAIR_TIMEOUT_SECONDS=180
SELFHEAL_TIMEOUT_CLEANUP_SECONDS=5
SELFHEAL_HARD_TIMEOUT=true
```

Existing process variables beat `.env`. Explicit `SelfHealConfig` constructor
values or `[runtime]` TOML values beat environment values. Settings are read at
configuration creation; edit `.env` and recreate/restart the runtime to change
them. Invalid values fail with `ConfigurationError`, without echoing their content.
The total accepts finite numbers from 1 through 3600 seconds; cleanup accepts
0.1 through 60 seconds; the hard-timeout switch accepts only true/false.

Cleanup is **inside** the total: reserve = min(configured cleanup, 10% of total).
With 180s and 5s, work stops at 175s and recovery has up to the 180s hard deadline.
With 10s and 5s, work has 9s and recovery has 1s. Watchdog startup, lock waiting,
provider retries/fallback, snapshot copying, native verification, developer review,
candidate checks, activation and live confirmation consume the same budget.
Human approval also consumes it: approval after expiry cannot activate a patch.
Increase the total when using interactive review; no stage silently renews it.

Provider socket timeouts are clipped to the remaining time, including body reads
and retry backoff. The transport's existing per-request timeout remains a ceiling.
Rust receives the remaining budget; copying, py_compile and pytest share it.
The final native verification receives only what remains after earlier stages.
Direct `_core` methods accept optional `timeout_seconds`, defaulting to 120s for
the complete verification/application invocation, rather than 120s per command.
CLI `--timeout` is still a client inactivity timeout, not server cancellation.

## Timeout behavior

Candidate checks execute in a fresh Python worker using a copied module/function
view and serializable application state. Windows workers establish kill-on-close
Job objects before user code; POSIX workers own process groups. Worker descendants
are stopped on timeout; the enabled independent watchdog also cleans up a tracked
health worker when its owning application exits. Snapshot support
uses separately installed `cloudpickle`; no package installation happens at runtime.
The worker uses the same Python interpreter/version as the owning application.

Locks, sockets, database clients and other nonserializable globals may prevent a
snapshot. Activation is refused with an actionable error; there is no in-process
candidate fallback. Keep health targets/state serializable and checks pure.
The snapshot is local trusted data, never an externally supplied pickle. Imported
application modules are serialized by value; imports executed inside callbacks
still behave like ordinary imports. This is not a filesystem/network sandbox.
Candidate checks cannot prove behavior against every live service or future state.

Baseline/post-activation checks and the safe retry retain actual live objects.
Opcode tracing interrupts Python loops in the repairing thread, then normal
rollback restores source and function code. The previous trace hook is restored
after each guard, but debugger tracing is temporarily replaced during guarded
callbacks. Arbitrary native calls cannot safely be interrupted or detached.

Async health checks and retries run on the application's owning event loop.
They use cooperative task timeouts; the worker never leaves its opcode trace
installed across an event-loop await. An async callback that blocks the loop,
runs a CPU loop or suppresses cancellation requires the hard watchdog to bound
the transaction. With the watchdog disabled, such callbacks can prevent
completion. Caller cancellation waits for callback cleanup and rollback before
releasing dispatch admission. See [async integration](ASYNC.md).

With `SELFHEAL_HARD_TIMEOUT=true` (default), an independent process watches the
hard deadline. If a native call or cleanup cannot finish, it terminates **the
owning application process**, not just a thread. Windows reports exit 124;
POSIX uses SIGKILL. Restart must wait for watchdog source recovery to finish;
configure an external supervisor accordingly, or inspect the snapshot and restart
manually. An immediate unattended restart can import an unconfirmed candidate
before recovery completes and is not supported. The watchdog
confirms owner exit before restoring a retained snapshot; restoration requires
both snapshot and candidate hashes to match, and never intentionally overwrites
a developer's external edit. OS scheduling and process teardown are not a
real-time guarantee; recovery I/O after fail-stop may take additional time.

Soft timeout returns `RepairResult(status="timed_out")`; after activation it first
attempts rollback. `runtime.call()` preserves the original exception and attaches
the repair result. Failed restoration disables further healing. Timeout does not
undo DB writes, emails, mutations in shared state or other external effects.

Set `SELFHEAL_HARD_TIMEOUT=false` only when you accept that a blocking native
callback may exceed the total indefinitely. Python loop interruption and worker
timeouts still operate. A watchdog cannot restart the app or reconcile arbitrary
forced kills. Live session files may remain after fail-stop; confirm app exit
before removing one. General durable crash recovery remains separate work.

## Commands for YOU to run

No test, build, dependency installation, calculator launch or provider request was
executed while implementing this change. The Python/native interface changed:
rebuild the editable native extension and install the added dependency yourself,
from Developer PowerShell with your existing Rust/MSVC toolchain:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The standalone fixture is `scripts/run_timing_calculator.py`. Every run creates
a disposable calculator under ignored `.selfheal/manual-timing/`; the SDK never
imports this script. It starts with wrong subtraction (`8 - 3` gives 11) and
unguarded zero division. Default execution uses a deterministic offline provider
and your configured `.env` deadline, making no API call:

```powershell
.\.venv\Scripts\python.exe scripts/run_timing_calculator.py
```

One command exercises all five controlled cases with an explicit 10s override:

```powershell
.\.venv\Scripts\python.exe scripts/run_timing_calculator.py --scenario all --timeout 10
```

Expected final output is `MANUAL CALCULATOR: PASS`, with each case reporting PASS:

| Case | Expected behavior |
| --- | --- |
| heal | Visible diff; same function reference returns 5 and safe zero division. |
| candidate-timeout | Infinite candidate loop stopped in its worker; original calculator unchanged. |
| retry-timeout | Live Python loop interrupted; source/function code rolled back; original exception retained. |
| global-timeout | Generation and review individually fit the total, but together exhaust the one budget; no apply. |
| hard-timeout | Only an isolated calculator child blocks in native sleep; watchdog stops it and restores source. The fixture runner survives. |

For the all-case command to use `.env` instead, omit `--timeout`. Timeouts spend
most of that configured budget; a large total makes intentional-hang scenarios
take correspondingly longer. `--scenario` selects one case. A real provider check
is separately opt-in, uses your own key, and may incur provider account usage:

```powershell
.\.venv\Scripts\python.exe scripts/run_timing_calculator.py --scenario heal --provider auto
```

The regression suite is also supplied for maintainer execution after rebuild:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_timing.py --basetemp=.selfheal/timing-tests -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest --basetemp=.selfheal/full-tests -p no:cacheprovider
$env:PYO3_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
cargo test
```

These commands have not been run. Successful source review is not proof of
Windows Job behavior, native build compatibility or production readiness.

Implementation references: [cloudpickle serialization](https://github.com/cloudpipe/cloudpickle),
[Python tracing](https://docs.python.org/3/library/sys.html#sys.settrace),
[Windows Job objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects).
