"""Small, dependency-free helpers shared by the sync and scheduler."""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class LockBusy(RuntimeError):
    """Another process owns the requested lock."""


class UserError(ValueError):
    """A safe, actionable message that never includes a service response."""


@contextmanager
def atomic_target(target: Path):
    """Write beside the destination, then replace it only on success."""
    target = Path(target)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        yield temporary
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(target: Path, value):
    with atomic_target(target) as temporary:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


@contextmanager
def process_lock(path: Path):
    """OS-backed lock: automatically released even if the process crashes."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise LockBusy("Another instance is already running") from None
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LockBusy("Another instance is already running") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
