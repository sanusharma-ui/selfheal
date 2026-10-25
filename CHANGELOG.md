# Changelog

## Unreleased

- Dotted targets for directly defined Python instance/static/class methods and
  property accessors, with class-cell preservation and descriptor-aware rollback.
- Developer-owned `HealthSuite`, `ScenarioCheck` and optional Hypothesis-backed
  `PropertyCheck`, including bounded exploration and retained counterexamples.
- Explicit, bounded `context_files` for read-only cross-module diagnosis, with
  secret checks and stale-context rejection before commit.
- Native interface marker and offline `selfheal doctor` command to identify
  stale editable extensions before contacting an AI provider.
- Windows virtualenv workers start the underlying interpreter while retaining
  their environment, so parent ownership and process cleanup use the real PID.
- Broader CI matrix and release checks for worker files, required dependencies,
  matching versions and rebuilding a wheel from the source distribution.
- Final validation was stopped at the maintainer's request. These source changes
  require a fresh native build, complete regression runs and installed-wheel
  validation before publication; they are not a verified production release.

- Module-level async function dispatch and safe retry on the owning asyncio loop,
  with async health/inspection APIs and async agent bindings and lifecycle.
- Awaited calls participate in activation coordination; pending repairs drain
  active calls and pause new admissions before changing function code.
- Cancellation requests abort accepted repairs at safe checkpoints and waits for
  coroutine cleanup and rollback; failed retries cannot recursively request AI.
- Direct automatic repairs have an hourly transaction limit and failure cooldown,
  alongside the background agent's existing budgets, backoff and suspension.
- Supplied offline async regression cases and an integration/cancellation guide.
  Tests and native builds remain for maintainer execution; no passing result or
  production-readiness claim is made for these changes.

## 0.3.0 — initial PyPI release preparation

- Python SDK with registered live targets, developer health contracts, background
  inspection, manual heal/improve commands, previews and opt-in automatic patching.
- Developer-owned Groq/OpenAI credentials required; `.env` boolean selection,
  safe `ConfigurationError` messages and offline `selfheal check-config` command.
- Function-body activation preserves module state and function identity. Scope
  checks protect unrelated code; candidate and running health checks gate commit.
- Same-process activation, retained function-code rollback, source snapshots,
  review decisions, terminal diffs and Rust audit records.
- Native verification deadline and bounded diagnostics, copied-source import
  paths, atomic source rollback and source-permission preservation.
- CPython 3.11+ ABI3 packaging, locked dependencies, MIT source license and
  bundled dependency notices, archive checker and manual artifact workflow.

Release preparation does not establish a successful build, installed-wheel
compatibility, live-provider result or production deployment. Run the maintainer
release checks before publishing this version.
