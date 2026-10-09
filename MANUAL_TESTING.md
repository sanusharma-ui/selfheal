# Manual testing on Windows

Run all commands from `D:\Projects\selfheal`.

## V0.3 live calculator and background heal bot

These are commands for **you to run**. Release builds, installed-wheel validation,
browser testing and live-provider requests remain maintainer validation steps.

Rebuild the editable package to include native verification, stable-ABI packaging
and the current Python SDK. Run this build yourself:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

`python -m selfheal` is available through the existing editable installation.
The Rust verification implementation and wheel ABI changed for this release;
an older extension does not validate the updated native behavior.

For manual command-based testing, start this in Terminal 1:

```powershell
# Configure your own key/model in .env; SELFHEAL_USE_GROQ=true selects Groq.
.\.venv\Scripts\python.exe -m selfheal check-config
.\.venv\Scripts\python.exe examples/live_calculator.py
```

Use a JSON-output-capable Groq model available to your account, or set
`OPENAI_API_KEY` + `OPENAI_MODEL`. The key must be in Terminal 1's process, not
just the command client's terminal. The app also loads its working directory's
`.env`; process variables take precedence. No credential is embedded in the calculator.

Open `http://127.0.0.1:8765` yourself. This single-file app deliberately begins
with three bugs: `8 - 3` returns `11`, `10 % 3` returns `3`, and `10 / 0` raises.
`6 * 7` is a healthy comparison. The agent waits 300 seconds for its first
scheduled check and defaults to preview mode; pause it during a controlled
manual walkthrough if desired.

Run in Terminal 2, also from the project directory:

```powershell
.\.venv\Scripts\python.exe -m selfheal agent pause
.\.venv\Scripts\python.exe -m selfheal status
.\.venv\Scripts\python.exe -m selfheal heal --target calculator --preview
.\.venv\Scripts\python.exe -m selfheal heal --target calculator
.\.venv\Scripts\python.exe -m selfheal status
```

Expect full unified diffs and staged verification/activation output in both
terminals. A preview never changes live source. A candidate must pass checks in
a separate function view before activation. Running checks must also pass after
activation, or the old source and function code are restored.
AI-generated candidates are not guaranteed to pass. Repeat the arithmetic after
an `applied` result: expected results are `5`, `1`, and safe zero division (`None`).
The page and terminal should show the same PID before/after. No server restart
is requested by the repair. Source changes and snapshots persist on disk.

Try an improvement while keeping the browser open:

```powershell
.\.venv\Scripts\python.exe -m selfheal improve "Make invalid-expression messages clearer while preserving all arithmetic contracts" --target calculator
```

Only `calculate`'s body can change; the server, HTML, checks and integration
remain protected by the function policy. The existing checks cover arithmetic
regressions; assess any new UX behavior yourself.

For automatic periodic repair, stop the app with Ctrl+C, restore a broken
calculator fixture if you already healed it, then start:

```powershell
.\.venv\Scripts\python.exe examples/live_calculator.py --interval 60 --auto-apply
```

The first local health check runs after 60 seconds. Failing checks trigger an AI
request and verified activation automatically. Healthy checks skip AI by default;
add `--scan-healthy` to opt into periodic source inspection even when checks pass.

```powershell
.\.venv\Scripts\python.exe -m selfheal agent status
.\.venv\Scripts\python.exe -m selfheal agent scan --target calculator
.\.venv\Scripts\python.exe -m selfheal agent pause
.\.venv\Scripts\python.exe -m selfheal agent resume
.\.venv\Scripts\python.exe -m selfheal agent reset --target calculator
```

Periodic diffs print in the app terminal. `agent scan` queues work governed by
the agent's current activation policy and budget; `heal` performs a direct live
transaction. Backoff defaults to 60 seconds, AI budget to six transactions/hour,
and target suspension to three consecutive failures. Reset clears suspension
without resetting the AI budget. A disconnected client does not cancel an
accepted repair; inspect status before issuing another.

Snapshots and Rust audit events are under `examples/.selfheal/`. The authenticated
session file is `.selfheal/live.json` in the working directory. Do not share its
credential. If the app crashed, confirm its process exited before removing a
stale session file. Use `--session` for another location. Ports can be configured
with `--port` and `--control-port`.

To repeat the exact broken fixture, make a backup **before** your first repair:

```powershell
New-Item -ItemType Directory -Force examples/.selfheal
Copy-Item -LiteralPath examples/live_calculator.py -Destination examples/.selfheal/calculator-original.py
# After stopping the app, restore that fixture:
Copy-Item -LiteralPath examples/.selfheal/calculator-original.py -Destination examples/live_calculator.py
```

The original V0.2 offline/rollback scenarios below remain available. The added
agent/live regression files can be run with the existing pytest commands; they
were supplied but not run by the coding agent.

## One-time setup

Install Visual Studio 2022 Build Tools with **Desktop development with C++**,
including the MSVC compiler and Windows SDK. Then open **Developer PowerShell
for VS 2022**. The Rust MSVC toolchain and Python 3.11+ are also required.

If `cargo` is not on PATH but the existing toolchain is installed at the following
location, make it available in the current shell:

```powershell
$env:Path = "$env:USERPROFILE\.rustup\toolchains\stable-x86_64-pc-windows-msvc\bin;$env:Path"
cd D:\Projects\selfheal
```

Create a virtual environment if you do not already have `.venv`, then install:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

An existing `.venv` can be reused; skip the first command in that case. The
installation compiles the native Rust extension. `link.exe not found` means the
C++ tools are missing or their developer shell has not been activated.

## 1. Offline repair with review

```powershell
.\.venv\Scripts\python.exe scripts/manual_test.py
```

No API key is needed. Read the displayed diff and verification output, then type
`y`. Expected: `HEALING CHECK: PASS`; a repaired zero-division call returns `None`;
the next call returns `4.0`; the process ID remains the same.

Run it again and answer `n` to check rejection. Expected: original
`ZeroDivisionError` preserved, `NOT COMPLETED`, and no patch-applied event.

## 2. Real Groq repair

```powershell
$env:GROQ_API_KEY = "your-groq-key"
$env:GROQ_MODEL = "openai/gpt-oss-120b"
.\.venv\Scripts\python.exe scripts/manual_test.py --provider groq
```

Use a JSON-output-capable model available in your Groq account. This mode sends
the disposable demo module and exception context to Groq and can incur API usage.
No OpenAI key is required. Review the proposed change before answering `y`.
The deterministic regression test requires division by zero to return `None`;
an AI proposal with a different behavior will be rejected by verification.

For explicitly enabled automatic activation:

```powershell
.\.venv\Scripts\python.exe scripts/manual_test.py --provider groq --auto-apply
```

For OpenAI, set `OPENAI_API_KEY` and `OPENAI_MODEL`, then use `--provider openai`.
`--provider auto` selects the first available configured provider, defaulting to
Groq before OpenAI. The manual script does not enable cross-provider fallback.

## 3. Controlled rollback

```powershell
.\.venv\Scripts\python.exe scripts/manual_test.py --scenario rollback --auto-apply
```

No API key is needed. This candidate passes the normal-input test but deliberately
fails the protected retry. Expected: `ROLLBACK CHECK: PASS`, a
`repair_rolled_back` audit event, original source restored, and the original
`ZeroDivisionError` preserved. The next healthy call still returns `4.0`.

All scenarios use disposable source, tests, snapshots and audit logs in a temporary
directory. Logs are printed before cleanup; the repository's application source
is not changed by this script. Exit code 0 means the selected check passed; 1
means it did not complete; 2 indicates setup/configuration failure.

## Optional complete suites

If you want to run the added automated regression tests yourself:

```powershell
.\.venv\Scripts\python.exe -m pytest
$env:PYO3_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
cargo test
```

These cover provider routing and malformed responses, review decisions, stale
source rejection, failed activation/retry, rollback and native verification.
Provider transport tests use mocked responses; they do not consume API credits.
