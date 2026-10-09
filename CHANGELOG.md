# Changelog

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
