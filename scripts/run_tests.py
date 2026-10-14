"""Run pytest with isolated scratch files, including restricted Windows sessions.

Windows Python's mode-0700 directory ACL can exclude a sandbox token. Only
directories within this run's scratch tree inherit workspace permissions; no
existing user directory ACL is modified. Normal pytest assertions are unchanged.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(arguments: list[str] | None = None) -> int:
    scratch = ROOT / ".runtime" / "tests" / uuid.uuid4().hex
    scratch.mkdir(parents=True)
    temporary = scratch / "temporary"
    temporary.mkdir()
    os.environ["TEMP"] = os.environ["TMP"] = str(temporary)
    original_mkdir = os.mkdir

    def inherited_mkdir(path, mode=0o777, *, dir_fd=None):
        if os.name == "nt" and mode == 0o700 and dir_fd is None:
            resolved = Path(path).resolve()
            if resolved.is_relative_to(scratch):
                mode = 0o777
        return original_mkdir(path, mode, dir_fd=dir_fd)

    if os.name == "nt":
        os.mkdir = inherited_mkdir
    try:
        import pytest

        return int(
            pytest.main(
                [
                    "-p",
                    "no:cacheprovider",
                    f"--basetemp={scratch / 'pytest'}",
                    *(sys.argv[1:] if arguments is None else arguments),
                ]
            )
        )
    finally:
        os.mkdir = original_mkdir


if __name__ == "__main__":
    raise SystemExit(main())
