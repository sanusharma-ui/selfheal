from __future__ import annotations

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEMO_ROOT = Path(__file__).resolve().parent

# The package itself should be installed with pip/maturin.
# Only the demo module needs to be added to sys.path.
sys.path.insert(0, str(DEMO_ROOT))

from selfheal import SelfHealConfig, SelfHealRuntime
from selfheal.providers import MockAIProvider


runtime = SelfHealRuntime(
    SelfHealConfig(
        project_root=PROJECT_ROOT,
        run_tests=False,
        auto_apply=True,
    ),
    provider=MockAIProvider(),
)

print("Calling calculator.divide(10, 0)")
print("The initial implementation raises ZeroDivisionError.")

result = runtime.call(
    module_name="calculator",
    symbol_name="divide",
    args=(10, 0),
    retry_safe=True,
)

print("Result after healing:", result)
print("Audit log:", PROJECT_ROOT / ".selfheal" / "audit.jsonl")
