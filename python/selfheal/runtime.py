from __future__ import annotations

from collections.abc import Callable
from difflib import unified_diff
from threading import RLock
from types import ModuleType
from typing import Any
import importlib
import uuid

from .config import SelfHealConfig
from .languages import PythonRuntimeAdapter
from .models import (FailureContext, HealthCheckResult, HealthReport, RepairEvent,
                     RepairProposal, RepairRequest, RepairResult, RepairReview)
from .observer import build_failure_context
from .privacy import redact
from .providers import AIProvider, provider_from_env
from .registry import FunctionRegistry
from .scope import validate_edit_scope

try:
    from ._core import CoreEngine
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "SelfHeal native extension is unavailable. Install a wheel for your Python/OS with "
        "'python -m pip install selfheal-runtime', or build this checkout using the release guide."
    ) from exc


class SelfHealRuntime:
    """One protected operation, one candidate, one explicitly safe retry.

    reviewer receives a frozen review and must return exactly True to approve.
    Calls through this instance are serialized. Other runtimes/workers and direct
    calls are outside this lock; multi-worker activation remains unsupported.
    """

    def __init__(self, config: SelfHealConfig, *, provider: AIProvider | None = None,
                 reviewer: Callable[[RepairReview], bool] | None = None,
                 event_sink: Callable[[RepairEvent], None] | None = None):
        self.config = config
        self.project_root = config.normalized_root()
        self.provider = provider if provider is not None else provider_from_env()
        self.reviewer = reviewer
        self.event_sink = event_sink
        self.registry = FunctionRegistry()
        self.language = PythonRuntimeAdapter()
        self.last_result: RepairResult | None = None
        self._lock = RLock()
        self._healing = False
        self._disabled = False
        self.core = CoreEngine(str(self.project_root), config.python_executable,
                               config.run_tests, config.snapshot_dir, config.audit_file)

    def handle(self, module_name: str, symbol_name: str):
        return self.registry.handle(module_name, symbol_name)

    def invoke(self, *, module_name: str, symbol_name: str,
               args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None) -> Any:
        """Dispatch current code under the activation lock, without automatic repair."""
        with self._lock:
            return self.handle(module_name, symbol_name)(*args, **(kwargs or {}))

    @property
    def healing_disabled(self) -> bool:
        return self._disabled

    def _emit(self, phase, message, *, proposal_id=None, diff="", on_event=None):
        event = RepairEvent(phase, redact(message), proposal_id, redact(diff))
        for sink in (self.event_sink, on_event):
            if sink is not None:
                try:
                    sink(event)
                except Exception:
                    # A disconnected terminal must not interrupt recovery.
                    pass

    def _health(self, module_name, check, *, module=None) -> HealthReport:
        try:
            report = check(module if module is not None else importlib.import_module(module_name))
            if not isinstance(report, HealthReport) or any(
                not isinstance(item, HealthCheckResult) for item in report.checks
            ):
                raise TypeError("health_check must return a HealthReport")
            return report
        except Exception as exc:
            return HealthReport((HealthCheckResult(
                "health callback", False, redact(f"{type(exc).__name__}: {exc}")),))

    def check_health(self, module_name: str, check: Callable[[ModuleType], HealthReport]) -> HealthReport:
        """Run local developer checks without making an AI request or mutating source."""
        with self._lock:
            return self._health(module_name, check)

    def inspect(self, *, module_name: str, symbol_name: str,
                health_check: Callable[[ModuleType], HealthReport],
                retry_safe: bool = False, operation: str = "heal",
                instructions: str = "", editable_symbols: tuple[str, ...] = (),
                observed_failure: str = "",
                apply: bool = False,
                on_event: Callable[[RepairEvent], None] | None = None) -> RepairResult:
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
            type(name) is not str or not name.isidentifier() for name in editable_symbols
        ) or len(set(editable_symbols)) != len(editable_symbols):
            raise ValueError("editable_symbols must contain unique function names")
        editable_symbols = editable_symbols or (symbol_name,)
        with self._lock:
            if self._disabled or self._healing:
                return RepairResult(False, message="Healing disabled or another repair is active.", status="busy")
            if apply and (retry_safe is not True or not self.config.retry_after_repair):
                return RepairResult(False, message="Activation requires retry_safe=True and retry_after_repair=True.")
            self._healing = True
        try:
            with self._lock:
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
                                        editable_symbols)

            def confirm():
                report = self._health(module_name, health_check)
                self._emit("activation_health", report.describe(), on_event=on_event)
                if not report.ok:
                    raise RuntimeError("Activated candidate failed developer health checks")
                return None

            result = self._repair(request, original, confirm=confirm, retry_safe=retry_safe,
                                  auto_apply=apply, preview=not apply, on_event=on_event,
                                  health_check=health_check)
        except Exception as exc:
            result = RepairResult(False, message=redact(
                f"Live inspection stopped ({type(exc).__name__}). Source activation was not completed."
            ), status="failed")
        finally:
            with self._lock:
                self._healing = False
                if "result" in locals():
                    self.last_result = result
        self._emit("complete", result.message, proposal_id=result.proposal_id, on_event=on_event)
        return result

    def call(self, *, module_name: str, symbol_name: str,
             args: tuple[Any, ...] = (), kwargs: dict[str, Any] | None = None,
             retry_safe: bool = False) -> Any:
        with self._lock:
            if self._healing or self._disabled:
                return self.handle(module_name, symbol_name)(*args, **(kwargs or {}))
            self.last_result = None
            try:
                return self.handle(module_name, symbol_name)(*args, **(kwargs or {}))
            except Exception as exc:
                self._healing = True
                try:
                    self.last_result = self._heal(
                        exc=exc, module_name=module_name, symbol_name=symbol_name,
                        args=args, kwargs=kwargs or {}, retry_safe=retry_safe,
                    )
                except Exception as healing_exc:
                    self.last_result = RepairResult(
                        healed=False, message=f"Healing stopped ({type(healing_exc).__name__}). Original exception preserved.",
                    )
                finally:
                    self._healing = False
                if self.last_result.healed:
                    self._emit("complete", self.last_result.message, proposal_id=self.last_result.proposal_id)
                    return self.last_result.return_value
                self._emit("complete", self.last_result.message, proposal_id=self.last_result.proposal_id)
                exc.add_note(self.last_result.message)
                raise

    def _event(self, target, proposal_id: str, event: str, details: str) -> None:
        self.core.record_repair_event(str(target), proposal_id, event, redact(details))

    def _heal(self, *, exc, module_name, symbol_name, args, kwargs, retry_safe) -> RepairResult:
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
                            confirm=lambda: self.handle(module_name, symbol_name)(*args, **kwargs),
                            retry_safe=retry_safe, auto_apply=self.config.auto_apply)

    def _repair(self, request, original, *, confirm, retry_safe,
                auto_apply, preview=False, on_event=None, health_check=None) -> RepairResult:
        target = request.failure.target_file
        source = request.source_code
        module_name = request.failure.module_name
        self._emit("provider_started", f"Asking {type(self.provider).__name__} to inspect {target.name}", on_event=on_event)
        proposal = self.provider.propose_repair(request)
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
        self._emit("verification_started", "Rust is verifying the candidate in a copied workspace.",
                   proposal_id=proposal_id, on_event=on_event)
        report = self.core.verify(str(target), replacement, proposal_id)
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
            approved = self.reviewer(review) is True
            self._event(target, proposal_id, "repair_approved" if approved else "repair_rejected", "Developer review decision")
            if not approved:
                return stopped("Repair was not approved; source unchanged.")
        if retry_safe is not True or not self.config.retry_after_repair:
            return stopped("Activation requires retry_safe=True and retry_after_repair=True; source unchanged.")
        # All routed calls pause during final verification, swap, health and rollback.
        with self._lock:
            if target.read_bytes() != original:
                return stopped("Source changed after review; candidate discarded.")
            updates = self.language.prepare(module_name, replacement, request.editable_symbols)
            module = importlib.import_module(module_name)
            original_bindings = {name: getattr(module, name) for name in request.editable_symbols}
            if health_check is not None:
                candidate = self.language.candidate_module(module_name, updates)
                candidate_health = self._health(module_name, health_check, module=candidate)
                self._emit("candidate_health", candidate_health.describe(), proposal_id=proposal_id, on_event=on_event)
                if not candidate_health.ok:
                    return stopped("Candidate failed developer health checks; live source and functions unchanged.")
                if target.read_bytes() != original:
                    return stopped("Source changed during candidate health checks; candidate discarded.")
            original_codes = [(function, function.__code__) for function, _ in updates]
            self._emit("activation_started", "Final verification, snapshot and source replacement.",
                       proposal_id=proposal_id, on_event=on_event)
            try:
                receipt = self.core.verify_and_apply(str(target), replacement, proposal_id, source)
            except BaseException as native_exc:
                try:
                    source_intact = target.read_bytes() == original
                except OSError:
                    source_intact = False
                if not source_intact:
                    self._disabled = True
                if not isinstance(native_exc, Exception):
                    raise
                return stopped(
                    "Native verification/application failed; original source confirmed unchanged." if source_intact else
                    "Native application failed and original source could not be confirmed; healing disabled. Restart the application.",
                    "failed" if source_intact else "disabled",
                )
            try:
                self.language.activate(module_name, request.editable_symbols)
                self._emit("patch_applied", f"Updated {module_name} functions; snapshot: {receipt.snapshot_file}",
                           proposal_id=proposal_id, on_event=on_event)
                value = confirm()
                if any(getattr(module, name, None) is not function for name, function in original_bindings.items()):
                    raise RuntimeError("An editable function was rebound during activation checks")
                if target.read_bytes() != replacement.encode("utf-8"):
                    raise RuntimeError("Source changed during activation or health checks")
                self.core.commit(receipt.target_file, receipt.proposal_id)
            except BaseException as retry_exc:
                reason = f"Activation, retry or commit failed ({type(retry_exc).__name__})."
                self._emit("rollback_started", reason, proposal_id=proposal_id, on_event=on_event)
                try:
                    self.core.rollback(receipt.target_file, receipt.snapshot_file, receipt.proposal_id, reason)
                    for function, code in original_codes:
                        function.__code__ = code
                    for name, function in original_bindings.items():
                        setattr(module, name, function)
                except BaseException:
                    self._disabled = True
                    reason += " Source or function-code restoration failed; healing disabled. Restart the application."
                else:
                    reason += " Original source restored. External side effects are not undone."
                self._emit("rollback_finished", reason, proposal_id=proposal_id, on_event=on_event)
                if not isinstance(retry_exc, Exception):
                    raise
                return stopped(reason, "disabled" if self._disabled else "rolled_back", receipt.snapshot_file)
        message = "Repair verified, activated and retry confirmed." if request.operation == "repair" else "Live patch committed after all developer health checks passed."
        self._emit("committed", message, proposal_id=proposal_id, on_event=on_event)
        return RepairResult(True, proposal_id, message, value, review, "applied", receipt.snapshot_file)
