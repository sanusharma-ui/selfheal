"""Trusted scenarios and bounded property exploration for application health.

Contracts belong to the developer, never to the repair provider. Checks must
construct fresh disposable state and avoid external side effects. Exploration
finds counterexamples; an unsuccessful search does not prove correctness.
"""
from __future__ import annotations

from copy import deepcopy
import inspect

from ._async_support import RepairCancelled
from .errors import ConfigurationError
from .models import HealthCheckResult, HealthReport
from .privacy import redact
from .timing import RepairTimeout, checkpoint


def _check_name(name):
    if type(name) is not str or not name.strip() or len(name) > 160:
        raise ValueError("A check needs a nonempty name of at most 160 characters")


def _predicate(callback, module, *args):
    checkpoint("health scenario")
    try:
        value = callback(module, *args)
    except (RepairTimeout, RepairCancelled, ConfigurationError):
        raise
    except Exception as exc:
        return False, f"Scenario raised {type(exc).__name__}"
    if inspect.isawaitable(value):
        if inspect.iscoroutine(value):
            value.close()
        raise ConfigurationError("Scenario and property predicates must be synchronous; use a separate async health callback")
    if type(value) is not bool:
        raise ConfigurationError("Scenario and property predicates must return a boolean")
    return value, "Contract satisfied" if value else "Contract violated"


class ScenarioCheck:
    """A trusted workflow that constructs fresh state and returns a bool."""

    def __init__(self, name, scenario):
        _check_name(name)
        if not callable(scenario):
            raise TypeError("scenario must be callable")
        self.name, self.scenario = name, scenario

    def __call__(self, module):
        ok, details = _predicate(self.scenario, module)
        return HealthReport((HealthCheckResult(self.name, ok, details),))


class HealthSuite:
    """Combine synchronous HealthReport callbacks; empty reports fail closed."""

    def __init__(self, *checks):
        if not checks or any(not callable(check) for check in checks):
            raise ValueError("HealthSuite needs at least one callable check")
        self.checks = checks

    def __call__(self, module):
        results = []
        for check in self.checks:
            checkpoint("health suite")
            report = check(module)
            if not isinstance(report, HealthReport) or not report.checks or any(
                    not isinstance(item, HealthCheckResult) for item in report.checks):
                if inspect.iscoroutine(report):
                    report.close()
                raise ConfigurationError("Every HealthSuite check must return a nonempty HealthReport")
            results.extend(report.checks)
        return HealthReport(tuple(results))


class _ExplorationLimit(Exception):
    pass


class PropertyCheck:
    """Search for counterexamples and replay retained failures on every call.

    ``cases()`` constructs a Hypothesis strategy. Keeping a factory rather than
    a live strategy avoids serializing Hypothesis's internal caches. The trusted
    ``predicate(module, case)`` must create fresh state for each evaluation.
    ``describe_case`` opts into diagnostic input disclosure; otherwise case values
    stay local and are not included in health reports sent to the AI provider.
    """

    def __init__(self, name, cases, predicate, *, max_examples=100,
                 max_evaluations=1000, examples=(), describe_case=None):
        _check_name(name)
        if not callable(cases) or not callable(predicate):
            raise TypeError("cases and predicate must be callable")
        if (type(max_examples) is not int or not 1 <= max_examples <= 10_000
                or type(max_evaluations) is not int or not 1 <= max_evaluations <= 100_000):
            raise ValueError("Exploration limits must be positive bounded integers")
        if not isinstance(examples, tuple) or len(examples) > 100:
            raise ValueError("examples must be a tuple of at most 100 trusted cases")
        if describe_case is not None and not callable(describe_case):
            raise TypeError("describe_case must be callable")
        try:
            import hypothesis  # noqa: F401
        except ImportError:
            raise ConfigurationError("PropertyCheck requires 'pip install selfheal-runtime[exploration]'") from None
        self.name, self.cases, self.predicate = name, cases, predicate
        self.max_examples, self.max_evaluations = max_examples, max_evaluations
        self.examples = deepcopy(examples)
        self.describe_case = describe_case
        self._counterexamples = []

    def __call__(self, module):
        from hypothesis import Phase, Verbosity, find, settings
        from hypothesis.errors import NoSuchExample

        evaluations = 0
        last_details = "Contract violated"

        def fails(case):
            nonlocal evaluations, last_details
            checkpoint("property exploration")
            if evaluations >= self.max_evaluations:
                raise _ExplorationLimit()
            evaluations += 1
            ok, last_details = _predicate(self.predicate, module, deepcopy(case))
            return not ok

        def failed(case, *, retained=False):
            if not retained:
                self._counterexamples = (self._counterexamples + [deepcopy(case)])[-8:]
            details = last_details + "; failing case retained for candidate and activation checks"
            if self.describe_case is not None:
                details += "; " + redact(str(self.describe_case(deepcopy(case))))[:1000]
            return HealthReport((HealthCheckResult(self.name, False, details),))

        try:
            for case in (*self._counterexamples, *self.examples):
                if fails(case):
                    # Explicit examples already participate in every later call.
                    return failed(case, retained=True)
            case = find(self.cases(), fails, settings=settings(
                max_examples=self.max_examples, deadline=None, database=None,
                derandomize=True, verbosity=Verbosity.quiet,
                phases=(Phase.generate, Phase.shrink),
            ))
            if not fails(case):
                return HealthReport((HealthCheckResult(self.name, False, "Unstable scenario; automatic acceptance refused"),))
            return failed(case)
        except NoSuchExample:
            return HealthReport((HealthCheckResult(self.name, True,
                f"No counterexample found in {evaluations} evaluations; retained cases also passed"),))
        except _ExplorationLimit:
            return HealthReport((HealthCheckResult(self.name, False,
                f"Exploration inconclusive: {self.max_evaluations} evaluation limit reached"),))
