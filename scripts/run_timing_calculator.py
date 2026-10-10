"""Isolated, USER-RUN calculator fixture. Never imported by the SDK.

python scripts/run_timing_calculator.py --scenario all --timeout 10
Default heal uses your .env budget, deterministic offline provider, no API call.
--provider auto performs the ordinary heal with your own cloud configuration.
"""
from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

BROKEN = '''def calculate(left, operation, right):
    if operation == "+":
        return left + right
    if operation == "-":
        return left + right
    if operation == "*":
        return left * right
    if operation == "/":
        return left / right
    raise ValueError("Unsupported operation")
'''
FIXED = BROKEN.replace('if operation == "-":\n        return left + right',
                       'if operation == "-":\n        return left - right').replace(
    'return left / right', 'return None if right == 0 else left / right')
SCENARIOS = ("heal", "candidate-timeout", "retry-timeout", "global-timeout", "hard-timeout")


def calculator_health(module):
    from selfheal import HealthCheckResult, HealthReport
    checks = []
    for left, operation, right, expected in ((2, "+", 3, 5), (8, "-", 3, 5),
            (3, "-", 8, -5), (6, "*", 7, 42), (8, "/", 2, 4), (10, "/", 0, None)):
        try:
            actual = module.calculate(left, operation, right)
            ok = actual == expected
            detail = f"{left} {operation} {right}: expected {expected!r}, got {actual!r}"
        except Exception as exc:
            ok, detail = False, f"{left} {operation} {right}: {type(exc).__name__}"
        checks.append(HealthCheckResult("calculator arithmetic", ok, detail))
    return HealthReport(tuple(checks))


def run_case(root, scenario, timeout, provider_name):
    from selfheal import SelfHealConfig, SelfHealRuntime, TerminalReporter
    from selfheal.models import RepairProposal
    from selfheal.providers import AIProvider, provider_from_env

    class FixtureProvider(AIProvider):
        def propose_repair(self, request):
            if scenario == "global-timeout":
                # Spend budget across generation AND review, rather than one
                # slow stage. Each individual delay is below the total budget.
                from selfheal.timing import current_deadline
                time.sleep(current_deadline().total * 0.55)
            source = FIXED
            if scenario in ("candidate-timeout", "retry-timeout"):
                source = 'def calculate(left, operation, right):\n    while True:\n        pass\n'
            if scenario == "hard-timeout":
                source = ('def calculate(left, operation, right):\n'
                          '    __import__("time").sleep(7200)\n    return 0\n')
            return RepairProposal(request.failure.target_file, source, "Isolated manual calculator candidate")

    name = "timing_calc_" + uuid.uuid4().hex
    target = root / (name + ".py")
    target.write_text(BROKEN, encoding="utf-8")
    # Stable marker lets the parent verify watchdog restoration after child exit.
    (root / "target.txt").write_text(target.name, encoding="utf-8")
    sys.path.insert(0, str(root))
    try:
        module = importlib.import_module(name)
        saved = module.calculate
        print(f"\nScenario: {scenario} | BEFORE: 8 - 3 = {saved(8, '-', 3)} (bug; expected 5)", flush=True)
        config = SelfHealConfig(root, run_tests=False, auto_apply=True,
                               repair_timeout_seconds=timeout,
                               hard_timeout=True if scenario == "hard-timeout" else None)
        print(f"Total budget: {config.repair_timeout_seconds:g}s | hard timeout: {config.hard_timeout}", flush=True)

        def review(_):
            if scenario == "global-timeout":
                until = time.monotonic() + config.repair_timeout_seconds * 0.55
                while time.monotonic() < until:
                    pass  # Cooperative Python delay; hard native stalls have a child-only scenario.
            return True

        provider = provider_from_env() if provider_name == "auto" else FixtureProvider()
        runtime = SelfHealRuntime(config, provider=provider, reviewer=review, event_sink=TerminalReporter())
        started = time.monotonic()
        if scenario in ("retry-timeout", "hard-timeout", "global-timeout"):
            if scenario == "global-timeout":
                runtime.config.auto_apply = False
            try:
                runtime.call(module_name=name, symbol_name="calculate", args=(10, "/", 0), retry_safe=True)
            except ZeroDivisionError:
                pass
            result = runtime.last_result
        else:
            result = runtime.inspect(module_name=name, symbol_name="calculate", health_check=calculator_health,
                                     retry_safe=True, apply=scenario != "global-timeout")
        elapsed = time.monotonic() - started
        if scenario == "heal":
            ok = result is not None and result.healed and saved(8, "-", 3) == 5 and saved(10, "/", 0) is None
        else:
            ok = (result is not None and result.status == "timed_out" and
                  target.read_text(encoding="utf-8") == BROKEN and saved(8, "-", 3) == 11 and
                  elapsed <= config.repair_timeout_seconds + 3)
        print(f"{'PASS' if ok else 'FAIL'}: {scenario} | status={getattr(result, 'status', None)} | elapsed={elapsed:.2f}s", flush=True)
        if result:
            print(result.message, flush=True)
        if scenario == "heal" and ok:
            print(f"AFTER: 8 - 3 = {saved(8, '-', 3)}; 10 / 0 = {saved(10, '/', 0)!r}", flush=True)
        return ok
    finally:
        sys.path.remove(str(root))
        sys.modules.pop(name, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="heal")
    parser.add_argument("--timeout", type=float, default=None, help="Explicit override of the .env repair budget")
    parser.add_argument("--provider", choices=("offline", "auto"), default="offline")
    parser.add_argument("--worker-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        from selfheal import SelfHealConfig
    except ImportError as exc:
        print(f"SDK unavailable: {exc}")
        print('Build/install yourself first: .\\.venv\\Scripts\\python.exe -m pip install -e ".[dev]"')
        return 2
    if args.provider == "auto" and args.scenario != "heal":
        parser.error("Cloud provider is only for --scenario heal; controlled timeouts are offline")
    base = Path(__file__).resolve().parents[1] / ".selfheal" / "manual-timing"
    base.mkdir(parents=True, exist_ok=True)
    if args.worker_root is not None:
        root = args.worker_root.resolve(strict=True)
        root.relative_to(base.resolve())
        if args.scenario != "hard-timeout":
            parser.error("Private worker mode is only for hard-timeout")
        return 0 if run_case(root, args.scenario, args.timeout, args.provider) else 1
    config = SelfHealConfig(base, repair_timeout_seconds=args.timeout)
    timeout = config.repair_timeout_seconds
    scenarios = SCENARIOS if args.scenario == "all" else (args.scenario,)
    passed = True
    for scenario in scenarios:
        with tempfile.TemporaryDirectory(prefix="calculator-", dir=base) as directory:
            root = Path(directory)
            if scenario == "hard-timeout":
                child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                    "--scenario", scenario, "--timeout", str(timeout), "--worker-root", str(root)])
                try:
                    code = child.wait(timeout=timeout + 15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
                    print("FAIL: hard-timeout child exceeded the outer manual-fixture guard")
                    passed = False
                    continue
                target = root / (root / "target.txt").read_text(encoding="utf-8")
                end = time.monotonic() + 6
                while target.read_text(encoding="utf-8") != BROKEN and time.monotonic() < end:
                    time.sleep(0.05)  # Watchdog restores only after owner exit.
                expected_exit = code == 124 if os.name == "nt" else code == -signal.SIGKILL
                ok = expected_exit and target.read_text(encoding="utf-8") == BROKEN
                print(f"{'PASS' if ok else 'FAIL'}: hard-timeout | calculator child exit={code} | source restored")
            else:
                ok = run_case(root, scenario, args.timeout, args.provider)
            passed = passed and ok
    print(f"\nMANUAL CALCULATOR: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
