"""Minimaler PBS-API-Client: Fingerprint holen, als root einloggen, Benutzer/Token/ACL anlegen.

Das Root-Passwort wird nur für den Login verwendet und nicht gespeichert.
NICHT gegen einen echten PBS getestet; Endpunkte nach der PBS-API-Doku.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status

    @property
    def already_exists(self) -> bool:
        return self.status in (400, 409) and re.search(r"already exist|exists", str(self), re.I) is not None


class TokenExistsError(ApiError):
    """Der Token existiert schon; sein Secret ist nicht mehr abrufbar. Nur ersetzen, wenn der Benutzer zustimmt."""


def _q(part: str) -> str:
    """Pfadteil für die API maskieren (kein `/`, `?` oder `..` aus Benutzereingaben im Pfad)."""
    return urllib.parse.quote(part, safe="")


def _ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # Vertrauen entsteht über den gepinnten Fingerprint
    return ctx


def _fmt(der: bytes) -> str:
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def normalize_fp(fp: str) -> str:
    return fp.replace(":", "").replace(" ", "").upper()


def fetch_fingerprint(host: str, port: int = 8007, timeout: float = 10) -> str:
    """SHA-256-Fingerprint des Serverzertifikats (zur Bestätigung durch den Benutzer)."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            with _ctx().wrap_socket(s, server_hostname=host) as t:
                return _fmt(t.getpeercert(binary_form=True))
    except OSError as e:
        raise ApiError(f"Keine Verbindung zu {host}:{port}: {e}") from None


@dataclass
class ProvisionResult:
    auth_id: str  # Token-ID, z. B. pbsgui-laptop@pbs!backup
    secret: str   # Token-Secret, wird nur einmal geliefert
    userid: str


class PbsApi:
    def __init__(self, host: str, port: int, fingerprint: str, timeout: float = 20):
        self.host, self.port, self.timeout = host, port, timeout
        self.fingerprint = normalize_fp(fingerprint)
        self._ticket = ""
        self._csrf = ""
        self._token = ""  # "PBSAPIToken=id:secret"

    # -- Transport ----------------------------------------------------------
    def _request(self, method: str, path: str, params: dict | None = None) -> object:
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=_ctx())
        try:
            conn.connect()
            seen = normalize_fp(_fmt(conn.sock.getpeercert(binary_form=True)))  # type: ignore[union-attr]
            if seen != self.fingerprint:
                raise ApiError("Zertifikat-Fingerprint stimmt nicht mit dem gespeicherten überein")
            headers = {"Accept": "application/json"}
            body = None
            query = ""
            data = urllib.parse.urlencode({k: str(v) for k, v in (params or {}).items()})
            if method == "GET":
                query = f"?{data}" if data else ""
            else:
                body = data
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            if self._ticket:
                headers["Cookie"] = f"PBSAuthCookie={urllib.parse.quote(self._ticket, safe='')}"
                if method != "GET":
                    headers["CSRFPreventionToken"] = self._csrf
            if self._token:
                headers["Authorization"] = self._token
            conn.request(method, f"/api2/json{path}{query}", body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
        except (OSError, ssl.SSLError) as e:
            raise ApiError(f"Verbindungsfehler: {e}") from None
        finally:
            conn.close()
        if resp.status >= 400:
            msg = raw
            try:
                msg = json.loads(raw).get("message") or raw
            except ValueError:
                pass
            raise ApiError(f"HTTP {resp.status}: {msg.strip()}", resp.status)
        try:
            return json.loads(raw).get("data")
        except ValueError:
            raise ApiError("Ungültige Antwort des Servers") from None

    # -- Anmeldung ----------------------------------------------------------
    def login(self, username: str, password: str, tfa_code: str = "") -> None:
        data = self._request("POST", "/access/ticket", {"username": username, "password": password})
        ticket = (data or {}).get("ticket", "")  # type: ignore[union-attr]
        if "!tfa!" in ticket:  # zweiter Faktor nötig
            if not tfa_code:
                raise ApiError("Zwei-Faktor-Code erforderlich")
            data = self._request("POST", "/access/ticket", {
                "username": username, "password": f"totp:{tfa_code}", "tfa-challenge": ticket})
        d = data or {}
        if not d.get("ticket"):  # type: ignore[union-attr]
            raise ApiError("Anmeldung fehlgeschlagen")
        self._ticket, self._csrf = d["ticket"], d.get("CSRFPreventionToken", "")  # type: ignore[index]

    def use_token(self, auth_id: str, secret: str) -> None:
        self._token = f"PBSAPIToken={auth_id}:{secret}"
        self._ticket = ""

    # -- Provisionierung ----------------------------------------------------
    def _ensure(self, method: str, path: str, params: dict) -> bool:
        """Legt etwas an; True = neu angelegt, False = gab es schon (kein Fehler)."""
        try:
            self._request(method, path, params)
            return True
        except ApiError as e:
            if e.already_exists:
                return False
            raise

    def ensure_namespace(self, datastore: str, namespace: str) -> None:
        """Legt `a/b/c` Ebene für Ebene an (die API kennt `parent` + `name`); vorhandene Ebenen sind ok."""
        parent = ""
        for part in [x for x in namespace.split("/") if x]:
            params = {"name": part, **({"parent": parent} if parent else {})}
            self._ensure("POST", f"/admin/datastore/{_q(datastore)}/namespace", params)
            parent = f"{parent}/{part}" if parent else part

    def provision(self, datastore: str, hostname: str, namespace: str = "",
                  role: str = "DatastoreBackup", token_name: str = "backup",
                  replace_token: bool = False) -> ProvisionResult:
        """Wiederholbar: vorhandener Benutzer/Namespace wird weiterverwendet.

        Existiert der Token schon, wird TokenExistsError geworfen, außer replace_token=True
        (dann wird er gelöscht und neu erzeugt; alte Nutzer dieses Tokens verlieren den Zugang).
        """
        userid = f"pbsgui-{hostname}@pbs"
        quser = urllib.parse.quote(userid, safe="")
        self._ensure("POST", "/access/users", {"userid": userid, "comment": f"pbsgui {hostname}"})
        tok_path = f"/access/users/{quser}/token/{_q(token_name)}"
        try:
            tok = self._request("POST", tok_path, {"comment": "pbsgui"})
        except ApiError as e:
            if not e.already_exists:
                raise
            if not replace_token:
                raise TokenExistsError(f"Token '{userid}!{token_name}' existiert bereits", e.status) from None
            self._request("DELETE", tok_path)
            tok = self._request("POST", tok_path, {"comment": "pbsgui"})
        tok = tok or {}
        auth_id = tok.get("tokenid") or f"{userid}!{token_name}"  # type: ignore[union-attr]
        secret = tok.get("value", "")  # type: ignore[union-attr]
        if not secret:
            raise ApiError("Server lieferte kein Token-Secret")
        acl_path = f"/datastore/{datastore}"
        if namespace.strip("/"):
            namespace = namespace.strip("/")
            self.ensure_namespace(datastore, namespace)
            acl_path += "/" + namespace
        # Benutzer UND Token brauchen eine ACL (effektive Rechte = Schnitt); PUT ist idempotent
        for who in (userid, auth_id):
            self._request("PUT", "/access/acl", {"path": acl_path, "role": role,
                                                  "auth-id": who, "propagate": 1})
        return ProvisionResult(auth_id=auth_id, secret=secret, userid=userid)

    def list_datastores(self) -> list[str]:
        """Datastores, die der angemeldete Benutzer/Token sehen darf."""
        data = self._request("GET", "/admin/datastore")
        return sorted(d["store"] for d in (data or []) if isinstance(d, dict) and d.get("store"))  # type: ignore[union-attr]

    def datastore_status(self, datastore: str) -> dict:
        """Belegung (`total`, `used`, `avail`) und, falls vorhanden, `gc-status` des Datastores. Braucht Audit-/Backup-Recht auf dem Datastore."""
        data = self._request("GET", f"/admin/datastore/{_q(datastore)}/status")
        return data if isinstance(data, dict) else {}

    def list_namespaces(self, datastore: str) -> list[str]:
        """Vorhandene Namespaces (ohne Wurzel), auch verschachtelte wie `a/b`."""
        data = self._request("GET", f"/admin/datastore/{urllib.parse.quote(datastore, safe='')}/namespace")
        return sorted(n["ns"] for n in (data or []) if isinstance(n, dict) and n.get("ns"))  # type: ignore[union-attr]

    def deprovision(self, userid: str, datastore: str = "", namespace: str = "", token_name: str = "backup",
                    role: str = "DatastoreBackup") -> None:
        """Entfernt ACLs, dann den Benutzer (samt Tokens).

        Gemessen an PBS 4.2: Beim Löschen des Benutzers bleibt dessen eigene ACL bestehen
        (nur die Token-ACL verschwindet), deshalb werden die ACLs vorher ausdrücklich entfernt.
        """
        if datastore:
            path = f"/datastore/{datastore}" + (f"/{namespace.strip('/')}" if namespace.strip("/") else "")
            for who in (f"{userid}!{token_name}", userid):
                try:
                    self._request("PUT", "/access/acl", {"path": path, "role": role, "auth-id": who, "delete": 1})
                except ApiError:
                    pass  # ACL gab es nicht (mehr)
        self._request("DELETE", f"/access/users/{urllib.parse.quote(userid, safe='')}")

    def check_backup_permission(self, datastore: str, namespace: str = "") -> bool:
        """Mit Token angemeldet: darf dieser Token in dem Datastore sichern?"""
        path = f"/datastore/{datastore}" + (f"/{namespace}" if namespace else "")
        data = self._request("GET", "/access/permissions", {"path": path})
        privs = {}
        if isinstance(data, dict):
            privs = data.get(path) or data
        return bool(isinstance(privs, dict) and privs.get("Datastore.Backup"))

    def wait_for_backup_permission(self, datastore: str, namespace: str = "", timeout: float = 30,
                                   interval: float = 2, sleep=time.sleep) -> bool:
        """Wartet, bis frisch vergebene ACLs wirksam sind.

        Gemessen an PBS 4.2: neue ACLs greifen erst nach mehreren Sekunden (zwischen 3 und 8 s),
        bis dahin liefert /access/permissions {} und der Client „permission check failed“.
        """
        waited = 0.0
        while True:
            if self.check_backup_permission(datastore, namespace):
                return True
            if waited >= timeout:
                return False
            sleep(interval)
            waited += interval
