from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable

from ._symbols import resolve_binding


@dataclass(frozen=True, slots=True)
class DynamicFunctionHandle:
    module_name: str
    symbol_name: str

    def resolve(self) -> Callable[..., Any]:
        module = importlib.import_module(self.module_name)
        # Preserve ordinary dynamic module dispatch, including existing callers
        # using callable objects. Dotted repair targets use explicit descriptors.
        target = (getattr(module, self.symbol_name) if "." not in self.symbol_name
                  else resolve_binding(module, self.symbol_name).callable())

        if not callable(target):
            raise TypeError(
                f"{self.module_name}.{self.symbol_name} is not callable"
            )

        return target

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.resolve()(*args, **kwargs)


class FunctionRegistry:
    def handle(
        self,
        module_name: str,
        symbol_name: str,
    ) -> DynamicFunctionHandle:
        return DynamicFunctionHandle(
            module_name=module_name,
            symbol_name=symbol_name,
        )
