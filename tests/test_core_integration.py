from pathlib import Path

from selfheal._core import CoreEngine
import pytest
import sys


def test_rust_core_verifies_applies_and_rolls_back(tmp_path: Path):
    target = tmp_path / "sample.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")

    core = CoreEngine(
        str(tmp_path),
        __import__("sys").executable,
        False,
        ".selfheal/snapshots",
        ".selfheal/audit.jsonl",
    )

    receipt = core.verify_and_apply(
        str(target),
        "VALUE = 2\n",
        "test-proposal",
    )

    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert receipt.verification_ok is True

    core.rollback(
        receipt.target_file,
        receipt.snapshot_file,
        receipt.proposal_id,
        "integration test rollback",
    )

    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.parametrize("proposal_id", ["../escape", "a/b", "a\\b", "", ".."])
def test_core_rejects_unsafe_ids(tmp_path, proposal_id):
    target = tmp_path / "app.py"
    target.write_text("x = 1\n")
    core = CoreEngine(str(tmp_path), sys.executable, False)
    with pytest.raises(RuntimeError):
        core.verify_and_apply(str(target), "x = 2\n", proposal_id)
    assert target.read_text() == "x = 1\n"


def test_core_rejects_stale_source(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("x = 3\n")
    core = CoreEngine(str(tmp_path), sys.executable, False)
    with pytest.raises(RuntimeError, match="Source changed"):
        core.verify_and_apply(str(target), "x = 2\n", "stale", "x = 1\n")
    assert target.read_text() == "x = 3\n"


def test_failing_project_tests_prevent_apply(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("x = 1\n")
    (tmp_path / "test_app.py").write_text("from app import x\ndef test_x():\n    assert x == 1\n")
    core = CoreEngine(str(tmp_path), sys.executable, True)
    report = core.verify(str(target), "x = 2\n")
    assert report.syntax_ok and report.tests_ok is False and not report.ok
    with pytest.raises(RuntimeError, match="did not pass"):
        core.verify_and_apply(str(target), "x = 2\n")
    assert target.read_text() == "x = 1\n"


def test_snapshot_cannot_be_overwritten_by_reused_id(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("x = 1\n")
    core = CoreEngine(str(tmp_path), sys.executable, False)
    receipt = core.verify_and_apply(str(target), "x = 2\n", "reused")
    with pytest.raises(RuntimeError):
        core.verify_and_apply(str(target), "x = 3\n", "reused")
    assert target.read_text() == "x = 2\n"
    assert __import__("pathlib").Path(receipt.snapshot_file).read_text() == "x = 1\n"
