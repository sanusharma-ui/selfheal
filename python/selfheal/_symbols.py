"""Resolve editable Python definitions without invoking descriptors or constructors."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from types import FunctionType, ModuleType


def valid_symbol(value: str) -> bool:
    return type(value) is str and bool(value) and all(part.isidentifier() for part in value.split("."))


@dataclass(frozen=True)
class FunctionBinding:
    owner: ModuleType | type
    name: str
    descriptor: object
    function: FunctionType
    owners: tuple = ()

    def current(self) -> bool:
        return (all(vars(parent).get(name) is value for parent, name, value in self.owners)
                and vars(self.owner).get(self.name) is self.descriptor)

    def restore(self) -> None:
        for parent, name, value in self.owners:
            setattr(parent, name, value)
        setattr(self.owner, self.name, self.descriptor)

    def callable(self):
        if type(self.descriptor) is classmethod:
            return self.descriptor.__get__(None, self.owner)
        return self.function


def resolve_binding(module: ModuleType, symbol: str) -> FunctionBinding:
    if not valid_symbol(symbol):
        raise ValueError("Target must be a dotted Python function or method name")
    parts = symbol.split(".")
    accessor = None
    if len(parts) > 2 and parts[-1] in ("fget", "fset", "fdel"):
        accessor = parts.pop()
    owner = module
    owners = []
    for part in parts[:-1]:
        value = vars(owner).get(part)
        if type(value) is not type or value.__module__ != module.__name__:
            raise ValueError("Target owners must be local classes with the standard metaclass")
        owners.append((owner, part, value))
        owner = value
    name = parts[-1]
    descriptor = vars(owner).get(name)
    function = descriptor
    if type(descriptor) in (staticmethod, classmethod):
        function = descriptor.__func__
    elif type(descriptor) is property:
        function = getattr(descriptor, accessor or "fget")
    elif accessor is not None:
        raise ValueError("Property accessors require a property target")
    if not isinstance(function, FunctionType):
        raise ValueError("Live targets must resolve to Python functions, methods or property accessors")
    return FunctionBinding(owner, name, descriptor, function, tuple(owners))


def function_nodes(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Index direct definitions only; never expand a function's local closures."""
    result = {}

    def add(name, node):
        if name in result:
            raise ValueError("Duplicate function definition: " + name)
        result[name] = node

    def walk(body, prefix=""):
        for node in body:
            if isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                accessor = None
                for decorator in node.decorator_list:
                    if isinstance(decorator, ast.Name) and decorator.id == "property":
                        accessor = "fget"
                    elif (isinstance(decorator, ast.Attribute) and isinstance(decorator.value, ast.Name)
                          and decorator.value.id == node.name and decorator.attr in ("getter", "setter", "deleter")):
                        accessor = {"getter": "fget", "setter": "fset", "deleter": "fdel"}[decorator.attr]
                if accessor:
                    add(name + "." + accessor, node)
                    if accessor == "fget":
                        add(name, node)
                else:
                    add(name, node)

    walk(tree.body)
    return result
