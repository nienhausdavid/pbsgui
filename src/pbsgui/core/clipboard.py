"""Text in die Zwischenablage legen (für die Kommandozeile; GUI und TUI haben eigene Wege)."""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable

from ..i18n import tr

TOOLS = (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"])


def over_ssh(env=os.environ) -> bool:
    """Läuft das Programm in einer SSH-Sitzung? Dann gehört die Zwischenablage dem Terminal am anderen Ende, nicht diesem Rechner."""
    return bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY") or env.get("SSH_CLIENT"))


class ClipboardError(RuntimeError):
    pass


def copy_text(text: str, run: Callable | None = None, which: Callable | None = None) -> str:
    """Kopiert `text` mit wl-copy, xclip oder xsel (der Text geht über stdin, nie als Argument). Gibt den Programmnamen zurück."""
    run, which = run or subprocess.run, which or shutil.which            # erst beim Aufruf auflösen, damit Tests sie ersetzen können
    for argv in TOOLS:
        if which(argv[0]):
            try:
                r = run(argv, input=text, text=True, capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if r.returncode == 0:
                return argv[0]
    raise ClipboardError(tr("Kein Programm für die Zwischenablage gefunden (wl-clipboard, xclip oder xsel installieren)"))
