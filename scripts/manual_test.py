"""User-run healing checks. Default: offline mock, explicit review approval.

python scripts/manual_test.py
python scripts/manual_test.py --provider groq
python scripts/manual_test.py --provider mock --scenario rollback --auto-apply
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile
import uuid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("mock", "groq", "openai", "auto"), default="mock")
    parser.add_argument("--scenario", choices=("heal", "rollback"), default="heal")
    parser.add_argument("--auto-apply", action="store_true", help="Skip the interactive approval step")
    args = parser.parse_args()
    if args.scenario == "rollback" and args.provider != "mock":
        parser.error("The controlled rollback scenario uses --provider mock")

    try:
        from selfheal import SelfHealConfig, SelfHealRuntime
        from selfheal.models import RepairProposal
        from selfheal.providers import AIProvider, MockAIProvider, provider_from_env
    except ImportError:
        print("SelfHeal is not built in this Python environment.")
        print('Activate .venv and run: python -m pip install -e ".[dev]"')
        return 2

    class RetryFailureProvider(AIProvider):
        def propose_repair(self, request):
            return RepairProposal(
                request.failure.target_file,
                "def divide(a, b):\n"
                "    if b == 0:\n"
                "        raise RuntimeError('Intentional retry failure')\n"
                "    return a / b\n",
                "Deliberately pass normal-input tests but fail the retry, to demonstrate rollback.",
            )

    try:
        if args.scenario == "rollback":
            provider = RetryFailureProvider()
        elif args.provider == "mock":
            provider = MockAIProvider()
        else:
            provider = provider_from_env(priority=None if args.provider == "auto" else (args.provider,))
    except ValueError as exc:
        print("Provider configuration:", exc)
        return 2

    def review(candidate):
        print("\nPROPOSED DIFF\n" + candidate.diff)
        print("Explanation:", candidate.explanation)
        print("Verification:", candidate.verification_details)
        try:
            return input("Apply this exact change and retry? [y/N] ").strip().lower() == "y"
        except EOFError:
            return False

    # Keep all repaired code, regression tests, snapshots and logs disposable.
    with tempfile.TemporaryDirectory(prefix="selfheal-manual-") as directory:
        root = Path(directory)
        name = "calculator_" + uuid.uuid4().hex
        target = root / (name + ".py")
        original = b"def divide(a, b):\n    return a / b\n"
        target.write_bytes(original)
        test_source = (
            f"from {name} import divide\n"
            "def test_normal():\n    assert divide(8, 2) == 4\n"
        )
        if args.scenario == "heal":
            test_source += "def test_zero():\n    assert divide(10, 0) is None\n"
        (root / "test_calculator.py").write_text(test_source, encoding="utf-8")
        sys.path.insert(0, directory)
        try:
            runtime = SelfHealRuntime(SelfHealConfig(root, auto_apply=args.auto_apply),
                                      provider=provider, reviewer=review)
            print("Provider:", type(provider).__name__, "| Process ID:", os.getpid())
            print("Workspace:", root)
            print("1. Healthy call divide(8, 2):", runtime.call(module_name=name, symbol_name="divide", args=(8, 2)))
            print("2. Triggering divide(10, 0) in the same running process...")
            failed = False
            try:
                value = runtime.call(module_name=name, symbol_name="divide", args=(10, 0), retry_safe=True)
                print("Returned:", value)
            except ZeroDivisionError:
                failed = True
                print("Original ZeroDivisionError preserved.")
            result = runtime.last_result
            print("Healing status:", result.message)
            if result.review:
                print("Verification:", result.review.verification_details)
            journal = (root / ".selfheal/audit.jsonl").read_text(encoding="utf-8")
            print("\nAUDIT JOURNAL\n" + journal)
            if args.scenario == "rollback":
                passed = failed and target.read_bytes() == original and '"repair_rolled_back"' in journal
                print("ROLLBACK CHECK:", "PASS" if passed else "NOT COMPLETED")
            else:
                passed = not failed and result.healed and value is None
                print("HEALING CHECK:", "PASS" if passed else "NOT COMPLETED (see status above)")
            print("3. Next healthy call divide(12, 3):", runtime.call(module_name=name, symbol_name="divide", args=(12, 3)))
            print("Process ID is still:", os.getpid())
            print("Temporary demo code and logs will be removed; your project source is untouched.")
            return 0 if passed else 1
        finally:
            sys.path.remove(directory)
            sys.modules.pop(name, None)


if __name__ == "__main__":
    raise SystemExit(main())
