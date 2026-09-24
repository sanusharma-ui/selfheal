"""Run a real-provider (or offline mock) healing transaction on disposable code."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
import uuid

from selfheal import SelfHealConfig, SelfHealRuntime
from selfheal.providers import MockAIProvider, provider_from_env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="Use the offline deterministic provider")
    parser.add_argument("--auto-apply", action="store_true", help="Explicitly enable automatic verified activation")
    args = parser.parse_args()
    provider = MockAIProvider() if args.mock else provider_from_env()

    def review(candidate):
        print(candidate.diff)
        print("Explanation:", candidate.explanation)
        print("Verification:", candidate.verification_details)
        try:
            return input("Apply this exact repair and retry? [y/N] ").strip().lower() == "y"
        except EOFError:
            return False

    with tempfile.TemporaryDirectory(prefix="selfheal-demo-") as directory:
        root = Path(directory)
        name = "calculator_" + uuid.uuid4().hex
        (root / (name + ".py")).write_text("def divide(a, b):\n    return a / b\n", encoding="utf-8")
        (root / "test_calculator.py").write_text(
            f"from {name} import divide\n"
            "def test_normal():\n    assert divide(8, 2) == 4\n"
            "def test_zero():\n    assert divide(10, 0) is None\n", encoding="utf-8",
        )
        sys.path.insert(0, directory)
        try:
            runtime = SelfHealRuntime(SelfHealConfig(root, auto_apply=args.auto_apply),
                                      provider=provider, reviewer=review)
            print("Process:", os.getpid(), "Provider:", type(provider).__name__)
            try:
                result = runtime.call(module_name=name, symbol_name="divide", args=(10, 0), retry_safe=True)
            except ZeroDivisionError:
                print("Original operation failed:", runtime.last_result.message)
                if runtime.last_result.review:
                    print(runtime.last_result.review.verification_details)
                return 1
            print("Healed result:", result)
            print("Next call:", runtime.call(module_name=name, symbol_name="divide", args=(8, 2)))
            print("Same process:", os.getpid())
            print("Audit journal:")
            print((root / ".selfheal/audit.jsonl").read_text(encoding="utf-8"))
            print("Disposable demo workspace will now be removed.")
            return 0
        finally:
            sys.path.remove(directory)
            sys.modules.pop(name, None)


if __name__ == "__main__":
    raise SystemExit(main())
