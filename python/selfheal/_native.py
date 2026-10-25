"""Reject incompatible editable native builds before contacting a provider."""
from __future__ import annotations

import inspect

from .errors import ConfigurationError


def check_native_compatibility() -> None:
    from . import _core

    try:
        compatible = (getattr(_core, "NATIVE_API_VERSION", None) == 1
                      and "timeout_seconds" in inspect.signature(_core.CoreEngine.verify).parameters
                      and "timeout_seconds" in inspect.signature(_core.CoreEngine.verify_and_apply).parameters)
    except (TypeError, ValueError, AttributeError):
        compatible = False
    if not compatible:
        raise ConfigurationError(
            "SelfHeal's native extension is stale or incompatible with this Python SDK. "
            "Move obsolete _core*.pyd/_core*.so files out of an editable source directory, "
            "then rebuild with 'python -m pip install -e \".[dev]\"', or reinstall a matching "
            "selfheal-runtime wheel. Restart the application afterwards. No AI request was made."
        )
