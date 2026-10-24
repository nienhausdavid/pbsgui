"""Schutz vor Rechteausweitung: Als root läuft nur Code, den ein normaler Benutzer nicht verändern kann.

pbsgui startet für Vollbackups und Systemzeitpläne Python als root (pkexec, systemd-Unit). Liegt der Code oder der
Interpreter in einem Ordner, den der Benutzer beschreiben darf (Entwicklungs-Checkout, virtuelle Umgebung), könnte
jedes Programm dieses Benutzers dort Code ablegen, der dann mit Root-Rechten läuft. Deshalb wird in dem Fall nicht als
root gestartet. Die Debian-Pakete installieren nach /usr und sind unbedenklich.

Ausnahme für Entwickler: Umgebungsvariable PBSGUI_ALLOW_USER_CODE_AS_ROOT=1.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

ENV_ALLOW = "PBSGUI_ALLOW_USER_CODE_AS_ROOT"


class PrivilegeError(RuntimeError):
    pass


def _unsafe_part(path: Path) -> str | None:
    """Erstes Element (Datei oder übergeordneter Ordner) auf dem Weg, das nicht root gehört oder für andere beschreibbar ist."""
    p = Path(os.path.realpath(path))
    for cur in [p, *p.parents]:
        try:
            st = cur.stat()
        except OSError:
            return str(cur)
        if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return str(cur)
    return None


def package_root() -> Path:
    return Path(__file__).resolve().parents[2]    # .../src oder .../dist-packages


def require_trusted_code(python: str | None = None) -> None:
    """Bricht ab, wenn der Code oder der Interpreter von einem normalen Benutzer verändert werden kann."""
    if os.environ.get(ENV_ALLOW) == "1":
        return
    checks = [("pbsgui-Code", package_root() / "pbsgui"), ("Python-Interpreter", Path(python or sys.executable))]
    if sys.prefix != sys.base_prefix:     # virtuelle Umgebung: ihre Bibliotheken würden ebenfalls als root geladen
        checks.append(("virtuelle Python-Umgebung", Path(sys.prefix)))
    for what, path in checks:
        bad = _unsafe_part(path)
        if bad:
            raise PrivilegeError(
                f"Aus Sicherheitsgründen startet pbsgui {what} nicht als root: '{bad}' gehört nicht root oder ist für "
                f"andere beschreibbar. Bitte das Debian-Paket installieren (scripts/install.sh). Nur für Entwicklung: "
                f"{ENV_ALLOW}=1 setzen.")
