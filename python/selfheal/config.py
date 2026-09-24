from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


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

    def __post_init__(self) -> None:
        if type(self.max_replacement_bytes) is not int or self.max_replacement_bytes <= 0:
            raise ValueError("max_replacement_bytes must be a positive integer")

    def normalized_root(self) -> Path:
        return self.project_root.resolve()
