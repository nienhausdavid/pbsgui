"""Hintergrund-Thread für blockierende Aufrufe (Netzwerk, Client, pkexec)."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QThread, Signal

# Läuft ein Thread noch, wenn sein Dialog schon zerstört wird, stürzt Qt ab ("QThread destroyed while
# running"). Deshalb besitzt kein Widget den Thread: er bleibt hier erhalten, bis er fertig ist.
_alive: set["Worker"] = set()


class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)
    line = Signal(str)

    def __init__(self, fn: Callable[[Callable[[str], None]], object], parent=None):  # parent bewusst ignoriert
        super().__init__(None)
        self._fn = fn
        self.finished.connect(self._cleanup)

    def start(self, *args) -> None:
        _alive.add(self)
        super().start(*args)

    def _cleanup(self) -> None:
        _alive.discard(self)
        self.deleteLater()

    def run(self) -> None:
        try:
            self.done.emit(self._fn(self.line.emit))
        except Exception as e:  # Fehler an die GUI melden, nie den Thread abstürzen lassen
            self.failed.emit(str(e) or type(e).__name__)
