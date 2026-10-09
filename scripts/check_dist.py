"""Inspect existing release archives without building, installing or extracting."""
from __future__ import annotations

import argparse
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
import tarfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def check(path: Path) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            files = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
        metadata_name = next((name for name in files if name.endswith(".dist-info/METADATA")), None)
        wheel_metadata_name = next((name for name in files if name.endswith(".dist-info/WHEEL")), None)
        if wheel_metadata_name is None:
            raise ValueError("Wheel tags are missing")
        tags = BytesParser().parsebytes(files[wheel_metadata_name]).get_all("Tag", [])
        if not tags or any(tag.split("-")[:2] != ["cp311", "abi3"] for tag in tags):
            raise ValueError("Release wheels must target the CPython 3.11 stable ABI")
        native = [name for name in files if name.startswith("selfheal/_core") and name.endswith((".so", ".pyd"))]
        if len(native) != 1 or "selfheal/py.typed" not in files or "selfheal/_core.pyi" not in files:
            raise ValueError("Wheel must contain one native extension and the SDK type markers")
        if not all(name in files for name in ("selfheal/__init__.py", "selfheal/agent.py", "selfheal/providers/routing.py")):
            raise ValueError("Wheel is missing Python SDK files")
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            files = {}
            for member in archive.getmembers():
                if member.issym() or member.islnk():
                    raise ValueError("Source archive must not contain links")
                original = PurePosixPath(member.name)
                if original.is_absolute() or ".." in original.parts or "\\" in member.name:
                    raise ValueError("Invalid source archive path")
                if member.isfile():
                    handle = archive.extractfile(member)
                    if handle is not None:
                        files[member.name] = handle.read()
        roots = {PurePosixPath(name).parts[0] for name in files}
        if len(roots) != 1:
            raise ValueError("Source archive must have a single top-level directory")
        files = {str(PurePosixPath(name).relative_to(next(iter(roots)))): data for name, data in files.items()}
        metadata_name = "PKG-INFO"
        required = ("pyproject.toml", "Cargo.toml", "Cargo.lock", "src/lib.rs", "python/selfheal/__init__.py")
        if not all(name in files for name in required):
            raise ValueError("Source archive is missing files needed to build the SDK")
    else:
        raise ValueError("Expected a .whl or .tar.gz archive")
    forbidden = {".git", ".venv", "venv", ".selfheal", "__pycache__", ".pytest_cache", "target"}
    for name in files:
        item = PurePosixPath(name)
        if item.is_absolute() or ".." in item.parts or "\\" in name or set(item.parts) & forbidden:
            raise ValueError(f"Forbidden archive member: {name}")
        if any(part == ".env" or (part.startswith(".env.") and part != ".env.example") for part in item.parts):
            raise ValueError(f"Secret configuration file in archive: {name}")
        if item.suffix in (".pyc", ".pyo"):
            raise ValueError(f"Cached bytecode in archive: {name}")
    if metadata_name not in files:
        raise ValueError("Archive metadata is missing")
    metadata = BytesParser().parsebytes(files[metadata_name])
    for field, expected in (("Name", project["name"]), ("Version", project["version"]),
                            ("Requires-Python", project["requires-python"]), ("License-Expression", project["license"])):
        if metadata[field] != expected:
            raise ValueError(f"Incorrect release metadata: {field}")
    if not any(value.startswith("python-dotenv") for value in metadata.get_all("Requires-Dist", [])):
        raise ValueError("Required dotenv dependency is missing from metadata")
    for license_name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        matches = [data for name, data in files.items() if PurePosixPath(name).name == license_name]
        expected = (ROOT / license_name).read_bytes().replace(b"\r\n", b"\n")
        if not any(data.replace(b"\r\n", b"\n") == expected for data in matches):
            raise ValueError(f"Missing or stale legal notice: {license_name}")
    print(f"PASS: {path.name} ({len(files)} files; metadata, SDK and legal notices checked)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="*", type=Path)
    args = parser.parse_args()
    paths = args.archives or sorted((ROOT / "dist").glob("*.whl")) + sorted((ROOT / "dist").glob("*.tar.gz"))
    if not paths:
        print("No release archives found. Build them yourself first; this command does not build.")
        return 1
    try:
        for path in paths:
            check(path)
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as exc:
        print(f"Release check stopped: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
