"""Englischer Katalog: deutscher Quelltext -> englische Übersetzung (aus den Teildateien zusammengesetzt)."""
from __future__ import annotations

EN: dict[str, str] = {}
for _part in ("core", "core2", "cli", "gui", "tui", "web"):
    try:
        _m = __import__(f"{__package__}.en_{_part}", fromlist=["EN"])
    except ImportError:          # Teildatei fehlt (noch nicht übersetzt)
        continue
    EN.update(_m.EN)
