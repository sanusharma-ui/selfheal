from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from types import CodeType, FunctionType, ModuleType


class PythonRuntimeAdapter:
    def source_file(self, module_name: str) -> Path:
        module = importlib.import_module(module_name)
        source = inspect.getsourcefile(module)

        if source is None:
            raise RuntimeError(
                f"Could not determine source file for module {module_name!r}"
            )

        return Path(source).resolve()

    def prepare(self, module_name: str, source: str, symbols: tuple[str, ...]):
        """Compile function bodies without executing module initialization."""
        module = importlib.import_module(module_name)
        path = self.source_file(module_name)
        compiled = compile(source, str(path), "exec", dont_inherit=True)
        codes = {code.co_name: code for code in compiled.co_consts if isinstance(code, CodeType)}
        updates = []
        for name in symbols:
            function = getattr(module, name, None)
            code = codes.get(name)
            if (not isinstance(function, FunctionType) or function.__globals__ is not module.__dict__
                    or function.__code__.co_name != name or function.__code__.co_freevars
                    or code is None or code.co_freevars):
                raise ValueError("Live targets must be module-level Python functions without wrapper closures")
            unsupported = inspect.CO_COROUTINE | inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR
            if function.__code__.co_flags & unsupported or code.co_flags & unsupported:
                raise ValueError("Async and generator functions are not supported live targets")
            current = function.__code__
            if (current.co_argcount, current.co_posonlyargcount, current.co_kwonlyargcount,
                    current.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS)) != (
                    code.co_argcount, code.co_posonlyargcount, code.co_kwonlyargcount,
                    code.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS)):
                raise ValueError("The candidate does not preserve the running function's call signature")
            argument_count = current.co_argcount + current.co_kwonlyargcount
            argument_count += bool(current.co_flags & inspect.CO_VARARGS) + bool(current.co_flags & inspect.CO_VARKEYWORDS)
            if current.co_varnames[:argument_count] != code.co_varnames[:argument_count]:
                raise ValueError("The candidate changes running argument names")
            updates.append((function, code))
        return updates

    def candidate_module(self, module_name: str, updates) -> ModuleType:
        """A view for pure health checks; shared state is not an OS sandbox.

        Rebind module-owned helpers too, so they resolve candidate functions
        through the same globals without altering the running module.
        """
        module = importlib.import_module(module_name)
        candidate = ModuleType(module_name)
        candidate.__dict__.update(module.__dict__)
        replacements = {id(function): code for function, code in updates}
        clones = {}
        for name, value in module.__dict__.items():
            if isinstance(value, FunctionType) and value.__globals__ is module.__dict__:
                if id(value) not in clones:
                    clone = FunctionType(replacements.get(id(value), value.__code__), candidate.__dict__,
                                         value.__name__, value.__defaults__, value.__closure__)
                    clone.__kwdefaults__ = value.__kwdefaults__
                    clone.__annotations__ = value.__annotations__.copy()
                    clone.__dict__.update(value.__dict__)
                    clones[id(value)] = clone
                candidate.__dict__[name] = clones[id(value)]
        return candidate

    def activate(self, module_name: str, symbols: tuple[str, ...] = ()) -> None:
        if symbols:
            source = self.source_file(module_name).read_text(encoding="utf-8")
            updates = self.prepare(module_name, source, symbols)
            # prepare every function before changing any; the runtime owns the lock.
            originals = [(function, function.__code__) for function, _ in updates]
            try:
                for function, code in updates:
                    function.__code__ = code
            except BaseException:
                for function, code in originals:
                    function.__code__ = code
                raise
            return
        importlib.invalidate_caches()
        module = importlib.import_module(module_name)
        # Same-size replacement within one timestamp tick must not use stale pyc.
        cached = getattr(module, "__cached__", None)
        if cached:
            Path(cached).unlink(missing_ok=True)
        importlib.reload(module)
