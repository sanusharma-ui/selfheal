# Manual testing on Windows

Run all commands from `D:\Projects\selfheal`.

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
