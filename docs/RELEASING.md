# Release selfheal-runtime 0.3.0

This guide is for the maintainer. It does not publish anything automatically.
Build and installed-wheel validation remain required before uploading. Only the
Python SDK is shipped. The native core uses CPython's 3.11+ stable ABI; standard
GIL-enabled CPython is supported. Free-threaded Python, PyPy and Node are outside
this release's support contract.

## Review before building

Confirm ownership of the PyPI name `selfheal-runtime` and the source you are
licensing. `LICENSE` covers original source under MIT; `THIRD_PARTY_NOTICES.md`
preserves the locked Rust dependency notices. Distribution metadata includes
the conservative combined SPDX expression for those dependencies. A license
does not grant rights to another developer's source, API account or data.

Keep versions in `pyproject.toml`, `Cargo.toml` and `python/selfheal/__init__.py`
equal. Commit Cargo.lock with the release sources; rebuild notices whenever the
lock changes. Use an empty release output directory to avoid uploading old files.
Never bundle `.env`, `.selfheal`, API keys, session tokens or local snapshots.

## Commands for you to run

From Developer PowerShell with the Rust MSVC toolchain and Windows SDK available:

```powershell
python -m pip install --upgrade "maturin>=1.15,<2" build twine
cargo fetch --locked
python scripts/generate_notices.py --check
python -m build --outdir dist
python scripts/check_dist.py
python -m twine check dist/*
```

`python -m build` builds the source archive and then a wheel from that archive.
It must succeed with the committed lock. Python tests can be run after rebuilding
the editable native extension; Rust tests also compile code:

```powershell
python -m pip install -e ".[dev]"
python -m pytest
cargo test
```

The regression suite uses deterministic or mocked providers and must not require
a developer's real API credentials. API-backed manual testing is a separate step
using the maintainer's own key and enabled model; see `MANUAL_TESTING.md`.
The timing interface and worker helpers require a fresh native rebuild and the
`cloudpickle` dependency. Run the isolated manual timing calculator and validate
Windows Job cleanup/fail-stop behavior yourself using [TIMING.md](TIMING.md).

## Validate the installed wheel outside this checkout

Create a fresh environment in another directory. Install exactly the new wheel,
not an editable checkout, with the verification extra. Confirm imports, console
commands and the native extension. Do not run from the repository directory,
where local imports could hide missing wheel contents.

```powershell
python -m venv C:\Temp\selfheal-wheel-check
& C:\Temp\selfheal-wheel-check\Scripts\python.exe -m pip install "D:\Projects\selfheal\dist\<new-wheel-file>.whl[verification]"
Set-Location C:\Temp
& C:\Temp\selfheal-wheel-check\Scripts\python.exe -c "import selfheal; import selfheal._core; print(selfheal.__version__, selfheal.__file__)"
& C:\Temp\selfheal-wheel-check\Scripts\python.exe -m selfheal --help
```

In an app directory without `.env`, unset inherited `GROQ_API_KEY`, `GROQ_MODEL`,
`OPENAI_API_KEY`, `OPENAI_MODEL` and `SELFHEAL_USE_GROQ` in that test terminal.
`python -m selfheal check-config` must exit 2 with an actionable configuration
error. Repeat with Groq selected, OpenAI selected, a missing selected model, and
an invalid boolean. Configuration checks make no API requests. Then run a real
registered app with your own credentials, inspect a preview, approve a patch,
verify the PID stays the same, and verify rollback after a failing live check.

## Platform wheels and publication

Windows-built wheels serve Windows only. Supply wheels for every OS/architecture
you advertise. `.github/workflows/release.yml` is a manually dispatched artifact
workflow for Windows x64, Linux x64, and macOS Intel/Apple Silicon; it builds and
checks artifacts and tests the installed wheel but does not upload to PyPI.
Linux wheels use manylinux images.
One abi3 wheel per supported OS/architecture serves normal CPython 3.11+.
Users without a matching wheel need Rust and platform build prerequisites.

After reviewing all artifacts, start with TestPyPI. Configure maintainer upload
credentials separately from the SDK's provider keys. Prefer PyPI Trusted
Publishing when configuring an automated release; no publishing secret is stored
in this repository.

```powershell
python -m twine upload --repository testpypi dist/*
# After TestPyPI installation and real-app validation succeed:
python -m twine upload dist/*
```

PyPI uploads cannot be replaced using the same version. Upload only the reviewed
version's files. A successful local source review is not installed-wheel or
production verification.

References: [Maturin distribution](https://www.maturin.rs/distribution.html),
[package licensing](https://packaging.python.org/en/latest/guides/licensing-examples-and-user-scenarios/),
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/).
