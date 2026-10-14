"""Explicit source selection for Docker SDK custom contexts (ignores no secrets)."""

import os
from pathlib import Path, PurePosixPath

if __package__:
    from .checkpoint import excluded
else:
    from checkpoint import excluded


def source_files(root: Path, directory: Path):
    root = root.resolve()
    for current, directories, files in os.walk(directory):
        base = Path(current)
        directories[:] = sorted(
            name
            for name in directories
            if not (
                (base / name).is_symlink()
                or (base / name).is_junction()
                or excluded(PurePosixPath((base / name).relative_to(root).as_posix()))
            )
        )
        for name in sorted(files):
            path = base / name
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if (
                name.startswith(".env")
                or path.is_symlink()
                or path.is_junction()
                or excluded(relative)
                or not path.resolve().is_relative_to(root)
            ):
                continue
            yield path
