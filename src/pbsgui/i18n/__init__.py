"""Zweisprachige Oberfläche (Deutsch, Englisch).

Die Quelltexte sind deutsch und dienen zugleich als Schlüssel: `tr("Text {0}", wert)` liefert bei Deutsch den Text selbst,
bei Englisch die Übersetzung aus `en.EN` (fehlt sie, bleibt der deutsche Text stehen). Platzhalter sind `{0}`, `{1}` …; Argumente
werden nur eingesetzt, wenn welche übergeben werden.

Sprache, in dieser Reihenfolge: Umgebungsvariable `PBSGUI_LANG` (`de`, `en`), Datei `<config>/language` (Inhalt `de` oder `en`;
mit `pbsgui language` gesetzt), danach die Systemsprache (`LC_ALL`, `LC_MESSAGES`, `LANGUAGE`, `LANG`: beginnt sie mit `de`,
dann Deutsch). Sonst Englisch. `PBSGUI_LANG=pseudo` markiert alle übersetzbaren Texte (nur für Tests).
"""
from __future__ import annotations

import os
from pathlib import Path

SUPPORTED = ("de", "en")
_lang: str | None = None
_catalog: dict[str, str] | None = None


def config_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pbsgui" / "language"


def detect() -> str:
    env = os.environ.get("PBSGUI_LANG", "").strip().lower()
    if env in SUPPORTED or env == "pseudo":
        return env
    try:
        saved = config_file().read_text().strip().lower()
        if saved in SUPPORTED:
            return saved
    except OSError:
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        value = os.environ.get(var, "")
        if value:
            return "de" if value.lower().startswith("de") else "en"
    return "en"


def language() -> str:
    global _lang
    if _lang is None:
        _lang = detect()
    return _lang


def set_language(lang: str | None) -> None:
    """Sprache für diesen Prozess festlegen (None = neu erkennen). Gespeichert wird nichts."""
    global _lang
    _lang = lang


def save_language(lang: str | None) -> None:
    """Dauerhaft wählen: 'de', 'en' oder None/'auto' (Systemsprache)."""
    f = config_file()
    if lang in (None, "auto"):
        f.unlink(missing_ok=True)
    elif lang in SUPPORTED:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(lang + "\n")
    else:
        raise ValueError(lang)
    set_language(None)


def catalog() -> dict[str, str]:
    global _catalog
    if _catalog is None:
        from .en import EN
        _catalog = EN
    return _catalog


def tr(text: str, *args, **kw) -> str:
    lang = language()
    if lang == "en":
        text = catalog().get(text, text)
    elif lang == "pseudo":
        text = "⟦" + text + "⟧"
    return text.format(*args, **kw) if (args or kw) else text
