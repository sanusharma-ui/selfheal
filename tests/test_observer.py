from pathlib import Path

from selfheal.observer import build_failure_context


def test_failure_context_contains_exception_data(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("x = 1\n", encoding="utf-8")

    try:
        1 / 0
    except ZeroDivisionError as exc:
        context = build_failure_context(
            exc=exc,
            module_name="app",
            symbol_name="run",
            target_file=target,
            args=(1, 0),
            kwargs={},
        )

    assert context.exception_type == "ZeroDivisionError"
    assert context.module_name == "app"
    assert context.symbol_name == "run"
    assert "ZeroDivisionError" in context.traceback_text
