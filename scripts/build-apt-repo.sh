#!/bin/sh
# Baut ein signiertes APT-Repository (statische Dateien) aus allen .deb in DEBS_DIR.
# Aufruf: GPG_KEY_ID=<id> scripts/build-apt-repo.sh DEBS_DIR OUT_DIR [SUITE]
# Der Schlüssel muss im aktuellen GNUPGHOME liegen; optional GPG_PASSPHRASE.
set -eu
debs=${1:?DEBS_DIR fehlt}; out=${2:?OUT_DIR fehlt}; suite=${3:-stable}
: "${GPG_KEY_ID:?GPG_KEY_ID fehlt}"
rm -rf "$out"
pool="$out/pool/main/p/pbsgui"
bindir="$out/dists/$suite/main/binary-all"
mkdir -p "$pool" "$bindir"
cp "$debs"/*.deb "$pool/"
(cd "$out" && apt-ftparchive packages pool > "dists/$suite/main/binary-all/Packages")
gzip -9kn "$bindir/Packages"
# Alle Pakete sind Architecture: all. apt verlangt aber einen Index je Architektur des Systems (sonst "unterstützt die Architektur nicht"),
# daher dieselbe Paketliste auch für die üblichen Architekturen.
for arch in amd64 arm64; do
  mkdir -p "$out/dists/$suite/main/binary-$arch"
  cp "$bindir/Packages" "$bindir/Packages.gz" "$out/dists/$suite/main/binary-$arch/"
done
cat > "$out/apt-ftparchive.conf" <<CONF
APT::FTPArchive::Release {
  Origin "pbsgui";
  Label "pbsgui";
  Suite "$suite";
  Codename "$suite";
  Architectures "all amd64 arm64";
  Components "main";
  Description "pbsgui APT repository";
};
CONF
(cd "$out" && apt-ftparchive -c apt-ftparchive.conf release "dists/$suite" > "dists/$suite/Release")
rm "$out/apt-ftparchive.conf"
sign() { gpg --batch --yes --pinentry-mode loopback ${GPG_PASSPHRASE:+--passphrase "$GPG_PASSPHRASE"} --local-user "$GPG_KEY_ID" "$@"; }
sign --clearsign -o "$out/dists/$suite/InRelease" "$out/dists/$suite/Release"
sign -abs -o "$out/dists/$suite/Release.gpg" "$out/dists/$suite/Release"
gpg --batch --yes --export "$GPG_KEY_ID" > "$out/pbsgui.gpg"          # binär, für signed-by=
gpg --batch --yes --armor --export "$GPG_KEY_ID" > "$out/pbsgui.asc"  # ASCII
echo "APT-Repository in $out (Suite $suite)"
