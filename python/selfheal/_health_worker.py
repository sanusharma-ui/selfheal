"""Private, fresh-interpreter candidate health worker. Local trusted pickle only."""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
import sys

from _process_tree import contain_current_process


def _health_report(check, module):
    """Evaluate sync or async checks, then finish all owned loop resources.

    The disposable worker owns this loop. Cleanup remains inside the parent's
    repair deadline, including callbacks that resist task cancellation.
    """
    from selfheal.models import HealthCheckResult, HealthReport

    # Existing sync checks may own a loop themselves (for example asyncio.run).
    # Only start our loop when their return value actually needs awaiting.
    report = check(module)
    if inspect.isawaitable(report):
        async def evaluate():
            return await report

        report = asyncio.run(evaluate())
    if not isinstance(report, HealthReport) or any(not isinstance(item, HealthCheckResult) for item in report.checks):
        raise TypeError("health_check must return a HealthReport")
    return report


def main():
    contain_current_process()  # Must succeed before any callback/code is loaded.
    import cloudpickle
    request, response = map(Path, sys.argv[1:])
    with request.open("rb") as source:
        paths = cloudpickle.load(source)
        sys.path[:0] = paths
        check = cloudpickle.load(source)
        module, updates = cloudpickle.load(source)
    try:
        for function, code in updates:
            if hasattr(function, "__globals__") and isinstance(function.__globals__, dict):
                function.__globals__.update(module.__dict__)
            function.__code__ = code
        for val in module.__dict__.values():
            if hasattr(val, "__globals__") and isinstance(val.__globals__, dict):
                val.__globals__.update(module.__dict__)
        report = _health_report(check, module)
        data = {"checks": [{"name": item.name, "ok": item.ok is True, "details": item.details}
                           for item in report.checks]}
    except (Exception, asyncio.CancelledError) as exc:
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
