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

def tree_for(snapshot_path):
    """Dateibaum für einen Snapshot: FAKE_PBS_TREES/<Snapshot-Pfad mit _ statt />, sonst FAKE_PBS_TREE."""
    many = os.environ.get("FAKE_PBS_TREES")
    if many and (Path(many) / snapshot_path.replace("/", "_")).is_dir():
        return str(Path(many) / snapshot_path.replace("/", "_"))
    return os.environ.get("FAKE_PBS_TREE")


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
    split = "--change-detection-mode" in args and args[args.index("--change-detection-mode") + 1] == "metadata"
    names = []
    for s in specs:   # Modus metadata: der Client teilt x.pxar in x.mpxar.didx (Metadaten) und x.ppxar.didx (Inhalt)
        base = s.split(":", 1)[0]
        names += [base.replace(".pxar", ".mpxar") + ".didx", base.replace(".pxar", ".ppxar") + ".didx"] if split else [base]
    snaps.append({"backup-type": "host", "backup-id": bid, "backup-time": int(time.time()) + len(snaps),
                  "size": 1024 * (len(snaps) + 1), "files": [{"filename": n} for n in names]})
    snaps_file.write_text(json.dumps(snaps))
    print("End Time: done")
elif args[:2] == ["key", "create"]:
    Path(args[2]).write_text(json.dumps({"kdf": None, "data": "ZmFrZQ==", "fingerprint": "aa:bb:cc:dd"}))
    os.chmod(args[2], 0o600)
elif args[:2] == ["catalog", "dump"]:
    tree = tree_for(args[2])
    if tree:  # echten Dateibaum im Format des Clients ausgeben (Rust-Debug-Quoting, oberste Ebene = Archivname.didx)
        def q(p):
            return '"' + p.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t") + '"'
        for arch in sorted(Path(tree).iterdir()):
            print(f"d {q('./' + (arch.name.replace('.pxar', '.mpxar') if os.environ.get('FAKE_PBS_SPLIT') else arch.name) + '.didx')}", file=sys.stderr)
            for root, dirs, files in os.walk(arch):
                dirs.sort()
                for name in sorted(dirs + files):
                    full = Path(root) / name
                    top = (arch.name.replace(".pxar", ".mpxar") if os.environ.get("FAKE_PBS_SPLIT") else arch.name) + ".didx"
                    rel = "./" + top + "/" + str(full.relative_to(arch))
                    if full.is_symlink():
                        print(f"l {q(rel)}", file=sys.stderr)
                    elif full.is_dir():
                        print(f"d {q(rel)}", file=sys.stderr)
                    else:
                        print(f"f {q(rel)} {full.stat().st_size} 2026-10-18T12:00:00Z", file=sys.stderr)
    else:
        print('d "./etc.pxar.didx"\nf "./etc.pxar.didx/hosts" 5 2026-10-18T12:00:00Z', file=sys.stderr)  # wie der echte Client: stderr
elif args[:1] == ["mount"]:
    tree = tree_for(args[1])
    if os.environ.get("FAKE_PBS_NO_MOUNT") or not tree:
        print("Error: mount not available", file=sys.stderr)
        sys.exit(1)
    src, target = Path(tree) / args[2], Path(args[3])
    if not src.is_dir():
        print(f"Error: archive {args[2]} not found", file=sys.stderr)
        sys.exit(1)
    target.rmdir()
    os.symlink(src, target)   # Testersatz für FUSE: der Einhängepunkt zeigt auf den Dateibaum
elif args[:1] == ["restore"] and args[3] == "-" and args[2].endswith(".img"):     # Abbild auf stdout (der Aufrufer schreibt aufs Gerät)
    src = Path(os.environ.get("FAKE_PBS_IMAGES", "")) / args[2]
    if not src.is_file():
        print(f"Error: archive {args[2]} not found", file=sys.stderr)
        sys.exit(1)
    print(f"restore image {args[2]}", file=sys.stderr)
    sys.stdout.buffer.write(src.read_bytes())
    sys.stdout.buffer.flush()
elif args[:2] == ["key", "create-master-key"]:    # Passphrase nur auf dem Terminal, wie der echte Client
    import base64
    sys.stdout.write("Master Key Password: "); sys.stdout.flush(); pw = sys.stdin.readline().strip()
    sys.stdout.write("\nVerify Password: "); sys.stdout.flush(); pw2 = sys.stdin.readline().strip()
    if pw != pw2:
        print("Error: passwords do not match", file=sys.stderr)
        sys.exit(1)
    raw = base64.b64encode(os.urandom(48)).decode()
    Path("master-public.pem").write_text("-----BEGIN PUBLIC KEY-----\n" + raw + "\n-----END PUBLIC KEY-----\n")
    Path("master-private.pem").write_text("-----BEGIN ENCRYPTED PRIVATE KEY-----\nfake:" + pw + "\n-----END ENCRYPTED PRIVATE KEY-----\n")
elif args[:2] == ["key", "import-with-master-key"]:
    master = Path(args[args.index("--master-keyfile") + 1]).read_text()
    enc = Path(args[args.index("--encrypted-keyfile") + 1]).read_bytes()
    sys.stdout.write("Master Key Password: "); sys.stdout.flush(); pw = sys.stdin.readline().strip()
    if "fake:" + pw not in master or not enc:
        print("Error: failed to read PEM-formatted private key - bad decrypt", file=sys.stderr)
        sys.exit(1)
    Path(args[2]).write_text(enc.decode())            # Test: die "verschlüsselte" Kopie ist der Klartext des Schlüssels
elif args[:1] == ["restore"] and args[2] in ("rsa-encrypted.key", "rsa-encrypted.key.blob"):
    blob = os.environ.get("FAKE_PBS_RSA")
    if not blob or (args[2].endswith(".blob") and not os.environ.get("FAKE_PBS_RSA_BLOBNAME")):
        print(f"Error: archive {args[2]} not found", file=sys.stderr)
        sys.exit(1)
    Path(args[3]).write_text(blob)
elif args[:1] == ["restore"] and args[3] == "-":     # Konfiguration eines VM-Snapshots auf stdout (qemu-server.conf)
    conf = os.environ.get("FAKE_PBS_VMCONF")
    if not conf or args[2] != "qemu-server.conf":
        print(f"Error: archive {args[2]} not found", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(Path(conf).read_text())
elif args[:1] == ["restore"] and args[2].endswith(".img"):    # VM-Abbild auf ein Gerät oder in eine Datei schreiben
    src = Path(os.environ.get("FAKE_PBS_IMAGES", "")) / args[2]
    if not src.is_file():
        print(f"Error: archive {args[2]} not found", file=sys.stderr)
        sys.exit(1)
    data = src.read_bytes()
    with open(args[3], "r+b" if Path(args[3]).exists() else "wb") as f:
        f.write(data)
    print(f"restore complete ({len(data)} bytes processed)")
elif args[:1] == ["restore"]:
    target = Path(args[3])
    target.mkdir(parents=True, exist_ok=True)
    tree = os.environ.get("FAKE_PBS_TREE")
    if tree:  # Integrationstests: echten Dateibaum zurückspielen (Archivname -> Unterordner)
        import subprocess
        src = Path(tree) / args[2]
        if not src.is_dir():
            print(f"Error: archive {args[2]} not found", file=sys.stderr)
            sys.exit(1)
        subprocess.run(["cp", "-a", f"{src}/.", str(target)], check=True)
    else:
        (target / "restored.txt").write_text("fake restore\n")
else:
    print(f"fake client: unbekannter Befehl {args}", file=sys.stderr)
    sys.exit(2)
