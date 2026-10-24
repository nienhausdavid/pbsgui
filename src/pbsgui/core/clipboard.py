"""Text in die Zwischenablage legen (für die Kommandozeile; GUI und TUI haben eigene Wege)."""
from __future__ import annotations

import shutil
import subprocess
from typing import Callable

from ..i18n import tr

TOOLS = (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"])


class ClipboardError(RuntimeError):
    pass


def copy_text(text: str, run: Callable = subprocess.run, which: Callable = shutil.which) -> str:
    """Kopiert `text` mit wl-copy, xclip oder xsel (der Text geht über stdin, nie als Argument). Gibt den Programmnamen zurück."""
    for argv in TOOLS:
        if which(argv[0]):
            try:
                r = run(argv, input=text, text=True, capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0:
                return argv[0]
    raise ClipboardError(tr("Kein Programm für die Zwischenablage gefunden (wl-clipboard, xclip oder xsel installieren)"))
