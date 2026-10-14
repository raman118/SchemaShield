"""Checksummed source snapshots. Restoration only creates a new destination.

Usage: python scripts/checkpoint.py G00 --verify --command "uv run pytest"
       python scripts/checkpoint.py --archive .checkpoints/G00-....zip --verify
       python scripts/checkpoint.py --archive .checkpoints/G00-....zip --restore NEW_DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

MANIFEST_NAME = "__schemashield_manifest__.json"
EXCLUDED_DIRS = {
    ".runtime",
    ".tools",
    ".secrets",
    "artifacts",
    ".checkpoints",
    ".venv",
    "venv",
    "node_modules",
    ".next",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "build",
    "coverage",
    ".playwright",
    "test-results",
    "playwright-report",
    "volumes",
    "pgdata",
    "postgres-data",
}
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 20_000
RETENTION = 5


class CheckpointError(ValueError):
    """A snapshot violated integrity or filesystem safety constraints."""


def excluded(path: PurePosixPath, *, archive: bool = False) -> bool:
    """Exclude credentials, dependencies, volumes and generated artifacts."""
    if any(
        part.lower() in EXCLUDED_DIRS or part.lower().startswith(".security-review-")
        for part in path.parts
    ):
        return True
    name = path.name.lower()
    return (
        name.startswith(".env")
        and name not in {".env.example", ".env.template"}
        or name.endswith((".pem", ".key", ".p12", ".pfx", ".pyc", ".log", ".sqlite", ".db"))
        # Old format-v1 snapshots included this harmless compiler cache.
        or (not archive and name.endswith(".tsbuildinfo"))
        or name in {"secrets.json", "credentials.json", ".coverage"}
        or any(part.lower() == "secrets" for part in path.parts)
    )


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    reserved = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if (
        not name
        or "\\" in name
        or ":" in name
        or "\x00" in name
        or any(ord(char) < 32 or char in '<>"|?*' for char in name)
        or path.is_absolute()
        or any(part in {".", ".."} for part in name.split("/"))
        or any(
            part.endswith((".", " ")) or part.split(".")[0].upper() in reserved
            for part in path.parts
        )
        or str(path) != name
    ):
        raise CheckpointError(f"Unsafe archive path: {name!r}")
    return path


def _files(root: Path):
    def raise_walk_error(error):
        raise error

    for directory, directories, filenames in root.walk(on_error=raise_walk_error):
        directories[:] = sorted(
            name
            for name in directories
            if not excluded(PurePosixPath((directory / name).relative_to(root).as_posix()))
        )
        for name in [*directories, *sorted(filenames)]:
            path = directory / name
            relative = path.relative_to(root).as_posix()
            if excluded(PurePosixPath(relative)):
                continue
            if path.is_symlink() or path.is_junction():
                raise CheckpointError(f"Source links are forbidden: {relative}")
            if path.is_file():
                safe_name(relative)
                if path.name == MANIFEST_NAME:
                    raise CheckpointError("Reserved manifest filename exists in source")
                yield path, relative


def _verify_open(archive: zipfile.ZipFile) -> dict:
    entries = archive.infolist()
    if len(entries) > MAX_FILES + 1:
        raise CheckpointError("Archive file count exceeds limit")
    names = set()
    normalized_names = set()
    total = 0
    for info in entries:
        safe_name(info.filename)
        if info.filename.casefold() in normalized_names:
            raise CheckpointError("Duplicate archive member")
        names.add(info.filename)
        normalized_names.add(info.filename.casefold())
        mode = info.external_attr >> 16
        if info.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG} or info.flag_bits & 1:
            raise CheckpointError("Directories, links and encrypted members are forbidden")
        if info.file_size > MAX_FILE_BYTES:
            raise CheckpointError("Archive member exceeds size limit")
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise CheckpointError("Archive exceeds total size limit")
    for name in normalized_names:
        if any(str(parent) in normalized_names for parent in PurePosixPath(name).parents):
            raise CheckpointError("Archive file and directory paths conflict")
    if MANIFEST_NAME not in names:
        raise CheckpointError("Missing manifest")
    if archive.getinfo(MANIFEST_NAME).file_size > 4 * 1024 * 1024:
        raise CheckpointError("Manifest exceeds size limit")
    try:
        manifest = json.loads(archive.read(MANIFEST_NAME))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckpointError("Invalid manifest") from error
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
        raise CheckpointError("Unsupported manifest format")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise CheckpointError("Invalid manifest file map")
    for name in files:
        safe_name(name)
    if names != set(files) | {MANIFEST_NAME}:
        raise CheckpointError("Manifest and archive file sets differ")
    for name, expected in files.items():
        if excluded(safe_name(name), archive=True):
            raise CheckpointError(f"Excluded artifact in archive: {name}")
        if not isinstance(expected, dict) or set(expected) != {"sha256", "size"}:
            raise CheckpointError("Invalid file metadata")
        digest = hashlib.sha256()
        size = 0
        with archive.open(name) as source:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
        if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise CheckpointError(f"Checksum mismatch: {name}")
    return manifest


def verify_archive(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        return _verify_open(archive)


def create_checkpoint(root: Path, goal: str, commands: list[str] | None = None) -> Path:
    root = root.resolve(strict=True)
    if not goal or not all(char.isalnum() or char in "-_" for char in goal):
        raise CheckpointError(
            "Goal identifier must contain letters, digits, hyphens or underscores"
        )
    directory = root / ".checkpoints"
    if directory.is_symlink() or directory.is_junction():
        raise CheckpointError("Checkpoint directory cannot be a link")
    directory.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    name = f"{goal}-{stamp}-{uuid.uuid4().hex[:8]}"
    destination = directory / f"{name}.zip"
    temporary = directory / f"{name}.tmp"
    manifest = {
        "format_version": 1,
        "goal": goal,
        "created_at": datetime.now(UTC).isoformat(),
        "verification_commands": commands or [],
        "files": {},
    }
    total = 0
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, relative in _files(root):
                if path.stat().st_size > MAX_FILE_BYTES:
                    raise CheckpointError(f"Source file exceeds size limit: {relative}")
                content = path.read_bytes()
                if len(content) > MAX_FILE_BYTES:
                    raise CheckpointError(f"Source file exceeds size limit: {relative}")
                total += len(content)
                if total > MAX_TOTAL_BYTES or len(manifest["files"]) >= MAX_FILES:
                    raise CheckpointError("Source snapshot exceeds limits")
                manifest["files"][relative] = {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
                archive.writestr(relative, content)
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True))
        verify_archive(temporary)
        temporary.replace(destination)
        destination.with_suffix(".json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        snapshots = []
        for candidate in directory.glob("*.zip"):
            components = candidate.stem.rsplit("-", 2)
            if len(components) != 3:
                continue
            _, timestamp, token = components
            if len(token) != 8 or any(char not in "0123456789abcdef" for char in token):
                continue
            try:
                datetime.strptime(timestamp, "%Y%m%dT%H%M%S%fZ")
            except ValueError:
                continue
            snapshots.append(candidate)
        # Retention uses creation time, independent of goal ordering.
        snapshots.sort(key=lambda path: path.stat().st_mtime_ns, reverse=True)
        for stale in snapshots[RETENTION:]:
            stale.unlink()
            stale.with_suffix(".json").unlink(missing_ok=True)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def restore_archive(path: Path, destination: Path) -> dict:
    """Validate first, then create a directory exclusively; never overwrite files."""
    if destination.exists() or destination.is_symlink():
        raise CheckpointError("Restore destination must be new and nonexistent")
    # Reject linked ancestors to avoid restoring through an unrelated filesystem path.
    for parent in destination.absolute().parents:
        if parent.is_symlink() or parent.is_junction():
            raise CheckpointError("Restore ancestors cannot be links")
    with zipfile.ZipFile(path) as archive:
        manifest = _verify_open(archive)
        destination.mkdir(parents=False, exist_ok=False)
        try:
            for name, expected in manifest["files"].items():
                target = destination.joinpath(*safe_name(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(name) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
                if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
                    raise CheckpointError(f"Restored checksum mismatch: {name}")
        except BaseException:
            shutil.rmtree(destination)
            raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("goal", nargs="?")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--command", action="append", default=[])
    args = parser.parse_args()
    if args.archive is None and args.goal is None:
        parser.error("Supply a goal to snapshot or --archive to inspect/restore")
    if args.restore is not None and args.archive is None:
        parser.error("Restoration requires --archive")
    try:
        archive = args.archive or create_checkpoint(args.root, args.goal, args.command)
        manifest = verify_archive(archive)
        if args.restore is not None:
            restore_archive(archive, args.restore)
        print(
            json.dumps(
                {
                    "archive": str(archive),
                    "verified": True,
                    "files": len(manifest["files"]),
                    "restored_to": str(args.restore) if args.restore else None,
                }
            )
        )
        return 0
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as error:
        print(json.dumps({"error": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
