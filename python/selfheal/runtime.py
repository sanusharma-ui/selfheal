from __future__ import annotations

from collections.abc import Awaitable, Callable
from collections import deque
from contextlib import contextmanager
from difflib import unified_diff
from threading import Lock
from types import ModuleType
from typing import Any
import asyncio
import importlib
import inspect as inspection
import time
import uuid

from ._async_support import (AsyncOperation, AsyncUsageError, RepairCancelled, callback_runtime,
                             close_coroutine, operation, require_sync_api, run_worker)
from ._coordination import InvocationGate
from ._symbols import resolve_binding, valid_symbol
from ._native import check_native_compatibility
from .diagnostics import context_unchanged, related_sources
from .config import SelfHealConfig
from .languages import PythonRuntimeAdapter
from .models import (FailureContext, HealthCheckResult, HealthReport, RepairEvent,
                     RepairProposal, RepairRequest, RepairResult, RepairReview)
from .observer import build_failure_context
from .privacy import redact
from .providers import AIProvider, provider_from_env
from .registry import FunctionRegistry
from .scope import validate_edit_scope
from .health_worker import CandidateIsolationError, candidate_health
from .timing import RepairDeadline, RepairTimeout, current_deadline

try:
    from ._core import CoreEngine
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "SelfHeal native extension is unavailable. Install a wheel for your Python/OS with "
        "'python -m pip install pyselfheal-rt', or build this checkout using the release guide."
    ) from exc


class SelfHealRuntime:
    """One protected operation, one candidate, one explicitly safe retry.

    reviewer receives a frozen review and must return exactly True to approve.
    Coroutine calls share admission; sync calls and activation are exclusive.
    Other runtimes/workers and direct calls are outside this gate.
    """

    def __init__(self, config: SelfHealConfig, *, provider: AIProvider | None = None,
                 reviewer: Callable[[RepairReview], bool] | None = None,
                 event_sink: Callable[[RepairEvent], None] | None = None):
        check_native_compatibility()
        self.config = config
        self.project_root = config.normalized_root()
        self.provider = provider if provider is not None else provider_from_env()
        self.reviewer = reviewer
        self.event_sink = event_sink
        self.registry = FunctionRegistry()
        self.language = PythonRuntimeAdapter()
        self.last_result: RepairResult | None = None
        self._gate = InvocationGate()
        self._async_worker_slot = Lock()
        self._loop_lock = Lock()
        self._async_loop = None
        self._auto_attempts = deque()
        self._auto_backoff = {}
        self._healing = False
        self._disabled = False
        self.core = CoreEngine(str(self.project_root), config.python_executable,
                               config.run_tests, config.snapshot_dir, config.audit_file)

    def handle(self, module_name: str, symbol_name: str):
        return self.registry.handle(module_name, symbol_name)

    @contextmanager
    def _locked(self):
        with self._gate.sync(check=lambda: self._check("application lock")):
            yield

    def _check(self, stage):
        bridge = operation.get()
        if bridge is not None:
            bridge.checkpoint()
        budget = current_deadline()
        if budget is not None:
            budget.check(stage)

    def _guard(self, callback, stage, *, allow_async=False):
        self._check(stage)
        budget = current_deadline()
        token = callback_runtime.set(self)
        value = None

        def invoke_callback():
            nonlocal value
            value = callback()
            return value

        try:
            if budget is None:
                invoke_callback()
            else:
                budget.run(invoke_callback, stage)
        except BaseException:
            # The opcode guard can expire just after a coroutine was created.
            close_coroutine(value)
            raise
        finally:
            callback_runtime.reset(token)
        if inspection.isawaitable(value):
            if not allow_async:
                close_coroutine(value)
                raise AsyncUsageError(f"{stage} must be a synchronous callback")
            if budget is None or self._async_loop is None or not self._async_loop.is_running():
                close_coroutine(value)
                raise AsyncUsageError("Async callbacks require an attached running loop; use acheck_health/ainspect or async with agent")
            if budget._running:
                close_coroutine(value)
                raise AsyncUsageError("An async callback cannot be nested inside a guarded synchronous callback")
            bridge = operation.get() or AsyncOperation(self._async_loop, self)
            value = bridge.await_result(value, budget, stage)
        self._check(stage)
        return value

    def _sync_entry(self, async_name, *, dispatch=False):
        require_sync_api(async_name)
        if not (dispatch and self._gate.owns_sync()):
            self._callback_entry()
        if self._gate.in_async_call():
            raise RuntimeError("A protected coroutine cannot wait for synchronous runtime dispatch")

    def _callback_entry(self):
        if callback_runtime.get() is self:
            raise RuntimeError("Repair callbacks must call module functions directly, not reenter runtime dispatch")

    def attach_async_loop(self):
        """Attach this runtime to one running application loop for its lifetime."""
        loop = asyncio.get_running_loop()
        with self._loop_lock:
            if self._async_loop is not None and self._async_loop is not loop:
                raise RuntimeError("A runtime can belong to only one asyncio event loop")
            self._async_loop = loop
        return self

    def _async_entry(self, *, repair=False):
        self._callback_entry()
        self.attach_async_loop()
        if repair and self._gate.in_async_call():
            raise RuntimeError("An active protected coroutine cannot start a nested repair or health inspection")

    def _sync_target(self, module_name, symbol_name):
        function = self.handle(module_name, symbol_name).resolve()
        if inspection.iscoroutinefunction(function) or inspection.isasyncgenfunction(function):
            raise TypeError("Use ainvoke/acall or an async agent binding for coroutine targets")
        return function

    def _auto_admit(self, key):
        now = time.monotonic()
        while self._auto_attempts and self._auto_attempts[0] <= now - 3600:
            self._auto_attempts.popleft()
        if self._auto_backoff.get(key, 0) > now:
            return RepairResult(False, message="Automatic repair is cooling down after a failed attempt.", status="cooldown")
        if len(self._auto_attempts) >= self.config.max_auto_repairs_per_hour:
            return RepairResult(False, message="Hourly automatic repair budget reached.", status="budget_limited")
        self._auto_attempts.append(now)
        return None

    def _auto_finished(self, key, result):
        now = time.monotonic()
        self._auto_backoff = {name: until for name, until in self._auto_backoff.items() if until > now}
        if result.healed:
            self._auto_backoff.pop(key, None)
        else:
            self._auto_backoff[key] = now + self.config.repair_failure_cooldown_seconds

    def invoke(self, *, module_name: str, symbol_name: str,
               args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None) -> Any:
        """Dispatch current code under the activation lock, without automatic repair."""
        self._sync_entry("await runtime.ainvoke(...)", dispatch=True)
        with self._locked():
            return self._sync_target(module_name, symbol_name)(*args, **(kwargs or {}))

    async def ainvoke(self, *, module_name: str, symbol_name: str,
                      args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None) -> Any:
        """Await current code on the caller loop, participating in activation."""
        self._async_entry()
        async with self._gate.async_call():
            function = self.handle(module_name, symbol_name).resolve()
            if not inspection.iscoroutinefunction(function):
                raise TypeError("ainvoke/acall require a module-level coroutine function")
            return await function(*args, **(kwargs or {}))

    async def acall(self, *, module_name: str, symbol_name: str,
                    args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None,
                    retry_safe: bool = False) -> Any:
        """One awaited operation, one bounded repair, one explicitly safe retry."""
        self._async_entry()
        nested = self._gate.in_async_call()
        async with self._gate.async_call():
            function = self.handle(module_name, symbol_name).resolve()
            if not inspection.iscoroutinefunction(function):
                raise TypeError("ainvoke/acall require a module-level coroutine function")
            original_code = function.__code__
            try:
                return await function(*args, **(kwargs or {}))
            except Exception as exc:
                failure = exc
                if nested:
                    raise  # Only the outer operation may own a repair.
        # No application coroutine is held by this task while the writer drains
        # other calls. CancelledError (a BaseException) never enters this path.
        try:
            result = await run_worker(self, lambda: self._repair_async_failure(
                failure, module_name, symbol_name, args, kwargs or {}, retry_safe, original_code))
        except RepairTimeout as error:
            result = RepairResult(False, message=str(error), status="timed_out")
            self.last_result = result
        except Exception as error:
            result = RepairResult(False, message=f"Healing stopped ({type(error).__name__}). Original exception preserved.")
            self.last_result = result
        if result.healed:
            return result.return_value
        failure.add_note(result.message)
        raise failure.with_traceback(failure.__traceback__)

    def _repair_async_failure(self, exc, module_name, symbol_name, args, kwargs, retry_safe, original_code):
        result = None
        try:
            with RepairDeadline(self.config):
                with self._locked():
                    if self._healing or self._disabled:
                        return RepairResult(False, message="Healing disabled or another repair is active.", status="busy")
                    if self.handle(module_name, symbol_name).resolve().__code__ is not original_code:
                        return RepairResult(False, message="The failed code was already replaced; request not replayed.", status="superseded")
                    key = (module_name, symbol_name)
                    limited = self._auto_admit(key)
                    if limited is not None:
                        self.last_result = limited
                        return limited
                    self._healing = True
                    try:
                        result = self._heal(exc=exc, module_name=module_name, symbol_name=symbol_name,
                                            args=args, kwargs=kwargs, retry_safe=retry_safe)
                    except RepairCancelled as error:
                        result = RepairResult(False, message=str(error), status="cancelled")
                    except RepairTimeout as error:
                        result = RepairResult(False, message=str(error), status="timed_out")
                    except Exception as error:
                        result = RepairResult(False, message=f"Healing stopped ({type(error).__name__}). Original exception preserved.")
                    finally:
                        self._healing = False
                        if result is not None:
                            self.last_result = result
                            self._auto_finished(key, result)
                    if result.status not in ("cancelled", "timed_out"):
                        try:
                            self._emit("complete", result.message, proposal_id=result.proposal_id)
                        except (RepairTimeout, RepairCancelled):
                            pass
                    return result
        except (RepairCancelled, RepairTimeout) as error:
            result = RepairResult(False, message=str(error),
                                  status="cancelled" if isinstance(error, RepairCancelled) else "timed_out")
            self.last_result = result
            return result

    async def acheck_health(self, module_name: str,
                            check: Callable[[ModuleType], HealthReport | Awaitable[HealthReport]]) -> HealthReport:
        self._async_entry(repair=True)
        return await run_worker(self, lambda: self.check_health(module_name, check))

    async def ainspect(self, *, module_name: str, symbol_name: str,
                       health_check: Callable[[ModuleType], HealthReport | Awaitable[HealthReport]],
                       retry_safe: bool = False, operation: str = "heal",
                       instructions: str = "", editable_symbols: tuple[str, ...] = (),
                       observed_failure: str = "", apply: bool = False,
                       context_files: tuple[str, ...] = (),
                       on_event: Callable[[RepairEvent], None] | None = None) -> RepairResult:
        """Run inspection off the loop; await live health checks on this loop."""
        self._async_entry(repair=True)
        try:
            return await run_worker(self, lambda: self.inspect(
                module_name=module_name, symbol_name=symbol_name, health_check=health_check,
                retry_safe=retry_safe, operation=operation, instructions=instructions,
                editable_symbols=editable_symbols, observed_failure=observed_failure,
                apply=apply, context_files=context_files, on_event=on_event))
        except RepairTimeout as error:
            result = RepairResult(False, message=str(error), status="timed_out")
            self.last_result = result
            return result

    @property
    def healing_disabled(self) -> bool:
        return self._disabled

    def _emit(self, phase, message, *, proposal_id=None, diff="", on_event=None):
        event = RepairEvent(phase, redact(message), proposal_id, redact(diff))
        for sink in (self.event_sink, on_event):
            if sink is not None:
                try:
                    self._guard(lambda: sink(event), "event callback")
                except (RepairTimeout, RepairCancelled):
                    raise
                except Exception:
                    # A disconnected terminal must not interrupt recovery.
                    pass

    def _health(self, module_name, check, *, module=None) -> HealthReport:
        try:
            report = self._guard(
                lambda: check(module if module is not None else importlib.import_module(module_name)),
                "live health checks", allow_async=True)
            if not isinstance(report, HealthReport) or any(
                not isinstance(item, HealthCheckResult) for item in report.checks
            ):
                close_coroutine(report)
                raise TypeError("health_check must return a HealthReport")
            return report
        except (RepairTimeout, RepairCancelled, AsyncUsageError):
            raise
        except Exception as exc:
            return HealthReport((HealthCheckResult(
                "health callback", False, redact(f"{type(exc).__name__}: {exc}")),))

    def check_health(self, module_name: str,
                     check: Callable[[ModuleType], HealthReport | Awaitable[HealthReport]]) -> HealthReport:
        """Run local developer checks without making an AI request or mutating source."""
        self._sync_entry("await runtime.acheck_health(...)")
        with RepairDeadline(self.config):
            with self._locked():
                return self._health(module_name, check)

    def inspect(self, *, module_name: str, symbol_name: str,
                health_check: Callable[[ModuleType], HealthReport | Awaitable[HealthReport]],
                retry_safe: bool = False, operation: str = "heal",
                instructions: str = "", editable_symbols: tuple[str, ...] = (),
                observed_failure: str = "",
                apply: bool = False,
                context_files: tuple[str, ...] = (),
                on_event: Callable[[RepairEvent], None] | None = None) -> RepairResult:
        """Inspect a registered target within one configured repair deadline.

        Candidate checks use a disposable state snapshot; activation checks remain
        live. apply=True is explicit authorization; preview never changes source.
        """
        self._sync_entry("await runtime.ainspect(...)")
        try:
            with RepairDeadline(self.config):
                return self._inspect(module_name=module_name, symbol_name=symbol_name,
                    health_check=health_check, retry_safe=retry_safe, operation=operation,
                    instructions=instructions, editable_symbols=editable_symbols,
                    observed_failure=observed_failure, apply=apply, context_files=context_files, on_event=on_event)
        except (RepairTimeout, RepairCancelled) as exc:
            result = RepairResult(False, message=str(exc),
                                  status="cancelled" if isinstance(exc, RepairCancelled) else "timed_out")
            self.last_result = result
            return result

    def _inspect(self, *, module_name, symbol_name, health_check, retry_safe,
                 operation, instructions, editable_symbols, observed_failure, apply, context_files, on_event):
        """Inspect running code on command, including silent logic bugs.

        apply=True is explicit command authorization. Health checks are trusted,
        developer-owned, side-effect-free code, retained across activation. Normal
        invoke/call traffic can continue during provider generation and first
        verification; activation, final verification and checks are serialized.
        """
        if operation not in ("heal", "improve"):
            raise ValueError("operation must be heal or improve")
        if not callable(health_check):
            raise TypeError("A developer-owned health_check callback is required")
        if type(instructions) is not str or len(instructions.encode("utf-8")) > 16_000:
            raise ValueError("instructions must be a string of at most 16000 bytes")
        if operation == "improve" and not instructions.strip():
            raise ValueError("An improvement request is required")
        if type(observed_failure) is not str or len(observed_failure.encode("utf-8")) > 4000:
            raise ValueError("observed_failure must be a string of at most 4000 bytes")
        if type(apply) is not bool:
            raise TypeError("apply must be a boolean")
        if not isinstance(editable_symbols, tuple) or any(
            not valid_symbol(name) for name in editable_symbols
        ) or len(set(editable_symbols)) != len(editable_symbols):
            raise ValueError("editable_symbols must contain unique function names")
        editable_symbols = editable_symbols or (symbol_name,)
        with self._locked():
            if self._disabled or self._healing:
                return RepairResult(False, message="Healing disabled or another repair is active.", status="busy")
            if apply and (retry_safe is not True or not self.config.retry_after_repair):
                return RepairResult(False, message="Activation requires retry_safe=True and retry_after_repair=True.")
            self._healing = True
        try:
            with self._locked():
                self._check("source preparation")
                target = self.language.source_file(module_name).resolve()
                target.relative_to(self.project_root)
                if target.suffix != ".py":
                    raise ValueError("Only Python source modules can be repaired")
                original = target.read_bytes()
                if len(original) > self.config.max_replacement_bytes:
                    raise ValueError("Source exceeds the configured repair size limit")
                self.language.prepare(module_name, original.decode("utf-8"), editable_symbols)
                self._emit("inspection_started", f"Inspecting {module_name}.{symbol_name} ({operation})", on_event=on_event)
                baseline = self._health(module_name, health_check)
                self._emit("baseline_health", baseline.describe(), on_event=on_event)
                if not baseline.checks:
                    raise ValueError("Register at least one developer health check before requesting a patch")
                failure = FailureContext(
                    ("ObservedFailure" if observed_failure else "LiveInspection") if baseline.ok else "HealthCheckFailure",
                    observed_failure or ("Developer requested live inspection" if baseline.ok else baseline.describe()),
                    "", module_name, symbol_name, target, "", "",
                )
                if not baseline.ok:
                    self.core.record_failure(str(target), failure.exception_type,
                                             redact(failure.message), module_name, symbol_name)
                request = RepairRequest(failure, original.decode("utf-8"), self.project_root,
                                        operation, instructions,
                                        baseline.describe() + ("\nObserved runtime failure: " + redact(observed_failure) if observed_failure else ""),
                                        editable_symbols, related_sources(self.project_root, target, context_files))

            def confirm():
                report = self._health(module_name, health_check)
                self._emit("activation_health", report.describe(), on_event=on_event)
                if not report.ok:
                    raise RuntimeError("Activated candidate failed developer health checks")
                return None

            result = self._repair(request, original, confirm=confirm, retry_safe=retry_safe,
                                  auto_apply=apply, preview=not apply, on_event=on_event,
                                  health_check=health_check)
        except (RepairTimeout, RepairCancelled) as exc:
            result = RepairResult(False, message=str(exc),
                                  status="cancelled" if isinstance(exc, RepairCancelled) else "timed_out")
        except CandidateIsolationError as exc:
            result = RepairResult(False, message=str(exc), status="failed")
        except Exception as exc:
            result = RepairResult(False, message=redact(
                f"Live inspection stopped ({type(exc).__name__}). Source activation was not completed."
            ), status="failed")
        finally:
            # This flag is owned by the repairing thread; do not reacquire an
            # expired lock to clear it. Concurrent callers observe it under lock.
            self._healing = False
            if "result" in locals():
                self.last_result = result
        if result.status not in ("timed_out", "cancelled"):
            try:
                self._emit("complete", result.message, proposal_id=result.proposal_id, on_event=on_event)
            except (RepairTimeout, RepairCancelled):
                pass  # Completion reporting cannot change an already final result.
        return result

    def call(self, *, module_name: str, symbol_name: str,
             args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None,
             retry_safe: bool = False) -> Any:
        self._sync_entry("await runtime.acall(...)", dispatch=True)
        with self._locked():
            function = self._sync_target(module_name, symbol_name)
            if self._healing or self._disabled:
                return function(*args, **(kwargs or {}))
            self.last_result = None
            try:
                return function(*args, **(kwargs or {}))
            except Exception as exc:
                key = (module_name, symbol_name)
                limited = self._auto_admit(key)
                if limited is not None:
                    self.last_result = limited
                    exc.add_note(limited.message)
                    raise
                self._healing = True
                try:
                    # Normal app execution is not timed. The repair budget starts
                    # when its original exception is captured, including the retry.
                    with RepairDeadline(self.config):
                        self.last_result = self._heal(
                            exc=exc, module_name=module_name, symbol_name=symbol_name,
                            args=args, kwargs=kwargs or {}, retry_safe=retry_safe,
                        )
                except RepairTimeout as timeout_exc:
                    self.last_result = RepairResult(False, message=str(timeout_exc), status="timed_out")
                except Exception as healing_exc:
                    self.last_result = RepairResult(
                        healed=False, message=f"Healing stopped ({type(healing_exc).__name__}). Original exception preserved.",
                    )
                finally:
                    self._healing = False
                    if self.last_result is not None:
                        self._auto_finished(key, self.last_result)
                if self.last_result.healed:
                    self._emit("complete", self.last_result.message, proposal_id=self.last_result.proposal_id)
                    return self.last_result.return_value
                self._emit("complete", self.last_result.message, proposal_id=self.last_result.proposal_id)
                exc.add_note(self.last_result.message)
                raise

    def _event(self, target, proposal_id: str, event: str, details: str) -> None:
        self.core.record_repair_event(str(target), proposal_id, event, redact(details))

    def _heal(self, *, exc, module_name, symbol_name, args, kwargs, retry_safe) -> RepairResult:
        self._check("exception capture")
        target = self.language.source_file(module_name).resolve()
        target.relative_to(self.project_root)
        if target.suffix != ".py":
            return RepairResult(False, message="Only Python source modules can be repaired.")
        failure = build_failure_context(exc=exc, module_name=module_name,
            symbol_name=symbol_name, target_file=target, args=args, kwargs=kwargs)
        self.core.record_failure(str(target), failure.exception_type,
                                 redact(failure.message), module_name, symbol_name)
        original = target.read_bytes()
        if len(original) > self.config.max_replacement_bytes:
            return RepairResult(False, message="Source exceeds the configured repair size limit.")
        source = original.decode("utf-8")
        self.language.prepare(module_name, source, (symbol_name,))
        self._emit("failure_detected", f"{module_name}.{symbol_name}: {failure.exception_type}")
        return self._repair(RepairRequest(failure, source, self.project_root,
                                         editable_symbols=(symbol_name,)), original,
                            confirm=lambda: self._guard(
                                lambda: self.handle(module_name, symbol_name)(*args, **kwargs), "live retry", allow_async=True),
                            retry_safe=retry_safe, auto_apply=self.config.auto_apply)

    def _repair(self, request, original, *, confirm, retry_safe,
                auto_apply, preview=False, on_event=None, health_check=None) -> RepairResult:
        target = request.failure.target_file
        source = request.source_code
        module_name = request.failure.module_name
        budget = current_deadline()
        self._check("provider generation")
        self._emit("provider_started", f"Asking {type(self.provider).__name__} to inspect {target.name}", on_event=on_event)
        proposal = self._guard(lambda: self.provider.propose_repair(request), "provider generation")
        if proposal is None:
            status = "no_change" if request.failure.exception_type == "LiveInspection" else "failed"
            return RepairResult(False, message="AI provider returned no repair proposal.", status=status)
        if not isinstance(proposal, RepairProposal):
            return RepairResult(False, message="Invalid repair proposal type.")
        if proposal.target_file.resolve() != target:
            return RepairResult(False, message="Rejected repair for a different target file.")
        replacement = proposal.replacement_source
        if (type(replacement) is not str or not replacement.strip() or "\x00" in replacement
                or len(replacement.encode("utf-8")) > self.config.max_replacement_bytes):
            return RepairResult(False, message="Invalid or oversized replacement source.")
        proposal_id = str(uuid.uuid4())
        if replacement.encode("utf-8") == original:
            status = "no_change" if request.failure.exception_type == "LiveInspection" else "failed"
            return RepairResult(False, proposal_id, "Provider proposed unchanged source.", status=status)
        relative = target.relative_to(self.project_root).as_posix()
        diff = "".join(unified_diff(source.splitlines(keepends=True),
                          replacement.splitlines(keepends=True),
                          fromfile="a/" + relative, tofile="b/" + relative))
        self._emit("candidate", str(proposal.explanation), proposal_id=proposal_id, diff=diff, on_event=on_event)
        try:
            validate_edit_scope(source, replacement, request.editable_symbols)
            updates = self.language.prepare(module_name, replacement, request.editable_symbols)
        except (ValueError, SyntaxError) as exc:
            self._emit("scope_rejected", str(exc), proposal_id=proposal_id, on_event=on_event)
            return RepairResult(False, proposal_id, "Candidate violated the editable function policy.", status="failed")
        if target.read_bytes() != original:
            return RepairResult(False, proposal_id, "Source changed while the provider was running.")
        if not context_unchanged(self.project_root, request.related_sources):
            return RepairResult(False, proposal_id, "Related source changed while the provider was running.")
        self._emit("verification_started", "Rust is verifying the candidate in a copied workspace.",
                   proposal_id=proposal_id, on_event=on_event)
        try:
            report = self.core.verify(str(target), replacement, proposal_id, budget.remaining())
        except Exception:
            self._check("candidate verification")
            raise
        self._check("candidate verification")
        self._emit("verification_finished", report.details, proposal_id=proposal_id, on_event=on_event)
        review = RepairReview(
            proposal_id=proposal_id, target_file=target,
            diff=diff,
            explanation=redact(str(proposal.explanation)), verified=report.ok,
            syntax_ok=report.syntax_ok, tests_ok=report.tests_ok,
            verification_details=redact(report.details),
        )

        def stopped(message, status="failed", snapshot_file=None):
            return RepairResult(False, proposal_id, message, review=review,
                                status=status, snapshot_file=snapshot_file)

        self._event(target, proposal_id, "repair_reviewed", f"verified={report.ok}")
        if not report.ok:
            return stopped("Candidate verification failed; source unchanged.")
        if preview:
            return stopped("Preview verified. No live patch applied; run without --preview to activate a fresh candidate.", "preview")
        if not auto_apply:
            if self.reviewer is None:
                return stopped("Review required. Inspect last_result.review and configure a reviewer callback.")
            approved = self._guard(lambda: self.reviewer(review), "developer review") is True
            self._event(target, proposal_id, "repair_approved" if approved else "repair_rejected", "Developer review decision")
            if not approved:
                return stopped("Repair was not approved; source unchanged.")
        if retry_safe is not True or not self.config.retry_after_repair:
            return stopped("Activation requires retry_safe=True and retry_after_repair=True; source unchanged.")
        # All routed calls pause during final verification, swap, health and rollback.
        with self._locked():
            self._check("activation preparation")
            if target.read_bytes() != original:
                return stopped("Source changed after review; candidate discarded.")
            if not context_unchanged(self.project_root, request.related_sources):
                return stopped("Related source changed after review; candidate discarded.")
            updates = self.language.prepare(module_name, replacement, request.editable_symbols)
            module = importlib.import_module(module_name)
            original_bindings = [resolve_binding(module, name) for name in request.editable_symbols]
            if health_check is not None:
                candidate_health_report = candidate_health(health_check, module, self.project_root, budget,
                                                           updates=updates)
                self._emit("candidate_health", candidate_health_report.describe(), proposal_id=proposal_id, on_event=on_event)
                if not candidate_health_report.ok:
                    return stopped("Candidate failed developer health checks; live source and functions unchanged.")
                if target.read_bytes() != original:
                    return stopped("Source changed during candidate health checks; candidate discarded.")
                if not context_unchanged(self.project_root, request.related_sources):
                    return stopped("Related source changed during candidate health checks; candidate discarded.")
            original_codes = [(function, function.__code__) for function, _ in updates]
            self._emit("activation_started", "Final verification, snapshot and source replacement.",
                       proposal_id=proposal_id, on_event=on_event)
            self._check("final verification")
            budget.arm_recovery(target, self.project_root / self.config.snapshot_dir / proposal_id /
                                target.relative_to(self.project_root), original, replacement)
            try:
                receipt = self.core.verify_and_apply(str(target), replacement, proposal_id, source, budget.remaining())
            except BaseException as native_exc:
                try:
                    source_intact = target.read_bytes() == original
                except OSError:
                    source_intact = False
                if not source_intact:
                    self._disabled = True
                if not isinstance(native_exc, Exception):
                    raise
                timed_out = budget.remaining() <= 0
                return stopped(
                    "Repair deadline exceeded during final verification; original source unchanged." if timed_out and source_intact else
                    "Native verification/application failed; original source confirmed unchanged." if source_intact else
                    "Native application failed and original source could not be confirmed; healing disabled. Restart the application.",
                    ("timed_out" if timed_out else "failed") if source_intact else "disabled",
                )
            try:
                self._check("function activation")
                self.language.activate(module_name, request.editable_symbols)
                self._emit("patch_applied", f"Updated {module_name} functions; snapshot: {receipt.snapshot_file}",
                           proposal_id=proposal_id, on_event=on_event)
                # confirm orchestrates health/retry; each user callback guards
                # itself. Never trace a thread while it waits for the app loop.
                value = confirm()
                if any(not binding.current() for binding in original_bindings):
                    raise RuntimeError("An editable function was rebound during activation checks")
                if target.read_bytes() != replacement.encode("utf-8"):
                    raise RuntimeError("Source changed during activation or health checks")
                if not context_unchanged(self.project_root, request.related_sources):
                    raise RuntimeError("Related source changed during activation or health checks")
                self._check("commit")
                self.core.commit(receipt.target_file, receipt.proposal_id)
                budget.committed()
            except BaseException as retry_exc:
                reason = str(retry_exc) if isinstance(retry_exc, (RepairTimeout, RepairCancelled)) else \
                    f"Activation, retry or commit failed ({type(retry_exc).__name__})."
                with budget.cleanup():
                    # Restore first. Terminal callbacks must never delay recovery.
                    restoration_failed = False
                    try:
                        self.core.rollback(receipt.target_file, receipt.snapshot_file, receipt.proposal_id, reason)
                    except BaseException:
                        restoration_failed = True
                    try:
                        for function, code in original_codes:
                            function.__code__ = code
                        for binding in original_bindings:
                            binding.restore()
                    except BaseException:
                        restoration_failed = True
                    if restoration_failed:
                        self._disabled = True
                        reason += " Source or function-code restoration failed; healing disabled. Restart the application."
                    else:
                        budget.committed()
                        reason += " Original source restored. External side effects are not undone."
                    if not isinstance(retry_exc, RepairTimeout):
                        self._emit("rollback_finished", reason, proposal_id=proposal_id, on_event=on_event)
                if not isinstance(retry_exc, Exception):
                    raise
                return stopped(reason, "disabled" if self._disabled else
                               ("timed_out" if isinstance(retry_exc, RepairTimeout) else
                                "cancelled" if isinstance(retry_exc, RepairCancelled) else "rolled_back"), receipt.snapshot_file)
        message = "Repair verified, activated and retry confirmed." if request.operation == "repair" else "Live patch committed after all developer health checks passed."
        try:
            self._emit("committed", message, proposal_id=proposal_id, on_event=on_event)
        except (RepairTimeout, RepairCancelled):
            pass
        return RepairResult(True, proposal_id, message, value, review, "applied", receipt.snapshot_file)
