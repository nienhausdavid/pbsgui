"""pbsgui – GUI, CLI und TUI für den Proxmox Backup Client."""


def _detect_version() -> str:
    # 1) vom Paketbau geschriebene Datei (.deb), 2) Metadaten (pip/venv), 3) unbekannt
    try:
        from ._version import __version__  # type: ignore[import-not-found]
        return __version__
    except ImportError:
        pass
    try:
        from importlib.metadata import version
        return version("pbsgui")
    except Exception:
        return "unbekannt"


__version__ = _detect_version()
