"""Verbindung eines vorhandenen Profils nachträglich ändern (Server, Port, Fingerprint, Datastore, Namespace, Token).

Profilname, Schlüssel, Quellen, Zeitplan und Verlauf bleiben. Neue Angaben werden vor dem Speichern mit dem echten Client
geprüft; schlägt das fehl, bleibt das Profil unverändert.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from ..i18n import tr
from .client import ClientError, PbsClient
from .manager import ProfileManager
from .models import Profile
from .scheduler import SchedulerError


class ConnectionChangeError(RuntimeError):
    """Ungültige oder nicht prüfbare Änderung der Verbindung."""


@dataclass
class ConnectionResult:
    profile: Profile
    changed: list[str] = field(default_factory=list)       # geänderte Felder
    new_target: bool = False                               # Backups gehen künftig an ein anderes Ziel
    token_replaced: bool = False
    notes: list[str] = field(default_factory=list)


FIELDS = ("server", "port", "fingerprint", "datastore", "namespace", "auth_id")


def change_connection(mgr: ProfileManager, profile: Profile, *, server: str | None = None, port: int | None = None,
                      fingerprint: str | None = None, datastore: str | None = None, namespace: str | None = None,
                      auth_id: str | None = None, token: str | None = None, verify: bool = True) -> ConnectionResult:
    """Übernimmt die angegebenen Änderungen (None = unverändert). Wirft ConnectionChangeError, wenn die Prüfung scheitert."""
    new = dataclasses.replace(profile)
    for name, value in (("server", server), ("port", port), ("fingerprint", fingerprint), ("datastore", datastore),
                        ("namespace", namespace), ("auth_id", auth_id)):
        if value is not None:
            setattr(new, name, value.strip().strip("/") if name == "namespace" else (value.strip() if isinstance(value, str) else value))
    if not new.server or not new.datastore or not new.auth_id:
        raise ConnectionChangeError(tr("Server, Datastore und Token-ID dürfen nicht leer sein"))
    if not 1 <= int(new.port) <= 65535:
        raise ConnectionChangeError(tr("Ungültiger Port"))
    changed = [f for f in FIELDS if getattr(new, f) != getattr(profile, f)]
    if token:
        changed.append("token")
    if not changed:
        return ConnectionResult(profile, [])
    res = ConnectionResult(new, changed)
    secret = token or mgr.secrets.get(profile.slug, "token")
    if "auth_id" in changed and not token:
        raise ConnectionChangeError(tr("Bei einer anderen Token-ID wird auch das zugehörige Token-Secret gebraucht"))
    if not secret:
        raise ConnectionChangeError(tr("Kein Token-Secret gespeichert: bitte das Secret angeben"))
    if ("server" in changed or "port" in changed) and "fingerprint" not in changed:
        res.notes.append(tr("Server oder Port geändert, der Fingerprint blieb gleich: er muss zum Zertifikat des neuen Servers passen."))
    if verify:
        try:
            PbsClient(new, secret).test_connection()
        except ClientError as e:
            raise ConnectionChangeError(tr("Verbindung mit den neuen Angaben nicht möglich, nichts wurde geändert: {0}", e)) from None
    res.new_target = any(f in changed for f in ("server", "port", "datastore", "namespace"))
    res.token_replaced = bool(token)
    if res.new_target:
        res.notes.append(tr("Vorhandene Snapshots bleiben auf dem alten Ziel. Das nächste Backup lädt am neuen Ziel alles neu hoch (Deduplizierung gilt je Datastore). Der Verschlüsselungs-Schlüssel bleibt derselbe und passt weiter."))
    try:
        mgr.save(new, token=token or None)
    except SchedulerError as e:
        raise ConnectionChangeError(str(e)) from None
    return res
