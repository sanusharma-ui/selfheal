"""Trusted-contract and bounded-exploration regressions; no live AI requests."""
from types import SimpleNamespace

from hypothesis import strategies as st
import pytest

from selfheal import ConfigurationError, HealthReport, HealthSuite, PropertyCheck, ScenarioCheck
from selfheal.diagnostics import context_unchanged, related_sources


def test_property_finds_silent_bug_and_replays_case_before_fresh_search():
    check = PropertyCheck("quantity", lambda: st.integers(1, 10),
                          lambda module, n: module.quote(n) == n * 2)
    broken = SimpleNamespace(quote=lambda n: 2)
    assert not check(broken).ok
    assert check._counterexamples
    fixed = SimpleNamespace(quote=lambda n: n * 2)
    assert check(fixed).ok
    assert not check(broken).ok


def test_scenario_catches_sequence_bug_without_an_exception():
    def scenario(module):
        state = []
        module.add(state, 5)
        module.remove(state, 5)
        return not state
    check = ScenarioCheck("add/remove", scenario)
    assert not check(SimpleNamespace(add=lambda s, v: s.append(v), remove=lambda s, v: None)).ok
    assert check(SimpleNamespace(add=lambda s, v: s.append(v), remove=lambda s, v: s.remove(v))).ok


def test_exploration_budget_is_not_misreported_as_success():
    check = PropertyCheck("bounded", lambda: st.integers(), lambda module, n: True,
                          max_examples=50, max_evaluations=1)
    report = check(SimpleNamespace())
    assert not report.ok and "inconclusive" in report.describe()


def test_example_values_are_private_unless_explicitly_described():
    value = "private-customer-example"
    check = PropertyCheck("private", lambda: st.just(value), lambda module, case: False,
                          examples=(value,))
    assert value not in check(SimpleNamespace()).describe()


def test_predicate_receives_fresh_case_copy():
    def predicate(module, case):
        case.append(2)
        return len(case) == 2
    check = PropertyCheck("copy", lambda: st.just([1]), predicate, examples=([1],))
    assert check(SimpleNamespace()).ok
    assert check.examples == ([1],)


def test_suite_cannot_hide_empty_checks_or_non_boolean_predicates():
    with pytest.raises(ConfigurationError):
        HealthSuite(lambda module: HealthReport(()))(SimpleNamespace())
    with pytest.raises(ConfigurationError):
        ScenarioCheck("invalid", lambda module: 1)(SimpleNamespace())


@pytest.mark.parametrize("name", ["../outside.py", ".env", "source.txt"])
def test_context_rejects_non_project_python_paths(tmp_path, name):
    with pytest.raises(ValueError):
        related_sources(tmp_path, tmp_path / "app.py", (name,))


def test_context_rejects_secrets_and_detects_changes(tmp_path):
    path = tmp_path / "rules.py"
    path.write_text("LIMIT = 7\n", encoding="utf-8")
    sources = related_sources(tmp_path, tmp_path / "app.py", ("rules.py",))
    assert context_unchanged(tmp_path, sources)
    path.write_text("LIMIT = 8\n", encoding="utf-8")
    assert not context_unchanged(tmp_path, sources)
    path.write_text("API_KEY = 'private-credential'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="secret"):
        related_sources(tmp_path, tmp_path / "app.py", ("rules.py",))
