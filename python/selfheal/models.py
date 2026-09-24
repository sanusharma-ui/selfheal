from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class FailureContext:
    exception_type: str
    message: str
    traceback_text: str
    module_name: str
    symbol_name: str
    target_file: Path
    args_repr: str
    kwargs_repr: str
    timestamp: str = field(default_factory=utc_now_iso)


@dataclass(slots=True)
class RepairRequest:
    failure: FailureContext
    source_code: str
    project_root: Path


@dataclass(slots=True)
class RepairProposal:
    target_file: Path
    replacement_source: str
    explanation: str = ""
    proposal_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(slots=True)
class RepairResult:
    healed: bool
    proposal_id: str | None = None
    message: str = ""
    return_value: Any = None
    review: RepairReview | None = None


@dataclass(frozen=True, slots=True)
class RepairReview:
    """Immutable local review of the exact candidate that may be activated."""
    proposal_id: str
    target_file: Path
    diff: str
    explanation: str
    verified: bool
    syntax_ok: bool
    tests_ok: bool | None
    verification_details: str
