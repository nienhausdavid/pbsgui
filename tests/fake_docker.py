#!/usr/bin/env python3
"""Simuliert `docker` für Tests (ps, inspect, exec, stop, start, run, logs). Zustand und Protokoll in $FAKE_DOCKER_STATE.

Zustand (JSON):
  sockets       {"<socket>": [<docker-inspect-Objekte>]}
  mysql_users   {"benutzer": "passwort"}          gültige Passwort-Anmeldungen (z. B. root)
  fail          ["container"]                      exec-Dump schlägt für diese Container fehl
  stopped       ["container"]                      gestoppte Container (nicht in `ps`, exec scheitert)
  socket_users  ["container"]                      Container mit passwortlosem Benutzer „mysql“ (unix_socket)
  no_mariadbd   ["container"]                      Container ohne mariadbd (z. B. MySQL-Images)
  fail_oneoff / fail_start   true                  der einmalige Start bzw. `docker start` schlägt fehl
  events        []                                 Reihenfolge der Ereignisse (stop/oneoff/start) für die Tests
  calls         []                                 alle Aufrufe (argv ohne stdin)
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


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


state.setdefault("calls", []).append(args)
state.setdefault("events", [])
for key in ("stopped", "socket_users", "no_mariadbd"):
    state.setdefault(key, [])
state.setdefault("oneoffs", {})
save()

if args[:1] != ["-H"]:
    die("fake docker: -H fehlt", 2)
sock = args[1].removeprefix("unix://")
args = args[2:]
containers = state["sockets"].get(sock)
if containers is None:
    die(f"Cannot connect to the Docker daemon at unix://{sock}. Is the docker daemon running?")
names = {c["Name"].lstrip("/"): c for c in containers}


def real(name):
    """Name des echten Containers hinter einem Wegwerf-Container (oder der Name selbst)."""
    return state["oneoffs"].get(name, name)


cmd = args[0]
if args[:2] == ["ps", "-q"]:
    print("\n".join(c["Id"] for c in containers if c["Name"].lstrip("/") not in state["stopped"]))

elif cmd == "inspect":
    ids = args[1:]
    print(json.dumps([c for c in containers if c["Id"] in ids or c["Name"].lstrip("/") in ids]))

elif cmd == "stop":
    name = args[-1]
    if name in state["oneoffs"]:
        state["events"].append(f"oneoff-stop {state['oneoffs'].pop(name)}")
    elif name in names:
        state["stopped"].append(name)
        state["events"].append(f"stop {name}")
    else:
        die(f"Error response from daemon: No such container: {name}")
    save()

elif cmd == "start":
    name = args[-1]
    if name not in names:
        die(f"Error response from daemon: No such container: {name}")
    if state.get("fail_start"):
        die("Error response from daemon: cannot start container")
    if name in state["stopped"]:
        state["stopped"].remove(name)
    state["events"].append(f"start {name}")
    save()

elif cmd == "logs":
    print("fake log line")

elif cmd == "run":
    # docker run -d --rm --name N --user mysql --entrypoint mariadbd --mount type=T,source=S,destination=D -v X:/pbsgui-init.sql:ro IMAGE ...
    name = args[args.index("--name") + 1]
    mount = next(a for a in args if a.startswith("type="))
    source = re.search(r"source=([^,]+)", mount).group(1)
    owner = next((n for n, c in names.items()
                  if any(m.get("Source") == source or m.get("Name") == source for m in c.get("Mounts") or [])), None)
    if owner is None:
        die(f"fake docker: kein Container mit Datenverzeichnis {source}")
    if owner not in state["stopped"]:
        die("mariadbd: Can't lock aria control file: Datenverzeichnis ist noch in Benutzung")   # Reihenfolge prüfen
    init = args[args.index("-v") + 1].split(":", 1)[0]
    if "--init-file=/pbsgui-init.sql" not in args or "unix_socket" not in Path(init).read_text():
        die("fake docker: init-file fehlt oder ohne unix_socket")
    if state.get("fail_oneoff"):
        die("docker: Error response from daemon: oci runtime error")
    state["oneoffs"][name] = owner
    state["socket_users"].append(owner)
    state["events"].append(f"oneoff {owner} image={args[args.index('--skip-networking') - 1]}")
    save()
    print("fake-container-id")

elif cmd == "exec":
    rest = args[1:]
    user, interactive = None, False
    while rest and rest[0].startswith("-"):
        if rest[0] == "-u":
            user, rest = rest[1], rest[2:]
        elif rest[0] == "-i":
            interactive, rest = True, rest[1:]
        else:
            die(f"fake docker: unbekannte exec-Option {rest[0]}", 2)
    name, script = rest[0], rest[rest.index("-c") + 1]
    extra = rest[rest.index("-c") + 3:] if "sh" in rest[rest.index("-c") + 2:] else []
    target = real(name)
    if name not in state["oneoffs"] and name not in names:
        die(f"Error response from daemon: No such container: {name}")
    if name in state["stopped"]:
        die(f"Error response from daemon: container {name} is not running")
    if script == "command -v mariadbd":
        if target in state["no_mariadbd"]:
            sys.exit(1)
        print("/usr/sbin/mariadbd")
    elif user == "mysql":   # passwortlose Anmeldung als Betriebssystem-Benutzer mysql (unix_socket)
        if target not in state["socket_users"]:
            die("ERROR 1045 (28000): Access denied for user 'mysql'@'localhost' (using password: NO)")
        if "mariadb-dump" in script:
            if target in state.get("fail", []):
                die("mariadb-dump: Got error: 2013: Lost connection")
            print(f"-- fake socket dump of {target} as mysql")
        else:
            print("1")
    elif target in state.get("fail", []):
        die("pg_dumpall: error: connection to server failed")
    elif "pg_dumpall" in script:
        print(f"-- fake postgres dump of {name}")
    else:
        stdin = sys.stdin.read().split("\n", 1) if interactive else ["", ""]
        pw, sql = stdin[0], (stdin[1] if len(stdin) > 1 else "")
        pwuser = extra[0] if extra else ""
        if state.get("mysql_users", {}).get(pwuser) != pw:
            die(f"ERROR 1045 (28000): Access denied for user '{pwuser}'@'localhost' (using password: YES)")
        if "mariadb-dump" in script:
            print(f"-- fake mysql dump of {name} as {pwuser}")
        else:  # Client: SQL ausführen
            m = re.search(r"CREATE USER IF NOT EXISTS '([^']+)'@'localhost' IDENTIFIED BY '([^']+)'", sql)
            if m:
                state.setdefault("mysql_users", {})[m.group(1)] = m.group(2)
                state.setdefault("sql", []).append(sql)
                save()
            else:
                print("1")
else:
    die(f"fake docker: unbekannter Befehl {args}", 2)
