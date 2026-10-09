"""Restrict live changes to developer-selected function bodies."""
from __future__ import annotations

import ast


def validate_edit_scope(original: str, replacement: str, symbols: tuple[str, ...]) -> None:
    if not symbols:
        return

    def protected_tree(source):
        tree = ast.parse(source)
        found = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in symbols:
                if node.name in found:
                    raise ValueError("Duplicate editable function definition")
                found.add(node.name)
                # Keep signatures, defaults and decorators frozen. Only bodies vary.
                node.body = [ast.Pass()]
        if found != set(symbols):
            raise ValueError("An editable function was removed or is not a top-level function")
        return ast.dump(tree, include_attributes=False)

    if protected_tree(original) != protected_tree(replacement):
        raise ValueError("Candidate changed code outside the allowed function bodies")
