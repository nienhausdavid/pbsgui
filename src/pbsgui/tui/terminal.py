"""Welches Terminal zeigt die TUI an, und nimmt es Text für die Zwischenablage an (OSC 52)?

Über SSH ist die Terminal-Zwischenablage der einzige Weg in die Zwischenablage des eigenen Rechners. Ob das Terminal OSC 52
annimmt, kann man nicht abfragen; erkennbar ist nur, welches Terminal es ist: an `TERM` und an den Antworten auf XTVERSION
(Name und Version) und DA2 (Gerätekennung). Die Abfrage läuft einmal vor dem Start der TUI, solange noch niemand sonst von
der Tastatur liest; DA1 beantwortet jedes Terminal und markiert das Ende der Antworten.
"""
from __future__ import annotations

import os
import re
import select
import sys
import time

YES, NO, UNKNOWN = "yes", "no", "unknown"

GOOD = ("kitty", "wezterm", "ghostty", "foot", "contour", "alacritty", "rio")     # nehmen OSC 52 ohne Einstellung an
GOOD_TERM = ("xterm-kitty", "xterm-ghostty", "foot", "foot-extra", "wezterm", "contour", "alacritty", "rio")

_probe: tuple[str, str] | None = None


def query(timeout: float = 0.5, fd_in: int | None = None, fd_out: int | None = None) -> tuple[str, str]:
    """Fragt das Terminal nach XTVERSION und DA2; liefert (xtversion, da2) oder leere Texte, wenn es nichts sagt."""
    try:
        import termios
        import tty
    except ImportError:
        return "", ""
    fd_in = sys.stdin.fileno() if fd_in is None else fd_in
    fd_out = sys.stdout.fileno() if fd_out is None else fd_out
    if not (os.isatty(fd_in) and os.isatty(fd_out)):
        return "", ""
    old = termios.tcgetattr(fd_in)
    buf = b""
    try:
        tty.setraw(fd_in, termios.TCSANOW)
        os.write(fd_out, b"\x1b[>0q\x1b[>c\x1b[c")                    # XTVERSION, DA2, DA1 (DA1 antwortet immer zuletzt)
        end = time.monotonic() + timeout
        while not re.search(rb"\x1b\[\?[0-9;]*c", buf):
            left = end - time.monotonic()
            if left <= 0 or not select.select([fd_in], [], [], left)[0]:
                break
            buf += os.read(fd_in, 1024)
    except OSError:
        pass
    finally:
        termios.tcsetattr(fd_in, termios.TCSAFLUSH, old)
    return parse(buf)


def parse(buf: bytes) -> tuple[str, str]:
    text = buf.decode(errors="replace")
    xt = re.search(r"\x1bP>\|([^\x1b]*)\x1b\\", text)
    da2 = re.search(r"\x1b\[>([0-9;]*)c", text)
    return (xt.group(1) if xt else ""), (da2.group(1) if da2 else "")


def probe() -> None:
    """Einmal vor dem Start der TUI aufrufen."""
    global _probe
    _probe = query()


def clipboard_support(term: str | None = None, xtversion: str | None = None, da2: str | None = None) -> str:
    """YES: Terminal nimmt OSC 52 an; NO: sicher nicht (GNOME Terminal & Co., Terminal.app); sonst UNKNOWN."""
    term = os.environ.get("TERM", "") if term is None else term
    if xtversion is None or da2 is None:
        xtversion, da2 = _probe or ("", "")
    name = xtversion.lower()
    if term in GOOD_TERM or any(name.startswith(g) for g in GOOD):
        return YES
    if name.startswith("vte"):                 # GNOME Terminal, Ptyxis, Tilix, Terminator: kein OSC 52
        return NO
    if name:                                   # iTerm2 (Einstellung nötig), tmux, xterm (meist gesperrt), Konsole …
        return UNKNOWN
    parts = da2.split(";")
    if len(parts) >= 2 and parts[1].isdigit():
        if parts[0] in ("61", "65") and int(parts[1]) >= 5000:        # VTE ohne XTVERSION-Antwort (ältere Fassungen)
            return NO
        if parts[:2] == ["1", "95"]:                                  # Terminal.app (macOS)
            return NO
    return UNKNOWN
