#!/bin/sh
# Bereitet ein Release vor: Version erhöhen, CHANGELOG abschließen, Tests, Commit und Tag.
# Aufruf:  scripts/release.sh X.Y.Z [--push]
# Ohne --push bleibt alles lokal (zum Prüfen); mit --push startet der Tag den GitHub-Workflow "Release"
# (baut .deb und ISO, legt das Release an und aktualisiert das APT-Repository).
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
new=${1:?Version angeben, z. B. scripts/release.sh 0.4.0}
push=${2:-}
echo "$new" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || { echo "Keine gültige Version (X.Y.Z): $new" >&2; exit 1; }
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || { echo "Bitte auf dem Branch main arbeiten." >&2; exit 1; }
[ -z "$(git status --porcelain)" ] || { echo "Arbeitsverzeichnis nicht sauber: erst committen." >&2; exit 1; }
git fetch -q origin
[ "$(git rev-list --count HEAD..origin/main)" = 0 ] || { echo "origin/main ist weiter: erst git pull." >&2; exit 1; }
git rev-parse "v$new" >/dev/null 2>&1 && { echo "Tag v$new existiert schon." >&2; exit 1; }
old=$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml | head -1)
[ "$new" != "$old" ] || { echo "Version ist schon $new (pyproject.toml); für ein Re-Release neue Nummer wählen." >&2; exit 1; }
# CHANGELOG: es muss etwas unter [Unreleased] stehen
python3 - "$new" <<'PY'
import re, sys, datetime
new = sys.argv[1]
text = open("CHANGELOG.md", encoding="utf-8").read()
m = re.search(r"## \[Unreleased\]\n(.*?)(?=\n## \[)", text, re.S)
if not m or not m.group(1).strip():
    sys.exit("CHANGELOG.md: unter [Unreleased] steht nichts. Erst die Änderungen eintragen.")
today = datetime.date.today().isoformat()
text = text.replace("## [Unreleased]\n", f"## [Unreleased]\n\n## [{new}] - {today}\n", 1)
text = re.sub(r"\[Unreleased\]: (\S+)/compare/v\S+\.\.\.HEAD",
              lambda mm: f"[Unreleased]: {mm.group(1)}/compare/v{new}...HEAD", text)
text = text.replace("[Unreleased]: ", f"[Unreleased]: ", 1)
link = re.search(r"\[Unreleased\]: (\S+)/compare", text).group(1)
text = re.sub(r"(\[Unreleased\]: [^\n]*\n)", lambda mm: mm.group(1) + f"[{new}]: {link}/releases/tag/v{new}\n", text, count=1)
open("CHANGELOG.md", "w", encoding="utf-8").write(text)
PY
sed -i "s/^version = \"$old\"/version = \"$new\"/" pyproject.toml
echo "Tests laufen …"
py=${PYTHON:-python3}
[ -z "${PYTHON:-}" ] && [ -x .venv/bin/python ] && py=.venv/bin/python
if ! PYTHONPATH="$root/src" "$py" -m pytest -q; then
  git checkout -q -- pyproject.toml CHANGELOG.md      # saubere Ausgangslage wiederherstellen
  echo "Tests fehlgeschlagen: Release abgebrochen, nichts wurde committet oder getaggt." >&2
  exit 1
fi
git add pyproject.toml CHANGELOG.md
git commit -q -m "Release v$new"
git tag -a "v$new" -m "pbsgui $new"
echo "Lokal bereit: Commit und Tag v$new."
if [ "$push" = "--push" ]; then
  git push origin main && git push origin "v$new"
  echo "Gepusht: der Workflow 'Release' baut jetzt .deb, ISO und das APT-Repository."
else
  echo "Zum Veröffentlichen:  git push origin main && git push origin v$new"
fi
