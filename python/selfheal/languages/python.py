from __future__ import annotations

import importlib
import inspect
from pathlib import Path


class PythonRuntimeAdapter:
    def source_file(self, module_name: str) -> Path:
        module = importlib.import_module(module_name)
        source = inspect.getsourcefile(module)

        if source is None:
            raise RuntimeError(
                f"Could not determine source file for module {module_name!r}"
            )

        return Path(source).resolve()

    def activate(self, module_name: str) -> None:
        importlib.invalidate_caches()
        module = importlib.import_module(module_name)
        # Same-size replacement within one timestamp tick must not use stale pyc.
        cached = getattr(module, "__cached__", None)
        if cached:
            Path(cached).unlink(missing_ok=True)
        importlib.reload(module)
