"""Private, fresh-interpreter candidate health worker. Local trusted pickle only."""
from __future__ import annotations

import json
from pathlib import Path
import sys

from _process_tree import contain_current_process


def main():
    contain_current_process()  # Must succeed before any callback/code is loaded.
    import cloudpickle
    request, response = map(Path, sys.argv[1:])
    with request.open("rb") as source:
        paths = cloudpickle.load(source)
        sys.path[:0] = paths
        check, module = cloudpickle.load(source)
    from selfheal.models import HealthCheckResult, HealthReport
    try:
        report = check(module)
        if not isinstance(report, HealthReport) or any(not isinstance(item, HealthCheckResult) for item in report.checks):
            raise TypeError("health_check must return a HealthReport")
        data = {"checks": [{"name": item.name, "ok": item.ok is True, "details": item.details}
                           for item in report.checks]}
    except Exception as exc:
        data = {"checks": [{"name": "health callback", "ok": False, "details": type(exc).__name__}]}
    encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(encoded) > 64_000:
        raise ValueError("Health report exceeds the diagnostic limit")
    temporary = response.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    temporary.replace(response)
    # Keep the group/job leader alive until the parent stops the whole owned
    # tree. This also prevents PID reuse between result delivery and cleanup.
    sys.stdin.buffer.read(1)


if __name__ == "__main__":
    main()
