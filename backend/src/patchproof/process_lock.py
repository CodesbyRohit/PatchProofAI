"""One server process per data directory.

The run queue, rate limits and idempotency keys live in the server process, and every start marks
unfinished runs as interrupted. A second process on the same data directory (for example
`uvicorn --workers 2`) would cancel the first one's runs, so hosted mode refuses to start it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

from .config import ConfigError


def acquire_process_lock(path: Path) -> IO[str]:
    """Hold an exclusive lock on `path` for the life of the process; raise ConfigError if it is taken."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ConfigError(
            f"Another PatchProof process is already using {path.parent}. "
            "Run exactly one server process (one uvicorn worker) per data directory."
        ) from None
    return handle
