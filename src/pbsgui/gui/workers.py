"""Hintergrund-Thread für blockierende Aufrufe (Netzwerk, Client, pkexec)."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)
    line = Signal(str)

    def __init__(self, fn: Callable[[Callable[[str], None]], object], parent=None):
        super().__init__(parent)
        self._fn = fn
        self.finished.connect(self.deleteLater)

    def run(self) -> None:
        try:
            self.done.emit(self._fn(self.line.emit))
        except Exception as e:  # Fehler an die GUI melden, nie den Thread abstürzen lassen
            self.failed.emit(str(e) or type(e).__name__)
