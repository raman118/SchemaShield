import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.checkpoint import (
    MANIFEST_NAME,
    CheckpointError,
    create_checkpoint,
    restore_archive,
    verify_archive,
)


def make_archive(path: Path, members: dict[str, bytes], metadata: dict | None = None):
    files = {
        name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        for name, content in members.items()
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
        archive.writestr(
            MANIFEST_NAME,
            json.dumps(
                metadata
                or {
                    "format_version": 1,
                    "files": files,
                }
            ),
        )


def test_snapshot_restore_checksums_exclusions_and_retention(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "app.py").write_text("print('SchemaShield')\n", encoding="utf-8")
    (root / ".env").write_text("secret", encoding="utf-8")
    (root / ".env.example").write_text("PLACEHOLDER=", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dependency.js").write_text("generated", encoding="utf-8")
    (root / "private.key").write_text("private", encoding="utf-8")
    for number in range(7):
        checkpoint = create_checkpoint(root, f"G{number:02}", ["pytest tests/test_checkpoint.py"])
    assert len(list((root / ".checkpoints").glob("*.zip"))) == 5
    assert len(list((root / ".checkpoints").glob("*.json"))) == 5
    manifest = verify_archive(checkpoint)
    assert set(manifest["files"]) == {"app.py", ".env.example"}
    assert manifest["verification_commands"] == ["pytest tests/test_checkpoint.py"]
    restored = tmp_path / "restored"
    restore_archive(checkpoint, restored)
    assert (restored / "app.py").read_bytes() == (root / "app.py").read_bytes()
    assert not (restored / ".env").exists()


def test_restore_never_overwrites_even_empty_directory(tmp_path):
    archive = tmp_path / "source.zip"
    make_archive(archive, {"app.py": b"source"})
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(CheckpointError, match="new and nonexistent"):
        restore_archive(archive, target)
    (target / "user.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(CheckpointError):
        restore_archive(archive, target)
    assert (target / "user.txt").read_text() == "keep"


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "C:/escape",
        "a\\b",
        "a/../b",
        "./a",
        "CON.txt",
        "trailing.",
        "a:stream",
    ],
)
def test_archive_traversal_rejected(tmp_path, name):
    archive = tmp_path / "attack.zip"
    make_archive(archive, {name: b"attack"})
    with pytest.raises(CheckpointError, match="Unsafe"):
        restore_archive(archive, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_corrupt_checksum_and_unmanifested_member_rejected(tmp_path):
    archive = tmp_path / "corrupt.zip"
    make_archive(
        archive,
        {"app.py": b"altered"},
        {
            "format_version": 1,
            "files": {"app.py": {"sha256": "0" * 64, "size": 7}},
        },
    )
    with pytest.raises(CheckpointError, match="Checksum"):
        verify_archive(archive)
    make_archive(archive, {"app.py": b"source"})
    with zipfile.ZipFile(archive, "a") as output:
        output.writestr("extra.py", b"extra")
    with pytest.raises(CheckpointError, match="file sets differ"):
        verify_archive(archive)


def test_archive_symlink_and_duplicate_rejected(tmp_path):
    archive = tmp_path / "link.zip"
    make_archive(archive, {})
    info = zipfile.ZipInfo("linked")
    info.create_system = 3
    info.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive, "a") as output:
        output.writestr(info, b"/outside")
    with pytest.raises(CheckpointError, match="links"):
        verify_archive(archive)
    make_archive(archive, {"app.py": b"source"})
    with pytest.warns(UserWarning, match="Duplicate"):
        with zipfile.ZipFile(archive, "a") as output:
            output.writestr("app.py", b"source")
    with pytest.raises(CheckpointError, match="Duplicate"):
        verify_archive(archive)


def test_excluded_secret_archive_rejected(tmp_path):
    archive = tmp_path / "secret.zip"
    make_archive(archive, {".env": b"secret"})
    with pytest.raises(CheckpointError, match="Excluded"):
        verify_archive(archive)


def test_invalid_goal_rejected(tmp_path):
    with pytest.raises(CheckpointError, match="identifier"):
        create_checkpoint(tmp_path, "../../escape")


def test_case_collision_and_directory_conflict_rejected(tmp_path):
    archive = tmp_path / "collision.zip"
    make_archive(archive, {"a.py": b"a", "A.py": b"b"})
    with pytest.raises(CheckpointError, match="Duplicate"):
        verify_archive(archive)
    make_archive(archive, {"a": b"a", "A/b.py": b"b"})
    with pytest.raises(CheckpointError, match="paths conflict"):
        verify_archive(archive)


def test_size_limit_rejected_before_restore(tmp_path, monkeypatch):
    archive = tmp_path / "large.zip"
    make_archive(archive, {"app.py": b"a" * 1000})
    monkeypatch.setattr("scripts.checkpoint.MAX_FILE_BYTES", 500)
    with pytest.raises(CheckpointError, match="size limit"):
        restore_archive(archive, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_retention_preserves_unowned_files(tmp_path):
    directory = tmp_path / ".checkpoints"
    directory.mkdir()
    user_archive = directory / "user-backup.zip"
    user_archive.write_bytes(b"user content")
    for number in range(6):
        create_checkpoint(tmp_path, f"G{number:02}")
    assert user_archive.read_bytes() == b"user content"
    assert len(list(directory.glob("G*.zip"))) == 5
