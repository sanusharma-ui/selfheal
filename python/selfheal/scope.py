"""Restrict live changes to developer-selected function bodies."""
from __future__ import annotations

import ast

from ._symbols import function_nodes


def validate_edit_scope(original: str, replacement: str, symbols: tuple[str, ...]) -> None:
    if not symbols:
        return

    def protected_tree(source):
        tree = ast.parse(source)
        definitions = function_nodes(tree)
        seen = set()
        for symbol in symbols:
            node = definitions.get(symbol)
            if node is None:
                raise ValueError("An editable function or method was removed or is not a direct definition")
            if id(node) in seen:
                raise ValueError("Editable symbols must not alias the same definition")
            seen.add(id(node))
            # Keep class layout, signatures, defaults and descriptors frozen.
            node.body = [ast.Pass()]
        return ast.dump(tree, include_attributes=False)

    if protected_tree(original) != protected_tree(replacement):
        raise ValueError("Candidate changed code outside the allowed function bodies")
