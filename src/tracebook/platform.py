"""Filesystem, ownership and service integration boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("TRACEBOOK_HOME", "~/.tracebook")).expanduser()


@contextmanager
def daemon_lock(root: Path) -> Iterator[None]:
    """OS-released lock: a crash cannot strand a PID-file lock."""
    private_dir(root)
    with (root / "daemon.lock").open("a+b") as stream:
        os.chmod(stream.name, 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt

                stream.write(b"\0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("A Tracebook daemon already owns this state directory") from exc
        yield


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def child_tree(pid: int) -> list[dict[str, object]]:
    """Best-effort POSIX process metadata, without environment or output capture."""
    if sys.platform == "win32":
        return []
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,comm="],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        rows = [line.strip().split(None, 2) for line in result.stdout.splitlines()]
        parents = {pid}
        found: dict[int, dict[str, object]] = {}
        for _ in range(16):
            additions = {
                int(row[0]): {"pid": int(row[0]), "parent_pid": int(row[1]), "executable": row[2]}
                for row in rows
                if len(row) == 3
                and row[0].isdigit()
                and row[1].isdigit()
                and int(row[1]) in parents
                and int(row[0]) not in parents
            }
            if not additions:
                break
            found.update(additions)
            parents.update(additions)
        return list(found.values())[:1024]
    except (OSError, subprocess.TimeoutExpired):
        return []


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)
    return path


def atomic_write(path: Path, content: str | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".tracebook-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content.encode() if isinstance(content, str) else content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
