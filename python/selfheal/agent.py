"""An embeddable, bounded background caretaker for a running Python app."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict
import importlib
from pathlib import Path
from threading import Event, RLock, Thread
import time
import tomllib

from .config import AgentConfig, SelfHealConfig
from .live import LiveTarget
from .privacy import redact
from .timing import RepairDeadline, RepairTimeout
from .runtime import SelfHealRuntime
from .terminal import TerminalReporter


class SelfHealAgent:
    """Periodic health checks, exception notifications and opted-in improvements.

    One runtime owns activation. The worker coalesces failure notifications,
    limits AI transactions, backs off failures, and suspends repeatedly failing
    targets. Application traffic uses bind/call to resolve the latest function.
    """

    def __init__(self, runtime: SelfHealRuntime, targets: tuple[LiveTarget, ...], *,
                 config: AgentConfig | None = None):
        if not targets or len({target.name for target in targets}) != len(targets):
            raise ValueError("Register targets with unique names")
        for target in targets:
            source = runtime.language.source_file(target.module_name)
            source.relative_to(runtime.project_root)
            runtime.language.prepare(target.module_name, source.read_text(encoding="utf-8"), target.editable_symbols)
        self.runtime = runtime
        if self.runtime.event_sink is None:
            self.runtime.event_sink = TerminalReporter()
        self.targets = {target.name: target for target in targets}
        self.config = config or AgentConfig()
        if self.config.auto_apply and any(target.retry_safe is not True for target in targets):
            raise ValueError("Automatic activation requires retry_safe=True on every target")
        self._lock = RLock()
        self._wake = Event()
        self._stop = Event()
        self._thread = None
        self._paused = False
        self._pending = set()
        self._forced = set()
        self._notifications = {}
        self._attempts = deque()
        self._failures = {name: 0 for name in self.targets}
        self._backoff = {}
        self._suspended = set()
        self._last = {}
        self._active = None

    @classmethod
    def from_config(cls, path: Path, *, provider=None, event_sink=None):
        """Load a developer-owned TOML file. Callback imports are trusted code."""
        path = Path(path).resolve()
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        runtime = SelfHealRuntime(SelfHealConfig.from_toml(path), provider=provider, event_sink=event_sink)
        targets = []
        for item in data.get("targets", []):
            fields = dict(item)
            module_name, separator, symbol = fields.pop("health_check").partition(":")
            if not separator or not symbol.isidentifier():
                raise ValueError("health_check must be a module:function reference")
            callback = getattr(importlib.import_module(module_name), symbol)
            fields["editable_symbols"] = tuple(fields.get("editable_symbols", ()))
            targets.append(LiveTarget(health_check=callback, **fields))
        return cls(runtime, tuple(targets), config=AgentConfig.from_toml(path))

    def _emit(self, phase, message):
        try:
            self.runtime._emit(phase, message)
        except RepairTimeout:
            pass  # Reporting cannot count one expired transaction as two failures.

    def start(self):
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Agent already started; create another agent after stopping")
            self._thread = Thread(target=self._run, name="selfheal-agent", daemon=True)
            self._thread.start()
        self._emit("agent_started", f"Interval: {self.config.interval_seconds}s; auto apply: {self.config.auto_apply}")
        return self

    def stop(self, timeout: float | None = None) -> bool:
        """Wait for an in-flight transaction by default; never cancel mid-activation."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
        stopped = self._thread is None or not self._thread.is_alive()
        if stopped:
            self._emit("agent_stopped", "Background caretaker stopped.")
        return stopped

    def pause(self):
        with self._lock:
            self._paused = True
        self._emit("agent_paused", "Future scans paused. An accepted transaction can finish.")

    def resume(self):
        with self._lock:
            self._paused = False
        self._wake.set()
        self._emit("agent_resumed", "Background checks resumed.")

    def request_scan(self, target_name: str | None = None):
        with self._lock:
            if target_name is not None and target_name not in self.targets:
                raise ValueError("Unknown registered target")
            names = tuple(self.targets) if target_name is None else (target_name,)
            self._pending.update(names)
            self._forced.update(names)
        self._wake.set()

    def reset(self, target_name: str | None = None):
        """Re-enable a failed target. This does not reset the hourly AI budget."""
        with self._lock:
            names = tuple(self.targets) if target_name is None else (target_name,)
            if any(name not in self.targets for name in names):
                raise ValueError("Unknown registered target")
            for name in names:
                self._suspended.discard(name)
                self._backoff.pop(name, None)
                self._failures[name] = 0
        self._emit("agent_reset", "Failure backoff and suspension reset for " + ", ".join(names))

    def call(self, target_name: str, /, *args, **kwargs):
        target = self.targets[target_name]
        try:
            return self.runtime.invoke(module_name=target.module_name, symbol_name=target.symbol_name,
                                       args=args, kwargs=kwargs)
        except Exception as exc:
            self._emit("agent_failure", f"{target_name}: {type(exc).__name__}; queued an inspection.")
            with self._lock:
                text = redact(f"{type(exc).__name__}: {exc}")
                self._notifications[target_name] = text.encode("utf-8")[:4000].decode("utf-8", errors="ignore")
            self.request_scan(target_name)
            raise  # Preserve the application error; only future calls use the repair.

    def bind(self, target_name: str):
        if target_name not in self.targets:
            raise ValueError("Unknown registered target")

        def protected(*args, **kwargs):
            return self.call(target_name, *args, **kwargs)

        return protected

    def status(self):
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            return {"running": self._thread is not None and self._thread.is_alive(),
                    "paused": self._paused, "active_target": self._active,
                    "config": asdict(self.config), "pending": sorted(self._pending),
                    "attempts_last_hour": len(self._attempts),
                    "suspended": sorted(self._suspended),
                    "backoff_seconds": {name: round(max(0, until - now), 1) for name, until in self._backoff.items()},
                    "last_results": dict(self._last)}

    def _prune(self, now):
        while self._attempts and self._attempts[0] <= now - 3600:
            self._attempts.popleft()

    def _run(self):
        deadline = time.monotonic() + (0 if self.config.run_on_start else self.config.interval_seconds)
        while not self._stop.is_set():
            self._wake.wait(max(0, deadline - time.monotonic()))
            self._wake.clear()
            with self._lock:
                if self._stop.is_set():
                    break
                if time.monotonic() >= deadline:
                    self._pending.update(self.targets)
                    deadline = time.monotonic() + self.config.interval_seconds
                if self._paused:
                    continue
                names = tuple((name, name in self._forced) for name in self.targets if name in self._pending)
                self._pending.difference_update(name for name, _ in names)
                self._forced.difference_update(name for name, _ in names)
            for name, force in names:
                if self._stop.is_set():
                    break
                try:
                    self._scan(name, force=force)
                except Exception as exc:
                    self._failed(name, f"Agent scan stopped ({type(exc).__name__}).")
        with self._lock:
            self._active = None

    def _scan(self, name, *, force=False):
        with self._lock:
            if self._paused or name in self._suspended or self._backoff.get(name, 0) > time.monotonic():
                return
        # The initial local scan and any ensuing repair share ONE budget. Nested
        # runtime health/inspection calls reuse it instead of renewing it.
        try:
            with RepairDeadline(self.runtime.config):
                self._scan_with_deadline(name, force=force)
        except RepairTimeout as exc:
            self._failed(name, str(exc), status="timed_out")

    def _scan_with_deadline(self, name, *, force=False):
        with self._lock:
            now = time.monotonic()
            if self._paused or name in self._suspended or self._backoff.get(name, 0) > now:
                return
            if self.runtime.healing_disabled:
                self._paused = True
                self._emit("agent_disabled", "Runtime recovery failed; restart the application.")
                return
        target = self.targets[name]
        with self._lock:
            observed_failure = self._notifications.pop(name, "")
        report = self.runtime.check_health(target.module_name, target.health_check)
        improvement = self.config.improvement_instructions.strip()
        improve_now = bool(report.ok and improvement and not observed_failure)
        if report.ok and not self.config.scan_when_healthy and not improvement and not force and not observed_failure:
            with self._lock:
                self._last[name] = {"status": "healthy", "message": "Local checks passed; no AI request."}
                self._failures[name] = 0
                self._backoff.pop(name, None)
            self._emit("agent_healthy", f"{name}: local checks passed; no AI request.")
            return
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            if self._paused or self._stop.is_set():
                return
            if len(self._attempts) >= self.config.max_attempts_per_hour:
                self._last[name] = {"status": "budget_limited", "message": "Hourly AI transaction budget reached."}
                self._emit("agent_budget", f"{name}: hourly AI transaction budget reached.")
                return
            self._attempts.append(now)
            self._active = name
        try:
            result = self.runtime.inspect(
                module_name=target.module_name, symbol_name=target.symbol_name,
                health_check=target.health_check, retry_safe=target.retry_safe,
                operation="improve" if improve_now else "heal",
                instructions=target.instructions + ("\n\n" + improvement if improve_now else ""),
                editable_symbols=target.editable_symbols, apply=self.config.auto_apply,
                observed_failure=observed_failure,
            )
            if result.status == "busy":
                with self._lock:
                    self._attempts.pop()  # No provider request was made.
                    self._pending.add(name)
                    if force:
                        self._forced.add(name)
                    if observed_failure:
                        self._notifications[name] = observed_failure
                return
            if result.status in ("applied", "no_change", "preview"):
                with self._lock:
                    self._last[name] = {"status": result.status, "message": result.message,
                                        "proposal_id": result.proposal_id, "snapshot_file": result.snapshot_file}
                    self._failures[name] = 0
                    self._backoff.pop(name, None)
            else:
                self._failed(name, result.message, status=result.status if result.status == "timed_out" else "failed")
        finally:
            with self._lock:
                self._active = None

    def _failed(self, name, message, *, status="failed"):
        with self._lock:
            self._failures[name] += 1
            count = self._failures[name]
            self._last[name] = {"status": status, "message": redact(message)}
            self._backoff[name] = time.monotonic() + min(
                self.config.failure_backoff_seconds * 2 ** min(count - 1, 10), 3600)
            if count >= self.config.max_consecutive_failures:
                self._suspended.add(name)
        self._emit("agent_backoff", f"{name}: attempt failed ({count}); {redact(message)}")
        if count >= self.config.max_consecutive_failures:
            self._emit("agent_suspended", f"{name}: repeated failures; use agent reset after fixing the cause.")

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.stop()
