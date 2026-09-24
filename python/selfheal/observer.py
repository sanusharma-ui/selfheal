from __future__ import annotations

import traceback
from pathlib import Path

from .models import FailureContext


def build_failure_context(
    *,
    exc: BaseException,
    module_name: str,
    symbol_name: str,
    target_file: Path,
    args: tuple,
    kwargs: dict,
) -> FailureContext:
    return FailureContext(
        exception_type=type(exc).__name__,
        message=str(exc),
        traceback_text="".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        ),
        module_name=module_name,
        symbol_name=symbol_name,
        target_file=target_file.resolve(),
        args_repr=repr(args),
        kwargs_repr=repr(kwargs),
    )
