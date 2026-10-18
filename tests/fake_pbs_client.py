#!/usr/bin/env python3
"""Simuliert `proxmox-backup-client` für Tests. Zustand in $FAKE_PBS_DIR.

Steuerung: FAKE_PBS_EXPECT_PASSWORD (sonst Auth-Fehler), FAKE_PBS_FAIL=1 (Backup schlägt fehl).
Jeder Aufruf wird (mit relevanten Umgebungsvariablen) in calls.jsonl protokolliert.
"""
import json
import os
import sys
import time
from pathlib import Path

state = Path(os.environ["FAKE_PBS_DIR"])
state.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
env = {k: os.environ.get(k) for k in ("PBS_REPOSITORY", "PBS_PASSWORD", "PBS_FINGERPRINT",
                                       "PBS_ENCRYPTION_PASSWORD")}
kf = {}
if "--keyfile" in args:  # Zustand der Schlüsseldatei zum Aufrufzeitpunkt festhalten
    kp = Path(args[args.index("--keyfile") + 1])
    kf = {"path": str(kp), "exists": kp.exists(), "content": kp.read_text() if kp.exists() else None,
          "mode": oct(kp.stat().st_mode & 0o777) if kp.exists() else None}
with open(state / "calls.jsonl", "a") as f:
    f.write(json.dumps({"argv": args, "env": env, "keyfile": kf}) + "\n")

expect = os.environ.get("FAKE_PBS_EXPECT_PASSWORD")
if args[:1] != ["version"] and expect is not None and env["PBS_PASSWORD"] != expect:
    print("Error: authentication failed - invalid credentials", file=sys.stderr)
    sys.exit(1)

snaps_file = state / "snapshots.json"
snaps = json.loads(snaps_file.read_text()) if snaps_file.exists() else []

if args[:1] == ["version"]:
    print("client version: 4.2.8")
elif args[:2] == ["snapshot", "list"]:
    print(json.dumps(snaps))
elif args[:1] == ["backup"]:
    if os.environ.get("FAKE_PBS_FAIL"):
        print("Error: upload failed: connection reset", file=sys.stderr)
        sys.exit(1)
    specs = [a for a in args[1:] if ".pxar:" in a]
    for s in specs:
        print(f"Upload directory '{s.split(':', 1)[1]}' to '{env['PBS_REPOSITORY']}' as {s.split(':', 1)[0]}")
    bid = args[args.index("--backup-id") + 1]
    snaps.append({"backup-type": "host", "backup-id": bid, "backup-time": int(time.time()) + len(snaps),
                  "size": 1024 * (len(snaps) + 1), "files": [{"filename": s.split(":", 1)[0]} for s in specs]})
    snaps_file.write_text(json.dumps(snaps))
    print("End Time: done")
elif args[:2] == ["key", "create"]:
    Path(args[2]).write_text(json.dumps({"kdf": None, "data": "ZmFrZQ==", "fingerprint": "aa:bb:cc:dd"}))
    os.chmod(args[2], 0o600)
elif args[:2] == ["catalog", "dump"]:
    print("d ./etc\nf ./etc/hosts\nd ./home")
elif args[:1] == ["restore"]:
    target = Path(args[3])
    target.mkdir(parents=True, exist_ok=True)
    (target / "restored.txt").write_text("fake restore\n")
else:
    print(f"fake client: unbekannter Befehl {args}", file=sys.stderr)
    sys.exit(2)
