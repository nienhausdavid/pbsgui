"""pbsgui – GUI, CLI und TUI für den Proxmox Backup Client."""


def _detect_version() -> str:
    # 1) vom Paketbau geschriebene Datei (.deb), 2) pyproject.toml im Checkout, 3) pip-Metadaten, 4) unbekannt
    try:
        from ._version import __version__  # type: ignore[import-not-found]
        return __version__
    except ImportError:
        pass
    try:  # Entwicklungsstand: pyproject.toml des Checkouts (die pip-Metadaten eines editierbaren Installs sind veraltet)
        import re
        from pathlib import Path
        pp = Path(__file__).resolve().parents[2] / "pyproject.toml"
        if pp.is_file() and 'name = "pbsgui"' in pp.read_text():
            m = re.search(r'^version = "(.*)"', pp.read_text(), re.M)
            if m:
                return m.group(1)
    except OSError:
        pass
    try:
        from importlib.metadata import version
        return version("pbsgui")
    except Exception:
        return "unbekannt"


__version__ = _detect_version()
