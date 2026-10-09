import importlib
import json
import sys
import uuid
from dataclasses import FrozenInstanceError

import pytest

from selfheal import SelfHealConfig, SelfHealRuntime
from selfheal.models import RepairProposal
from selfheal.providers import AIProvider, ProviderUnavailable

BROKEN = "def run():\n    return 1 / 0\n"
FIXED = "def run():\n    return 42\n"


class FixedProvider(AIProvider):
    def __init__(self, source=FIXED):
        self.source = source
        self.calls = 0

    def propose_repair(self, request):
        self.calls += 1
        return RepairProposal(request.failure.target_file, self.source, "Fix failure")


@pytest.fixture
def app(tmp_path, monkeypatch):
    name = "app_" + uuid.uuid4().hex
    target = tmp_path / (name + ".py")
    target.write_text(BROKEN, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name, target
    sys.modules.pop(name, None)


def runtime(app, **kwargs):
    options = kwargs.pop("config", {})
    return SelfHealRuntime(SelfHealConfig(app[1].parent, run_tests=False, **options),
                           provider=kwargs.pop("provider", FixedProvider()), **kwargs)


def call(rt, app, **kwargs):
    return rt.call(module_name=app[0], symbol_name="run", **kwargs)


def events(app):
    return [json.loads(line)["event"] for line in (app[1].parent / ".selfheal/audit.jsonl").read_text().splitlines()]


def test_default_review_exposes_diff_without_mutation(app):
    rt = runtime(app)
    with pytest.raises(ZeroDivisionError):
        call(rt, app, retry_safe=True)
    assert app[1].read_text() == BROKEN
    review = rt.last_result.review
    assert review.verified and "+    return 42" in review.diff
    assert review.tests_ok is None
    assert "patch_applied" not in events(app)


def test_approved_review_heals_in_same_process(app):
    reviews = []
    def approve(review):
        reviews.append(review)
        with pytest.raises(FrozenInstanceError):
            review.verified = False
        return True
    rt = runtime(app, reviewer=approve)
    assert call(rt, app, retry_safe=True) == 42
    assert rt.last_result.healed
    assert app[1].read_text() == FIXED
    assert importlib.import_module(app[0]).run() == 42
    assert "repair_approved" in events(app)
    assert "repair_committed" in events(app)
    assert len(reviews) == 1


@pytest.mark.parametrize("decision", [False, None, "yes", 1])
def test_approval_requires_literal_true(app, decision):
    rt = runtime(app, reviewer=lambda _: decision)
    with pytest.raises(ZeroDivisionError):
        call(rt, app, retry_safe=True)
    assert app[1].read_text() == BROKEN
    assert "repair_rejected" in events(app)


def test_retry_must_be_explicitly_safe(app):
    rt = runtime(app, config={"auto_apply": True})
    with pytest.raises(ZeroDivisionError):
        call(rt, app)
    assert app[1].read_text() == BROKEN
    assert "retry_safe=True" in rt.last_result.message


def test_disabled_retry_does_not_claim_success(app):
    rt = runtime(app, config={"auto_apply": True, "retry_after_repair": False})
    with pytest.raises(ZeroDivisionError):
        call(rt, app, retry_safe=True)
    assert app[1].read_text() == BROKEN
    assert not rt.last_result.healed


@pytest.mark.parametrize("source", ["def run(:\n", "def run():\n    raise RuntimeError('retry failed')\n"])
def test_invalid_or_unsuccessful_repair_restores_source(app, source):
    rt = runtime(app, provider=FixedProvider(source), config={"auto_apply": True})
    with pytest.raises(ZeroDivisionError):
        call(rt, app, retry_safe=True)
    assert app[1].read_bytes() == BROKEN.encode()
    with pytest.raises(ZeroDivisionError):
        importlib.import_module(app[0]).run()
    assert "repair_committed" not in events(app)
    if source != "def run(:\n":
        assert "repair_rolled_back" in events(app)


def test_provider_error_preserves_original(app, monkeypatch):
    p = FixedProvider()
    def fail(_):
        raise ProviderUnavailable("api-key-must-not-leak")
    monkeypatch.setattr(p, "propose_repair", fail)
    rt = runtime(app, provider=p)
    with pytest.raises(ZeroDivisionError) as exc:
        call(rt, app)
    assert "api-key" not in str(exc.value.__notes__)
    assert "ProviderUnavailable" in rt.last_result.message
    assert app[1].read_text() == BROKEN


def test_stale_approval_does_not_overwrite_edit(app):
    manual = "def run():\n    return 7\n"
    def approve(_):
        app[1].write_text(manual)
        return True
    rt = runtime(app, reviewer=approve)
    with pytest.raises(ZeroDivisionError):
        call(rt, app, retry_safe=True)
    assert app[1].read_text() == manual
    assert "changed after review" in rt.last_result.message


def test_wrong_target_rejected(app, monkeypatch):
    p = FixedProvider()
    other = app[1].parent / "other.py"
    other.write_text("pass\n")
    monkeypatch.setattr(p, "propose_repair", lambda _: RepairProposal(other, FIXED))
    rt = runtime(app, provider=p, config={"auto_apply": True})
    with pytest.raises(ZeroDivisionError):
        call(rt, app, retry_safe=True)
    assert other.read_text() == "pass\n"
    assert app[1].read_text() == BROKEN


def test_same_size_cached_module_is_reloaded(app):
    replacement = BROKEN.replace("1 / 0", "6 * 7")
    rt = runtime(app, provider=FixedProvider(replacement), config={"auto_apply": True})
    assert call(rt, app, retry_safe=True) == 42


def test_commit_failure_rolls_back(app, monkeypatch):
    rt = runtime(app, config={"auto_apply": True})
    actual = rt.core
    class CoreProxy:
        def __getattr__(self, name):
            return getattr(actual, name)
        def commit(self, *args):
            raise OSError("disk full")
    rt.core = CoreProxy()
    with pytest.raises(ZeroDivisionError):
        call(rt, app, retry_safe=True)
    assert app[1].read_text() == BROKEN
    assert "repair_rolled_back" in events(app)


def test_synchronous_repair_cannot_replace_unrelated_module_code(app):
    rt = runtime(app, provider=FixedProvider("raise RuntimeError('module takeover')\n"), config={"auto_apply": True})
    with pytest.raises(ZeroDivisionError):
        call(rt, app, retry_safe=True)
    assert app[1].read_text() == BROKEN
    assert "patch_applied" not in events(app)


def test_live_repair_preserves_module_state_and_existing_function_reference(app):
    stateful = "INITIALIZED = object()\nCACHE = {}\ndef run():\n    return 1 / 0\n"
    app[1].write_text(stateful)
    module = importlib.import_module(app[0])
    initialized, cache, saved_function = module.INITIALIZED, module.CACHE, module.run
    cache["keep"] = 7
    rt = runtime(app, provider=FixedProvider(stateful.replace("1 / 0", "6 * 7")), config={"auto_apply": True})
    assert call(rt, app, retry_safe=True) == 42
    assert module.INITIALIZED is initialized and module.CACHE is cache
    assert module.CACHE == {"keep": 7}
    assert module.run is saved_function and saved_function() == 42
