from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import math
import os
import sys
import tomllib

from dotenv import load_dotenv

from .errors import ConfigurationError


@dataclass(slots=True)
class SelfHealConfig:
    project_root: Path
    run_tests: bool = True
    auto_apply: bool = False
    retry_after_repair: bool = True
    python_executable: str = sys.executable
    snapshot_dir: str = ".selfheal/snapshots"
    audit_file: str = ".selfheal/audit.jsonl"
    max_replacement_bytes: int = 128_000
    repair_timeout_seconds: float | None = None
    timeout_cleanup_seconds: float | None = None
    hard_timeout: bool | None = None
    max_auto_repairs_per_hour: int = 6
    repair_failure_cooldown_seconds: float = 60

    def __post_init__(self) -> None:
        self.project_root = Path(self.project_root).resolve()
        if not self.project_root.is_dir():
            raise ConfigurationError("project_root must be an existing application directory")
        for name in ("run_tests", "auto_apply", "retry_after_repair"):
            if type(getattr(self, name)) is not bool:
                raise ConfigurationError(f"{name} must be a boolean")
        if type(self.python_executable) is not str or not self.python_executable.strip():
            raise ConfigurationError("python_executable must identify the application's Python interpreter")
        for name in ("snapshot_dir", "audit_file"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ConfigurationError(f"{name} must be a nonempty relative path inside project_root")
            path = Path(value)
            if path.is_absolute() or path.drive or ".." in path.parts:
                raise ConfigurationError(f"{name} must be a relative path inside project_root")
            try:
                (self.project_root / path).resolve().relative_to(self.project_root)
            except ValueError:
                raise ConfigurationError(f"{name} must stay inside project_root") from None
        if type(self.max_replacement_bytes) is not int or self.max_replacement_bytes <= 0:
            raise ConfigurationError("max_replacement_bytes must be a positive integer")
        if type(self.max_auto_repairs_per_hour) is not int or self.max_auto_repairs_per_hour < 1:
            raise ConfigurationError("max_auto_repairs_per_hour must be a positive integer")
        if (type(self.repair_failure_cooldown_seconds) not in (int, float)
                or not math.isfinite(self.repair_failure_cooldown_seconds)
                or self.repair_failure_cooldown_seconds < 0):
            raise ConfigurationError("repair_failure_cooldown_seconds must be a finite nonnegative number")
        # Explicit constructor/TOML values win; process env wins over cwd/.env.
        load_dotenv(Path.cwd() / ".env", override=False, interpolate=False, encoding="utf-8-sig")
        for field, variable, default, minimum, maximum in (
            ("repair_timeout_seconds", "SELFHEAL_REPAIR_TIMEOUT_SECONDS", 180.0, 1.0, 3600.0),
            ("timeout_cleanup_seconds", "SELFHEAL_TIMEOUT_CLEANUP_SECONDS", 5.0, 0.1, 60.0),
        ):
            value = getattr(self, field)
            if value is None:
                try:
                    value = float(os.environ.get(variable, str(default)))
                except (ValueError, TypeError):
                    raise ConfigurationError(f"{variable} must be a finite number between {minimum} and {maximum}") from None
            if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
                raise ConfigurationError(f"{variable} must be a finite number between {minimum} and {maximum}")
            setattr(self, field, float(value))
        if self.hard_timeout is None:
            value = os.environ.get("SELFHEAL_HARD_TIMEOUT", "true").strip().lower()
            if value not in ("true", "false"):
                raise ConfigurationError("SELFHEAL_HARD_TIMEOUT must be true or false")
            self.hard_timeout = value == "true"
        elif type(self.hard_timeout) is not bool:
            raise ConfigurationError("hard_timeout must be a boolean")

    def normalized_root(self) -> Path:
        return self.project_root.resolve()

    @classmethod
    def from_toml(cls, path: Path) -> SelfHealConfig:
        path = Path(path).resolve()
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("runtime", {})
        root = Path(data.pop("project_root", "."))
        return cls(project_root=path.parent / root, **data)


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Background AI access and mutation are separately configurable."""
    interval_seconds: float = 300
    auto_apply: bool = False
    scan_when_healthy: bool = False
    run_on_start: bool = False
    max_attempts_per_hour: int = 6
    failure_backoff_seconds: float = 60
    max_consecutive_failures: int = 3
    improvement_instructions: str = ""

    def __post_init__(self):
        import math
        for name in ("interval_seconds", "failure_backoff_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 1:
                raise ValueError(f"{name} must be a finite number of at least 1 second")
        for name in ("max_attempts_per_hour", "max_consecutive_failures"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("auto_apply", "scan_when_healthy", "run_on_start"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if type(self.improvement_instructions) is not str or len(self.improvement_instructions.encode("utf-8")) > 4000:
            raise ValueError("improvement_instructions must be a string of at most 4000 bytes")

    @classmethod
    def from_toml(cls, path: Path) -> AgentConfig:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**data.get("agent", {}))
