"""Sperre pro Profil: nie zwei Backups desselben Profils gleichzeitig.

PBS erlaubt pro Backup-Gruppe nur einen laufenden Upload. Treffen der normale Zeitplan, der wöchentliche Lauf im Modus
`data` oder ein manueller Start aufeinander, wartet der zweite Lauf, statt mit einem Fehler abzubrechen.
"""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

WAIT_SECONDS = 4 * 3600   # so lange wartet ein Lauf maximal auf den vorherigen


class LockTimeout(RuntimeError):
    pass


@contextmanager
def run_lock(path: Path, on_line: Callable[[str], None] = lambda _l: None, wait: float | None = None,
             poll: float = 2.0) -> Iterator[None]:
    wait = WAIT_SECONDS if wait is None else wait   # zur Laufzeit lesen, damit Tests das Limit verkürzen können
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        told = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not told:
                    on_line("Ein anderes Backup dieses Profils läuft noch, warte darauf …")
                    told = True
                if time.monotonic() >= deadline:
                    raise LockTimeout("Das vorherige Backup dieses Profils läuft seit über "
                                      f"{wait / 3600:.0f} h noch; dieser Lauf wurde nicht gestartet.") from None
                time.sleep(poll)
        if told:
            on_line("Weiter: das vorherige Backup ist fertig.")
        yield
    finally:
        os.close(fd)   # schließt auch die Sperre


def is_locked(path: Path) -> bool:
    """Läuft gerade ein Backup, das diese Sperre hält? Nicht lesbare oder fehlende Sperrdateien gelten als frei."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)
