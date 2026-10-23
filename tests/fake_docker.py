#!/usr/bin/env python3
"""Simuliert `docker` für Tests (ps, inspect, exec). Zustand und Protokoll in $FAKE_DOCKER_STATE (JSON).

Zustand:
  {"sockets": {"<socket>": [<docker-inspect-Objekte>]},
   "mysql_users": {"benutzer": "passwort"},     # gültige Anmeldungen (z. B. root)
   "fail": ["container"],                        # exec schlägt für diese Container fehl
   "calls": []}                                  # wird vom Fake gefüllt (argv ohne stdin)
Das Passwort kommt wie beim echten Aufruf als erste stdin-Zeile; es darf nie in argv stehen.
"""
import json
import os
import re
import sys
from pathlib import Path

state_file = Path(os.environ["FAKE_DOCKER_STATE"])
state = json.loads(state_file.read_text())
args = sys.argv[1:]


def save():
    state_file.write_text(json.dumps(state))


state.setdefault("calls", []).append(args)
save()

if args[:1] == ["-H"]:
    sock = args[1].removeprefix("unix://")
    args = args[2:]
    containers = state["sockets"].get(sock)
    if containers is None:
        print(f"Cannot connect to the Docker daemon at unix://{sock}. Is the docker daemon running?", file=sys.stderr)
        sys.exit(1)
else:
    print("fake docker: -H fehlt", file=sys.stderr)
    sys.exit(2)

if args[:2] == ["ps", "-q"]:
    print("\n".join(c["Id"] for c in containers))
elif args[0] == "inspect":
    ids = args[1:]
    print(json.dumps([c for c in containers if c["Id"] in ids]))
elif args[0] == "exec":
    rest = args[1:]
    interactive = rest[0] == "-i"
    rest = rest[1:] if interactive else rest
    name, script = rest[0], rest[rest.index("-c") + 1]
    extra = rest[rest.index("-c") + 3:] if "sh" in rest[rest.index("-c") + 2:] else []
    if name in state.get("fail", []):
        print("pg_dumpall: error: connection to server failed", file=sys.stderr)
        sys.exit(1)
    if not any(c["Name"].lstrip("/") == name for c in containers):
        print(f"Error response from daemon: No such container: {name}", file=sys.stderr)
        sys.exit(1)
    if "pg_dumpall" in script:
        print(f"-- fake postgres dump of {name}")
    else:
        stdin = sys.stdin.read().split("\n", 1) if interactive else ["", ""]
        pw, sql = stdin[0], (stdin[1] if len(stdin) > 1 else "")
        user = extra[0] if extra else ""
        if state.get("mysql_users", {}).get(user) != pw:
            print(f"ERROR 1045 (28000): Access denied for user '{user}'@'localhost' (using password: YES)",
                  file=sys.stderr)
            sys.exit(1)
        if "mariadb-dump" in script:
            print(f"-- fake mysql dump of {name} as {user}")
        else:  # Client: SQL ausführen
            m = re.search(r"CREATE USER IF NOT EXISTS '([^']+)'@'localhost' IDENTIFIED BY '([^']+)'", sql)
            if m:
                state.setdefault("mysql_users", {})[m.group(1)] = m.group(2)
                state.setdefault("sql", []).append(sql)
                save()
            else:
                print("1")
else:
    print(f"fake docker: unbekannter Befehl {args}", file=sys.stderr)
    sys.exit(2)
