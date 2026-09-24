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
  the network, environment and host files and currently have no execution deadline.
- Only safe-to-repeat calls should use `retry_safe=True`. Rollback restores source,
  not external side effects, stale references, class instances or arbitrary memory.
- Single-runtime serialization is not process/worker coordination. Concurrent source
  writers and hostile filesystem changes are outside this development threat model.
- If rollback or restored-module reload fails, restart the application; healing is
  disabled for the affected runtime instance and reported through `last_result`.

## Reporting issues

For now, report security issues privately to the project maintainer rather than publishing sensitive exploitation details in a public issue.
