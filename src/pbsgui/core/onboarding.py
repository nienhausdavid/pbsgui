"""Gemeinsame Einrichtungslogik für GUI, CLI und TUI (Verbindung prüfen, Namespaces, Provisionierung)."""
from __future__ import annotations

import socket

from ..i18n import tr
from .client import PbsClient
from .models import Profile
from .pbs_api import ApiError, PbsApi, ProvisionResult


def namespace_state(known: list[str] | None, ns: str) -> str:
    """'root' | 'exists' | 'new' | 'unknown' für einen eingegebenen Namespace."""
    ns = ns.strip().strip("/")
    if not ns:
        return "root"
    if known is None:
        return "unknown"
    return "exists" if ns in known else "new"


def namespace_message(state: str, weg_a: bool) -> str:
    return {
        "root": tr("Wurzel des Datastores (kein eigener Namespace)."),
        "exists": tr("✔ Namespace existiert."),
        "unknown": tr("Namespace noch nicht geprüft (erst Verbindung prüfen und Datastore wählen)."),
        "new": (tr("ℹ Namespace existiert nicht und wird beim Einrichten neu angelegt.") if weg_a else
                tr("⚠ Namespace existiert nicht. Mit Weg B nicht anlegbar: bitte zuerst in PBS anlegen oder Weg A verwenden.")),
    }[state]


def connect_root(host: str, port: int, fingerprint: str, user: str, password: str, tfa: str = "") -> PbsApi:
    api = PbsApi(host, port, fingerprint)
    api.login(user, password, tfa)
    return api


def connect_token(host: str, port: int, fingerprint: str, auth_id: str, secret: str) -> PbsApi:
    api = PbsApi(host, port, fingerprint)
    api.use_token(auth_id, secret)
    return api


def provision_and_verify(api: PbsApi, host: str, port: int, fingerprint: str, datastore: str, namespace: str,
                         replace_token: bool = False, hostname: str | None = None) -> ProvisionResult:
    """Weg A: Zugang anlegen und warten, bis die neuen Rechte wirksam sind.

    Wirft TokenExistsError, wenn der Token existiert und replace_token=False.
    """
    res = api.provision(datastore, hostname or socket.gethostname(), namespace, replace_token=replace_token)
    check = connect_token(host, port, fingerprint, res.auth_id, res.secret)
    if not check.wait_for_backup_permission(datastore, namespace):
        raise ApiError(tr("Zugang wurde angelegt, die Rechte sind auf dem Server aber nach 30 s noch nicht wirksam. Bitte in PBS prüfen und es später erneut versuchen."))
    return res


def verify_token_profile(profile: Profile, secret: str) -> None:
    """Weg B: prüft Zugangsdaten und Leserechte mit dem echten Client."""
    PbsClient(profile, secret).test_connection()
