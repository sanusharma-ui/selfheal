"""Run candidate code in a disposable process, using a snapshot of app state.

No fallback to running unsafe candidates in the application. Nonserializable
health state is rejected with an actionable error. This is process isolation,
not an OS permission sandbox and not isolation of external side effects.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import RLock
from types import ModuleType
import time

import cloudpickle

from .models import HealthCheckResult, HealthReport
from .timing import RepairTimeout, stop_process_tree
from ._process_tree import python_worker_command

_serialization_lock = RLock()


class CandidateIsolationError(ValueError):
    pass


def candidate_health(check, candidate, project_root, budget, *, updates=()):
    def serialize():
        # cloudpickle's by-value registry is global; restore every registration
        # we own even when serialization or the deadline fails.
        if not _serialization_lock.acquire(timeout=budget.remaining()):
            budget.check("candidate snapshot lock")
            raise RepairTimeout("Candidate snapshot lock exceeded the repair budget")
        registered = []
        try:
            existing = cloudpickle.list_registry_pickle_by_value()
            modules = list(sys.modules.values())
            for module in modules:
                if module is None or not getattr(module, "__file__", None):
                    continue
                try:
                    path = Path(module.__file__)
                    if any(part in (".venv", "venv", "site-packages") for part in path.parts):
                        continue
                    path.resolve().relative_to(project_root)
                except (ValueError, OSError, TypeError):
                    continue
                if (module.__name__ == "selfheal" or module.__name__.startswith("selfheal.")
                        or module.__name__ in existing):
                    continue
                cloudpickle.register_pickle_by_value(module)
                registered.append(module)
            output = io.BytesIO()
            cloudpickle.dump(list(sys.path), output)
            # Serialize the original object graph together with pending code
            # updates. Class descriptors, instances, helpers and aliases retain
            # their relationships; code changes happen only inside the worker.
            view = ModuleType(candidate.__name__)
            view.__dict__.update(candidate.__dict__)
            cloudpickle.dump((check, view, tuple(updates)), output)
            payload = output.getvalue()
            if len(payload) > 4_000_000:
                raise ValueError("Candidate health snapshot exceeds 4 MB")
            return payload
        finally:
            for module in registered:
                cloudpickle.unregister_pickle_by_value(module)
            _serialization_lock.release()

    try:
        payload = budget.run(serialize, "candidate health snapshot")
    except RepairTimeout:
        raise
    except Exception as exc:
        raise CandidateIsolationError("Candidate health state cannot be serialized. Use pure checks with serializable state; "
                         "locks, sockets and live service clients must stay outside the health snapshot.") from exc
    directory = project_root / ".selfheal" / "health-workers"
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="candidate-", dir=directory) as temporary:
        request, response = Path(temporary) / "request.bin", Path(temporary) / "result.json"
        descriptor = os.open(request, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
        budget.check("candidate health worker startup")
        command, environment = python_worker_command(
            str(Path(__file__).with_name("_health_worker.py")), str(request), str(response))
        process = subprocess.Popen(
            command, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            budget.track_worker(process.pid)
            while not response.is_file():
                budget.check("candidate health checks")
                if process.poll() is not None:
                    raise CandidateIsolationError("Candidate health worker failed; live activation refused. "
                                                  "Check serializable state and the worker interpreter dependencies.")
                time.sleep(min(0.025, budget.remaining()))
            budget.check("candidate health result")
            with response.open("rb") as source:
                raw = source.read(64_001)
            if len(raw) > 64_000:
                raise ValueError("Candidate health report exceeds 64 KB")
            data = json.loads(raw)
            return HealthReport(tuple(HealthCheckResult(item["name"], item["ok"] is True, item["details"])
                                      for item in data["checks"]))
        finally:
            with budget.cleanup():
                try:
                    stop_process_tree(process)
                finally:
                    process.stdin.close()
                budget.track_worker(None)
