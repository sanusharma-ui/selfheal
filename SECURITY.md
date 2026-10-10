# Security

SelfHeal Runtime performs source-code verification and controlled source replacement. Treat it as privileged application infrastructure.

## Development guidance

- Use SelfHeal only on software and environments you are authorized to modify.
- Do not place provider API keys in source control.
- Keep `auto_apply=False` until verification policies are appropriate for the project.
- Do not give an AI provider direct filesystem or shell mutation authority through this framework.
- Review audit records under `.selfheal/`.
- Keep backups and source control available while experimenting.

## V0.2 boundaries

- OpenAI/Groq providers use HTTPS and refuse redirects. Credentials are not logged
  in provider errors. Cross-provider fallback requires explicit configuration.
- Provider context omits argument values and redacts common secrets. Source with
  detected secrets is refused. This is not comprehensive personal-data detection;
  review what source and exception context your application may transmit.
- Review mode verifies code before presenting approval. Verification tests execute
  in a copied workspace with host permissions, not an OS sandbox. They can access
  the network, environment and host files. Native verification consumes the
  remaining shared repair budget. Its workers own POSIX process groups or Windows
  kill-on-close Jobs before launching checks; this is process-tree cleanup, not
  an OS permission sandbox. Direct native-core calls default to a 120s total
  verification budget unless `timeout_seconds` is supplied.
- Only safe-to-repeat calls should use `retry_safe=True`. Rollback restores source,
  not external side effects, stale references, class instances or arbitrary memory.
- Single-runtime serialization is not process/worker coordination. Concurrent source
  writers and hostile filesystem changes are outside this development threat model.
- If source or function-code restoration fails, restart the application; healing is
  disabled for the affected runtime instance and reported through `last_result`.

## Background agent and local control

- Register only developer-owned code and pure, bounded health checks. Keep checks
  outside editable function bodies. Target allowlists are not execution sandboxes.
- Candidate checks run after authorization in a disposable process with a
  cloudpickle snapshot of trusted local app state. Unserializable state fails
  closed. Never accept serialized health payloads from external clients. Worker
  files live temporarily under ignored `.selfheal/` and are removed on normal
  completion or soft timeout; a forced kill may leave files for manual cleanup.
- Live baseline/activation checks and retries preserve the app's actual objects.
  Python bytecode has a cooperative deadline; an independent watchdog terminates
  the owning app if native code cannot unwind by the configured hard deadline.
  This is fail-stop, not transparent recovery. An external supervisor must restart
  the app. Disabling `SELFHEAL_HARD_TIMEOUT` removes that native-stall bound.
- Timeout watchdog recovery only restores source matching the exact uncommitted
  candidate and a hash-checked retained snapshot, after confirming owner exit.
  It preserves developer edits. It cannot undo external writes or memory changes,
  and it is not general forced-kill crash recovery. See `docs/TIMING.md`.
- Scoped activation preserves module state by updating function code rather than
  re-running initialization. Synchronous module-level functions are supported;
  wrapper closures, async/generator functions and native callables are rejected.
- Scheduled activation requires `AgentConfig(auto_apply=True)` and safe-to-repeat
  targets. Explicit `heal`/`improve` commands separately authorize activation.
- Use AI transaction budgets, failure backoff, and target suspension. Pausing the
  agent prevents future background scans; an accepted transaction may finish.
- Local control is loopback-only with a random credential in `.selfheal/live.json`.
  Keep that file private. Windows file mode flags do not provide a custom ACL.
- One app process must own the agent. All participating calls must use its dynamic
  dispatch/activation lock. Multi-worker patching and forced-kill crash recovery
  are not implemented. Graceful shutdown waits for accepted repairs.
- An AI patch can pass known checks while still containing bugs. Improvements need
  developer-defined contracts; rollback does not undo side effects or memory changes.

## Reporting issues

For now, report security issues privately to the project maintainer rather than publishing sensitive exploitation details in a public issue.

## Licensing and data responsibilities

Original source is MIT licensed; bundled dependency notices are retained in
`THIRD_PARTY_NOTICES.md`. Use only source and systems you own or are authorized
to modify. The developer supplies their own API account and chooses the vendor;
review that vendor's terms and obtain permission to transmit the application's
source and diagnostic data. No SDK license guarantees AI output ownership,
regulatory compliance or permission to modify third-party software.
