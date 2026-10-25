"""Release regressions for maintainer execution after rebuilding the native core."""
import importlib
import sys
import uuid

import pytest

from selfheal import HealthCheckResult, HealthReport, SelfHealConfig, SelfHealRuntime
from selfheal.languages import PythonRuntimeAdapter
from selfheal.models import RepairProposal
from selfheal.providers import AIProvider
from selfheal.scope import validate_edit_scope


@pytest.fixture
def application(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(tmp_path))
    names = []

    def create(source):
        name = "method_app_" + uuid.uuid4().hex
        names.append(name)
        path = tmp_path / (name + ".py")
        path.write_text(source, encoding="utf-8")
        return importlib.import_module(name), path

    yield create
    for name in names:
        sys.modules.pop(name, None)


class Candidate(AIProvider):
    def __init__(self, source):
        self.source, self.requests = source, []

    def propose_repair(self, request):
        self.requests.append(request)
        return RepairProposal(request.failure.target_file, self.source)


@pytest.mark.parametrize("body,symbol,read", [
    ("    def value(self):\n        return 1\n", "Box.value", lambda m: m.Box().value()),
    ("    @staticmethod\n    def value():\n        return 1\n", "Box.value", lambda m: m.Box.value()),
    ("    @classmethod\n    def value(cls):\n        return 1\n", "Box.value", lambda m: m.Box.value()),
    ("    @property\n    def value(self):\n        return 1\n", "Box.value.fget", lambda m: m.Box().value),
])
def test_methods_and_properties_pass_isolated_and_live_checks(application, body, symbol, read):
    source = "class Box:\n" + body
    module, path = application(source)
    replacement = source.replace("return 1", "return 2")
    provider = Candidate(replacement)
    runtime = SelfHealRuntime(SelfHealConfig(path.parent, run_tests=False), provider=provider)
    original_class = module.Box
    health = lambda candidate: HealthReport((HealthCheckResult("result", read(candidate) == 2),))
    result = runtime.inspect(module_name=module.__name__, symbol_name=symbol,
                             health_check=health, retry_safe=True, apply=True)
    assert result.healed and read(module) == 2
    assert module.Box is original_class and path.read_text() == replacement


def test_super_class_cell_and_existing_bound_reference_survive_activation(application):
    source = ("class Base:\n    def value(self):\n        return 10\n"
              "class Child(Base):\n    def value(self):\n        return super().value() + 1\n")
    module, path = application(source)
    instance = module.Child()
    saved = instance.value
    original_cell = saved.__func__.__closure__[0]
    replacement = source.replace("value() + 1", "value() + 2")
    validate_edit_scope(source, replacement, ("Child.value",))
    path.write_text(replacement, encoding="utf-8")
    PythonRuntimeAdapter().activate(module.__name__, ("Child.value",))
    assert saved() == 12 and saved.__func__.__closure__[0] is original_cell


def test_property_setter_scope_does_not_authorize_getter_or_class_layout(application):
    source = ("class Box:\n    @property\n    def value(self):\n        return self._value\n"
              "    @value.setter\n    def value(self, value):\n        self._value = value + 1\n")
    module, path = application(source)
    replacement = source.replace("value + 1", "value + 2")
    validate_edit_scope(source, replacement, ("Box.value.fset",))
    path.write_text(replacement, encoding="utf-8")
    PythonRuntimeAdapter().activate(module.__name__, ("Box.value.fset",))
    box = module.Box()
    box.value = 3
    assert box.value == 5
    with pytest.raises(ValueError):
        validate_edit_scope(source, replacement.replace("return self._value", "return 999"), ("Box.value.fset",))


def test_class_rebinding_during_live_confirmation_is_rolled_back(application):
    source = "class Box:\n    def value(self):\n        return 1\n"
    module, path = application(source)
    original_class = module.Box
    replacement = source.replace("return 1", "return 2")
    runtime = SelfHealRuntime(SelfHealConfig(path.parent, run_tests=False), provider=Candidate(replacement))

    def health(candidate):
        value = candidate.Box().value()
        if candidate is module and value == 2:
            candidate.Box = type("Rebound", (), {})
        return HealthReport((HealthCheckResult("result", value == 2),))

    result = runtime.inspect(module_name=module.__name__, symbol_name="Box.value",
                             health_check=health, retry_safe=True, apply=True)
    assert result.status == "rolled_back"
    assert module.Box is original_class and module.Box().value() == 1
    assert path.read_text() == source


def test_changed_read_only_helper_invalidates_candidate(application):
    module, path = application("def run():\n    return 1\n")
    helper = path.parent / "helper.py"
    helper.write_text("LIMIT = 1\n", encoding="utf-8", newline="")
    provider = Candidate("def run():\n    return 2\n")
    propose = provider.propose_repair

    def edit_context(request):
        assert request.related_sources == {"helper.py": "LIMIT = 1\n"}
        helper.write_text("LIMIT = 3\n", encoding="utf-8")
        return propose(request)

    provider.propose_repair = edit_context
    runtime = SelfHealRuntime(SelfHealConfig(path.parent, run_tests=False), provider=provider)
    result = runtime.inspect(module_name=module.__name__, symbol_name="run",
        health_check=lambda candidate: HealthReport((HealthCheckResult("result", candidate.run() == 2),)),
        context_files=("helper.py",), retry_safe=True, apply=True)
    assert not result.healed and "Related source changed" in result.message
    assert module.run() == 1 and helper.read_text() == "LIMIT = 3\n"
