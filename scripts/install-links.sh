#!/bin/sh
# Legt `pbsgui` und `pbstui` als Links nach ~/.local/bin, damit sie ohne Pfad aufrufbar sind.
# Debian/Ubuntu nehmen ~/.local/bin beim nächsten Login automatisch in den PATH auf, sobald der Ordner existiert.
set -eu
root=$(cd "$(dirname "$0")/.." && pwd)
bin=${XDG_BIN_HOME:-$HOME/.local/bin}
mkdir -p "$bin"
for name in pbsgui pbstui; do
  [ -x "$root/.venv/bin/$name" ] || { echo "Fehlt: $root/.venv/bin/$name (erst 'pip install -e .')" >&2; exit 1; }
  ln -sf "$root/.venv/bin/$name" "$bin/$name"
  echo "Link: $bin/$name"
done
case ":$PATH:" in *":$bin:"*) ;; *) echo "Hinweis: $bin ist noch nicht im PATH. Neu einloggen oder: export PATH=\"$bin:\$PATH\"" ;; esac
