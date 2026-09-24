from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class DynamicFunctionHandle:
    module_name: str
    symbol_name: str

    def resolve(self) -> Callable[..., Any]:
        module = importlib.import_module(self.module_name)
        target = getattr(module, self.symbol_name)

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
