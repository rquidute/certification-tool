"""One harness run at a time per host.

Two runs share the test engine, the DUT app and the throwaway-project namespace: the pre-flight cleanup of one would
delete the live project of the other. An advisory `flock` is held for the life of the process, so a crash never leaves
a stale lock behind.
"""
import fcntl
import os
import tempfile
from typing import IO

LOCK_PATH = os.path.join(tempfile.gettempdir(), "th-sanity.lock")


class LockHeld(RuntimeError):
    """Another harness run holds the lock."""


def acquire_lock(path: str = LOCK_PATH) -> IO[str]:
    """Take the lock or raise LockHeld naming the holder. Keep the returned file open; closing it releases the lock."""
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        holder = fh.read().strip() or "unknown"
        fh.close()
        raise LockHeld(f"another sanity run is in progress (pid {holder}); two runs would fight over the test engine, "
                       f"the DUT app and each other's projects") from None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh
