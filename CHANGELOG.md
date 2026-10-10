# Changelog

## Unreleased

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
