"""Gemeinsame Test-Bausteine für Datenbank-Dumps: Fake-docker mit zwei Daemons (root und rootless)."""
import json
from pathlib import Path

import pytest

from pbsgui.core import dbdump

FAKE_DOCKER = str(Path(__file__).parent / "fake_docker.py")


def container(name, *dests, image="x"):
    return {"Id": f"id-{name}", "Name": f"/{name}", "Config": {"Image": image},
            "Mounts": [{"Destination": d, "Type": "bind"} for d in dests]}


@pytest.fixture
def dock(tmp_path, monkeypatch):
    """Zwei Daemons: root (Socket A) und ein rootless-Daemon (Socket B)."""
    sa, sb = str(tmp_path / "a.sock"), str(tmp_path / "b.sock")
    state = tmp_path / "docker-state.json"
    state.write_text(json.dumps({
        "sockets": {sa: [container("web", "/data"), container("pg_main", "/var/lib/postgresql/data", image="postgres:16")],
                    sb: [container("immich_postgres", "/var/lib/postgresql/data", image="ghcr.io/immich-app/postgres:14"),
                         container("firefly_db", "/var/lib/mysql", image="mariadb:11"),
                         container("cache", "/data", image="valkey/valkey:9"),
                         container("postgres_ohne_daten", image="postgres:16")]},
        "mysql_users": {"root": "adminpw"}}))
    monkeypatch.setenv("FAKE_DOCKER_STATE", str(state))
    monkeypatch.setenv("PBSGUI_DOCKER", FAKE_DOCKER)
    monkeypatch.setattr(dbdump, "discover_sockets", lambda *a, **k: [sa, sb])  # nie die echten Sockets des Hosts

    class D:
        sockets = [sa, sb]
        path = state

        def data(self):
            return json.loads(state.read_text())

        def update(self, **kw):
            d = self.data()
            d.update(kw)
            state.write_text(json.dumps(d))
    return D()
