from __future__ import annotations

from collections.abc import Callable
from difflib import unified_diff
from threading import RLock
from typing import Any
import uuid

from .config import SelfHealConfig
from .languages import PythonRuntimeAdapter
from .models import RepairProposal, RepairRequest, RepairResult, RepairReview
from .observer import build_failure_context
from .privacy import redact
from .providers import AIProvider, provider_from_env
from .registry import FunctionRegistry

try:
    from ._core import CoreEngine
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "SelfHeal Rust extension is not available. Install with 'pip install -e .'."
    ) from exc


class SelfHealRuntime:
    """One protected operation, one candidate, one explicitly safe retry.

    reviewer receives a frozen review and must return exactly True to approve.
    Calls through this instance are serialized. Other runtimes/workers and direct
    calls are outside this lock; multi-worker activation remains unsupported.
    """

    def __init__(self, config: SelfHealConfig, *, provider: AIProvider | None = None,
                 reviewer: Callable[[RepairReview], bool] | None = None):
        self.config = config
        self.project_root = config.normalized_root()
        self.provider = provider if provider is not None else provider_from_env()
        self.reviewer = reviewer
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
                    return self.last_result.return_value
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
        proposal = self.provider.propose_repair(RepairRequest(failure, source, self.project_root))
        if proposal is None:
            return RepairResult(False, message="AI provider returned no repair proposal.")
        if not isinstance(proposal, RepairProposal):
            return RepairResult(False, message="Invalid repair proposal type.")
        if proposal.target_file.resolve() != target:
            return RepairResult(False, message="Rejected repair for a different target file.")
        replacement = proposal.replacement_source
        if (type(replacement) is not str or not replacement.strip() or "\x00" in replacement
                or len(replacement.encode("utf-8")) > self.config.max_replacement_bytes):
            return RepairResult(False, message="Invalid or oversized replacement source.")
        proposal_id = str(uuid.uuid4())
        if target.read_bytes() != original:
            return RepairResult(False, proposal_id, "Source changed while the provider was running.")
        report = self.core.verify(str(target), replacement, proposal_id)
        relative = target.relative_to(self.project_root).as_posix()
        review = RepairReview(
            proposal_id=proposal_id, target_file=target,
            diff="".join(unified_diff(source.splitlines(keepends=True),
                         replacement.splitlines(keepends=True),
                         fromfile="a/" + relative, tofile="b/" + relative)),
            explanation=redact(str(proposal.explanation)), verified=report.ok,
            syntax_ok=report.syntax_ok, tests_ok=report.tests_ok,
            verification_details=redact(report.details),
        )

        def stopped(message):
            return RepairResult(False, proposal_id, message, review=review)

        self._event(target, proposal_id, "repair_reviewed", f"verified={report.ok}")
        if not report.ok:
            return stopped("Candidate verification failed; source unchanged.")
        if not self.config.auto_apply:
            if self.reviewer is None:
                return stopped("Review required. Inspect last_result.review and configure a reviewer callback.")
            approved = self.reviewer(review) is True
            self._event(target, proposal_id, "repair_approved" if approved else "repair_rejected", "Developer review decision")
            if not approved:
                return stopped("Repair was not approved; source unchanged.")
        if retry_safe is not True or not self.config.retry_after_repair:
            return stopped("Activation requires retry_safe=True and retry_after_repair=True; source unchanged.")
        if target.read_bytes() != original:
            return stopped("Source changed after review; candidate discarded.")
        receipt = self.core.verify_and_apply(str(target), replacement, proposal_id, source)
        try:
            self.language.activate(module_name)
            value = self.handle(module_name, symbol_name)(*args, **kwargs)
            self.core.commit(receipt.target_file, receipt.proposal_id)
        except BaseException as retry_exc:
            reason = f"Activation, retry or commit failed ({type(retry_exc).__name__})."
            try:
                self.core.rollback(receipt.target_file, receipt.snapshot_file, receipt.proposal_id, reason)
                self.language.activate(module_name)
            except BaseException:
                self._disabled = True
                reason += " Rollback or restored-module reload failed; healing disabled. Restart the application."
            else:
                reason += " Original source restored. External side effects are not undone."
            if not isinstance(retry_exc, Exception):
                raise
            return stopped(reason)
        return RepairResult(True, proposal_id, "Repair verified, activated and retry confirmed.", value, review)
