"""One monotonic repair budget, with time reserved inside it for recovery."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event, Thread
import time

_current = ContextVar("selfheal_repair_deadline", default=None)
# Keep deadlines independent of an application's patched scheduling clock.
# The watchdog uses the OS clock in a fresh interpreter, so both must agree.
_monotonic = time.monotonic
_SDK_FRAMES = frozenset((__file__, str(Path(__file__).with_name("runtime.py"))))


class RepairTimeout(TimeoutError):
    """The shared transaction budget expired, rather than a failed health check."""


class _CallbackExpired(BaseException):
    pass  # User callbacks' ordinary `except Exception` must not swallow a timeout.


def current_deadline():
    return _current.get()


def checkpoint(stage):
    budget = current_deadline()
    if budget is not None:
        budget.check(stage)


def stop_process_tree(process):
    """Workers own Windows jobs or POSIX groups before executing user code."""
    if os.name == "nt":
        if process.poll() is None:
            process.kill()  # Closing the worker's job handle kills descendants.
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=2)


class RepairDeadline:
    def __init__(self, config):
        self.started = _monotonic()
        self.total = config.repair_timeout_seconds
        self.hard_at = self.started + self.total
        self.reserve = min(config.timeout_cleanup_seconds, self.total * 0.1)
        self.work_at = self.hard_at - self.reserve
        self.cleaning = False
        self.stage = "starting"
        self.config = config
        self.watchdog = None
        self._running = False

    def remaining(self):
        return max(0.0, (self.hard_at if self.cleaning else self.work_at) - _monotonic())

    def check(self, stage):
        self.stage = stage
        if self.remaining() <= 0:
            raise RepairTimeout(f"Repair deadline exceeded during {stage}; total budget {self.total:g}s (cleanup reserved).")

    def run(self, callback, stage):
        """Bound Python bytecode in live callbacks; native stalls use the watchdog.

        This does not inject exceptions into another thread. The previous trace
        hook is restored; live retry state and return values remain in-process.
        """
        self.check(stage)
        if self._running:
            return callback()  # Keep the outer opcode guard during nested health.
        previous = sys.gettrace()
        self._running = True

        def trace(frame, event, arg):
            if frame.f_code.co_filename in _SDK_FRAMES:
                return None  # Never interrupt the SDK's exception/rollback machinery.
            frame.f_trace_opcodes = True
            if self.remaining() <= 0:
                raise _CallbackExpired()
            return trace

        try:
            sys.settrace(trace)
            result = callback()
        except _CallbackExpired:
            raise RepairTimeout(f"Repair deadline exceeded during {stage}; total budget {self.total:g}s (cleanup reserved).") from None
        finally:
            sys.settrace(previous)
            self._running = False
        self.check(stage)
        return result

    def _send(self, data):
        if self.watchdog is not None:
            if self.watchdog.poll() is not None:
                raise RuntimeError("Repair watchdog exited unexpectedly; activation refused")
            self.watchdog.stdin.write(json.dumps(data).encode("utf-8") + b"\n")
            self.watchdog.stdin.flush()

    def arm_recovery(self, target, snapshot, original, replacement):
        self._send({"recovery": {"root": str(self.config.project_root), "target": str(target),
                    "snapshot": str(snapshot), "snapshot_root": str(self.config.project_root / self.config.snapshot_dir),
                    "original_hash": hashlib.sha256(original).hexdigest(),
                    "candidate_hash": hashlib.sha256(replacement.encode("utf-8")).hexdigest()}})

    def committed(self):
        self._send({"recovery": None})

    def track_worker(self, pid):
        # The independent watchdog also owns worker cleanup if the app itself
        # stalls/exits, including a native worker that holds the Python GIL.
        self._send({"worker_pid": pid})

    @contextmanager
    def cleanup(self):
        previous, self.cleaning = self.cleaning, True
        try:
            yield
        finally:
            self.cleaning = previous

    def __enter__(self):
        self.nested = current_deadline() is not None
        if self.nested:
            return current_deadline()  # Nested SDK calls cannot renew the budget.
        self.token = _current.set(self)
        try:
            if self.config.hard_timeout:
                self.watchdog = subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name("_watchdog.py"))],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
                self._send({"pid": os.getpid(), "hard_at": self.hard_at})
                ready, reply = Event(), []

                def receive_ready():
                    try:
                        reply.append(self.watchdog.stdout.readline(16))
                    finally:
                        ready.set()

                Thread(target=receive_ready, name="selfheal-watchdog-ready", daemon=True).start()
                if not ready.wait(min(5.0, self.remaining())) or reply != [b"READY\n"]:
                    raise RuntimeError("Repair watchdog could not be armed; no user code or patch was executed")
            self.check("watchdog startup")
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, *exc):
        if self.nested:
            return
        try:
            if self.watchdog is not None:
                try:
                    self._send({"cancel": True})
                except (OSError, RuntimeError):
                    pass
                self.watchdog.stdin.close()
                try:
                    self.watchdog.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.watchdog.kill()
                    self.watchdog.wait(timeout=2)
                self.watchdog.stdout.close()
        finally:
            _current.reset(self.token)
