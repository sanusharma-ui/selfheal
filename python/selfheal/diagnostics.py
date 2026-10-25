"""Bounded, opt-in read-only context for diagnosing failures across modules."""
from __future__ import annotations

from pathlib import Path

from .privacy import redact


def related_sources(root: Path, target: Path, paths: tuple[str, ...]) -> dict[str, str]:
    if not isinstance(paths, tuple) or len(paths) > 8:
        raise ValueError("context_files must be a tuple of at most 8 project Python files")
    root, target = root.resolve(), target.resolve()
    result = {}
    total = 0
    for name in paths:
        if type(name) is not str or not name:
            raise ValueError("context_files entries must be nonempty relative paths")
        relative = Path(name)
        if relative.is_absolute() or relative.drive or ".." in relative.parts or relative.suffix != ".py":
            raise ValueError("Diagnostic context must be a relative Python source file inside project_root")
        path = (root / relative).resolve(strict=True)
        normalized = path.relative_to(root).as_posix()
        if path == target or normalized in result:
            continue
        if not path.is_file() or path.stat().st_size > 16_000:
            raise ValueError("A diagnostic source file exceeds the 16 KB context budget")
        with path.open("rb") as source:
            raw = source.read(16_001)
        total += len(raw)
        if total > 16_000:
            raise ValueError("Related source context exceeds the combined 16 KB budget")
        text = raw.decode("utf-8")
        if redact(text) != text:
            raise ValueError("Related source contains a detected secret; remove it from diagnostic context")
        result[normalized] = text
    return result


def context_unchanged(root: Path, sources: dict[str, str]) -> bool:
    try:
        for relative, expected in sources.items():
            path = (root / relative).resolve(strict=True)
            path.relative_to(root)
            with path.open("rb") as stream:
                if stream.read(16_001) != expected.encode("utf-8"):
                    return False
        return True
    except (OSError, ValueError):
        return False
