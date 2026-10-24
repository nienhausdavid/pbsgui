# Wiederherstellung mit der Rettungs-ISO

[English](RESTORE.md) · **Deutsch**

So spielst du ein **Vollbackup** auf einem neuen oder leeren PC zurück. Du brauchst:

| Was | Wo es herkommt |
|---|---|
| Die Rettungs-ISO `pbsgui-rescue-<version>-amd64.iso` | Release-Anhang auf GitHub oder selbst gebaut (`scripts/build-iso.sh`) |
| Einen USB-Stick zum Booten (≥ 2 GB) | `sudo dd if=pbsgui-rescue-*.iso of=/dev/sdX bs=4M status=progress conv=fsync` (**Achtung:** `/dev/sdX` ist der Stick) |
| Das **Notfallblatt** | `pbsgui recovery NAME --sheet DATEI`, ausgedruckt oder im Passwortmanager |
| Den **Schlüssel** (`key.json`) auf einem USB-Stick | `pbsgui key export NAME DATEI` – ohne ihn ist ein verschlüsseltes Backup nicht lesbar |
| Das **Token-Secret** des Backup-Zugangs | Passwortmanager; notfalls in PBS ein neues Token mit Leserecht erzeugen |
| Die **LUKS-Passphrase** des alten Systems | Nur falls die Platte verschlüsselt war |
| Eine **Ziel-Platte** mindestens so groß wie die alte | Wird komplett überschrieben! |

> Die Wiederherstellung ist **zerstörerisch** für die Ziel-Platte. Der Assistent zeigt vorher einen Plan und verlangt,
> dass du den Gerätenamen eintippst. Eingehängte Platten und der Rettungs-Stick werden nie als Ziel angeboten.

## 1. Vom Rettungsmedium starten

Stick einstecken, PC starten und im Boot-Menü (meist `F12`, `F11`, `Esc` oder `Entf`) den Stick wählen. Das
Das Boot-Menü (UEFI: GRUB, BIOS: syslinux) startet nach 5 Sekunden von selbst. Nach etwa einer halben Minute
erscheint der Wiederherstellungs-Assistent automatisch auf der ersten Konsole.

![Der Assistent startet automatisch](images/01-start.png)

Kein Netzwerk? Mit `F2` (oder dem Knopf „Netzwerk“) startet `nmtui` (WLAN/LAN einrichten). Lässt sich der Assistent später neu starten:
`pbsgui rescue`. Die Tastatur ist auf Deutsch gestellt.

## VPN und Updates (Tasten `F3` und `F4`)

**Updates beim Start:** Kurz nach dem Booten prüft die ISO (wenn ein Netzwerk da ist), ob es neuere Versionen von
`pbstui`, `proxmox-backup-client`, WireGuard, OpenVPN und NetBird gibt, und installiert sie. Der Assistent wartet dafür
bis zu 45 Sekunden (eine Taste überspringt das Warten). Ohne Netzwerk wird alles übersprungen und alle 2 Minuten erneut
versucht; von Hand geht es mit `F4` im Assistenten oder `pbsgui update`. Wurde `pbstui` selbst aktualisiert, bietet der
Assistent „Neu starten“ an.

**VPN zum Backup-Server:** Liegt der Server hinter einem VPN, richtest du es vor dem ersten Schritt mit `F3` (oder dem Knopf „VPN“) ein
(oder auf der Kommandozeile mit `pbsgui vpn`):

| Typ | Was du brauchst | Kommandozeile |
|---|---|---|
| WireGuard | `wg0.conf` vom USB-Stick (Dateiname = Schnittstelle, höchstens 15 Zeichen) | `pbsgui vpn up wireguard /media/usb/wg0.conf` |
| OpenVPN | `client.ovpn` (Zertifikate daneben oder eingebettet), optional Benutzer und Passwort | `pbsgui vpn up openvpn /media/usb/client.ovpn --user NAME` |
| NetBird | Setup-Key, bei eigener Instanz die Management-URL | `NB_SETUP_KEY=… pbsgui vpn up netbird --management-url https://netbird.example.org` |

`pbsgui vpn find` sucht Konfigurationen auf USB-Sticks, `pbsgui vpn status` zeigt die Tunnel, `pbsgui vpn down` trennt sie.
Passwörter und Setup-Keys gehen nie als Programmargument; OpenVPN-Zugangsdaten liegen nur kurz im RAM und werden beim
Trennen gelöscht. Danach die Verbindung zum Server wie gewohnt prüfen.

![VPN einrichten (F3)](images/11-vpn.png)

## Web-Oberfläche (für Server und VPS)

Hast du nur eine einfache Konsole (z. B. bei einem VPS), richtest du die Wiederherstellung im Browser ein:

1. Auf der Konsole `F5` (Knopf „Web“) oder `pbsgui web --daemon`. Angezeigt werden die **Adresse**, ein **zufälliges Passwort**
   aus vier Wörtern, jedes mit einer zweistelligen Zahl dahinter (z. B. `abacus47-grind83-lemon12-quest66`) und der **Fingerabdruck** des Zertifikats.
2. Im Browser die Adresse öffnen (Warnung wegen des selbstsignierten Zertifikats), den Fingerabdruck mit der Konsole
   **vergleichen** und mit dem Passwort anmelden.
3. Die Schritte sind dieselben wie im Assistenten: VPN (Konfiguration einfügen), Server, Snapshot, Platte und
   Verschlüsselung, Plan prüfen, Gerätenamen eintippen, Live-Log, Neustart.

`pbsgui web --status` zeigt die Zugangsdaten und `pbsgui web --stop` beendet die Oberfläche. Mit `--bind ADRESSE` lauscht sie nur
an einer Adresse; `--port` und `--timeout` (Minuten ohne Anfrage, danach schaltet sie sich ab,
Standard 60) sind einstellbar.

**Schutz:** HTTPS mit frischem Zertifikat und frischem Passwort bei jedem Aktivieren; Rate Limiting je Adresse; nach 5 falschen
Passwörtern wird die Adresse gesperrt (mit wachsender Dauer); nach 40 Fehlversuchen insgesamt schaltet sich die Oberfläche ab
(neu aktivieren auf der Konsole). **Honeypot:** ein unsichtbares Formularfeld und Köder-Adressen (`/admin`, `/wp-login.php`,
`/.env` …), die nur Bots aufrufen. Wer sie berührt, wird für Stunden gesperrt, bekommt nur noch verzögerte Antworten, und die
Konsole zeigt den Treffer. Es gibt immer nur eine Sitzung, sie läuft nach 15 Minuten Leerlauf ab, und jede Änderung braucht ein
CSRF-Token. Das Löschen einer Platte verlangt wie überall das Eintippen des Gerätenamens.

## 2. Verbindung zum Backup-Server

Die Felder Server, Datastore, Namespace, Backup-ID und Token-ID kommen von deinem **Notfallblatt** (bei einer
personalisierten ISO, `scripts/build-iso.sh … --profile NAME`, sind sie schon ausgefüllt).

![Schritt 1: Verbindung](images/02-verbindung.png)

1. **Token-Secret** eintragen.
2. **Fingerprint** mit dem Wert auf dem Notfallblatt vergleichen (oder „Fingerprint abrufen“ und prüfen).
3. **Schlüsseldatei:** USB-Stick einstecken und „USB-Stick nach Schlüsseldatei durchsuchen“ wählen, oder den Pfad von
   Hand eintragen (z. B. `/media/pbsgui/sdb1/key.json`).

![Schritt 1: Zugangsdaten ausgefüllt](images/03-zugangsdaten.png)

Mit **Weiter** sucht der Assistent nach Vollbackups. Es werden nur Snapshots angeboten, die Wiederherstellungsdaten
enthalten (Backups, die mit `pbsgui … --mode full` erstellt wurden).

## 3. Snapshot wählen

![Schritt 2: Snapshot](images/04-snapshot.png)

Der neueste steht oben. Der Assistent lädt jetzt nur das kleine Archiv mit den Layout-Daten (Sekunden), nicht das
ganze System.

## 4. Ziel-Platte wählen

![Schritt 3: Ziel-Platte](images/05-platte.png)

Wähle die Platte und entscheide, wie die neue Platte **verschlüsselt** werden soll:

| Auswahl | Was passiert | Du brauchst |
|---|---|---|
| **Wie im Original** (Standard) | Das System wird so aufgebaut, wie es war. War es mit LUKS verschlüsselt, wird der gesicherte LUKS-Header zurückgespielt. | die alte LUKS-Passphrase (falls das Original verschlüsselt war) |
| **Neu mit LUKS verschlüsseln** | Ein neuer LUKS2-Container mit neuer Passphrase, auch wenn das Original unverschlüsselt war. Die Datenpartitionen (nicht EFI und nicht `/boot`) kommen hinein, `crypttab` und Initramfs werden angepasst. | eine neue Passphrase (mindestens 8 Zeichen, zweimal) und eine getrennte `/boot`-Partition im Original |
| **Ohne Verschlüsselung** | Die Platte wird unverschlüsselt aufgebaut, der alte `crypttab`-Eintrag wird deaktiviert. | nichts |

![Verschlüsselung wählen](images/09-verschluesselung.png)

Bei „neu verschlüsseln“ erscheinen die Felder für die neue Passphrase:

![Neue Passphrase eingeben](images/10-neue-passphrase.png)

Gibt es im Zielsystem noch kein `cryptsetup-initramfs` (Original war unverschlüsselt), installiert der Assistent
es im neuen System nach; dafür braucht der Rechner Netzwerk. Dateisystem- und LVM-Kennungen bleiben in allen Fällen
dieselben; nur der LUKS-Container bekommt bei „neu“ eine neue UUID.

### Kleinere Ziel-Platte („Größe anpassen“)

Ist die gewählte Platte **kleiner** als die Original-Platte (Beispiel: Original 512 GB mit 50 GB belegt, Ziel 128 GB),
erscheint unter der Platten-Liste die Option **„Größe anpassen“**. Ohne Haken bricht der Assistent mit „Ziel-Platte zu
klein“ ab. Mit Haken berechnet er das Layout neu:

- **EFI-Partition und `/boot` behalten ihre Größe**, ebenso BIOS-Boot und unbekannte Partitionen.
- **Swap** (Partition oder LV) wird im Verhältnis Ziel/Original verkleinert, mindestens 1 GiB, nie größer als vorher.
- Alle übrigen **Partitionen und Logical Volumes** teilen sich den Rest im Verhältnis ihrer alten Größen, nie größer als
  vorher. Jedes Dateisystem bekommt mindestens **belegten Platz + 20 % Reserve, mindestens 2 GiB**. Wer damit auf sein
  Minimum fällt, behält es; die anderen teilen den verbleibenden Platz. Die Verteilung ist deterministisch.
- Passt es nicht, nennt die Fehlermeldung die **nötige Mindestgröße** der Ziel-Platte.
- **Alle UUIDs bleiben gleich** (Partitionen, Dateisysteme, LUKS im Modus „wie im Original“, PV/VG/LV); `fstab`,
  `crypttab` und GRUB bleiben gültig. Die Partitionstabelle wird neu geschrieben, der GPT-Sicherungs-Header liegt am
  Ende der kleineren Platte. Alle drei Verschlüsselungs-Modi funktionieren.
- Der Plan im nächsten Schritt zeigt **alt -> neu** für jede Partition und jedes LV.

Der belegte Platz stammt aus den Wiederherstellungsdaten (`fsusage.json`, ab pbsgui 0.6 im Backup). Bei älteren
Backups schätzt pbsgui ihn aus der Snapshot-Größe und weist im Plan darauf hin.
Nicht unterstützt: Volume Groups über mehrere Platten, Thin-Pools/Snapshots/RAID-LVs, erweiterte MBR-Partitionen.

## 5. Plan prüfen und bestätigen

![Schritt 4: Bestätigen](images/06-bestaetigen.png)

Du siehst, was gebaut wird: Partitionstabelle, LUKS, LVM, Dateisysteme (mit den **alten UUIDs**, damit `fstab`,
`crypttab` und GRUB-Konfiguration gültig bleiben). Zum Fortfahren den Gerätenamen eintippen, dann
**Wiederherstellen**. „Trockenlauf“ zeigt nur die Befehle an und ändert nichts.

## 6. Wiederherstellung läuft

![Schritt 5: läuft](images/07-laeuft.png)

Der Assistent legt das Layout an, spielt `root.pxar` zurück, bindet `/dev`, `/proc`, `/sys` ein und erzeugt im
neuen System Initramfs und Bootloader neu (UEFI inklusive Fallback-Eintrag `EFI/BOOT/BOOTX64.EFI`, damit auch neue
Hardware bootet). Die Dauer hängt von Datenmenge und Netzwerk ab.

![Fertig](images/08-fertig.png)

Bei „Fertig“: Rettungsstick abziehen, **Neu starten**. Bei verschlüsselter Platte fragt der Start nach der
LUKS-Passphrase.

## VM-Backups von Proxmox VE (V2P)

Neben pbsgui-Vollbackups kann die ISO auch **VM-Backups** von Proxmox VE (Snapshots `vm/<id>/…` mit Abbild-Archiven wie
`drive-scsi0.img`) auf **physische Platten** zurückspielen (V2P, Virtual to Physical). Auswahl im Assistenten: Haken
„VM-Abbild wiederherstellen“ im ersten Schritt; in der Web-Oberfläche „Art der Wiederherstellung“; auf der Kommandozeile
`pbsgui rescue vm-snapshots` und `vm-run`.

1. **Snapshot und Platten:** Jede Platte der VM (`scsi0`, `virtio1` …) bekommt eine Ziel-Platte; Platten ohne Ziel werden nicht
   wiederhergestellt. Die Boot-Platte wird aus der VM-Konfiguration (`boot: order=…`) erkannt. EFI-Variablen (`efidisk0`) und
   TPM-Zustand der VM werden nicht geschrieben. Die Ziel-Platte muss mindestens so groß sein wie das Abbild.
2. **Schreiben:** Das Abbild geht per `proxmox-backup-client restore` Block für Block auf die Platte; die Anzeige zeigt den Stand.
3. **Reparaturen** (alle im Plan sichtbar, einzeln abschaltbar; gelten für Debian und Ubuntu als Gast):
   - **Größe:** Ist die Ziel-Platte größer, wird die GPT korrigiert und die letzte Partition samt LUKS, LVM und Dateisystem
     (ext4, xfs, btrfs) auf den Rest der Platte vergrößert. Ein Dateisystem direkt auf der Platte (ohne Partitionstabelle)
     wird nur bei ext vergrößert und nie mit einer Partitionstabelle überschrieben.
   - **Initramfs und Bootloader:** `MODULES=most` (Treiber für echte Hardware wie NVMe und AHCI), `update-initramfs`, GRUB für UEFI mit
     Fallback `EFI/BOOT/BOOTX64.EFI` oder für BIOS. Ein geöffneter LUKS-Container wird so benannt wie in der `crypttab` des Gastes,
     damit das neue Initramfs nach der Passphrase fragt.
   - **Netzwerk:** Eine nicht mehr vorhandene Schnittstelle (z. B. `ens3`) wird in `/etc/network/interfaces`, Netplan,
     systemd-networkd und NetworkManager durch die einzige echte Netzwerkkarte ersetzt (Sicherung `*.pbsgui-orig`). Bei mehreren Karten
     oder Mustern wie `Name=en*` ändert pbsgui nichts und warnt.
   - **Gast-Dienste:** `qemu-guest-agent` wird abgeschaltet und `cloud-init` mit `/etc/cloud/cloud-init.disabled` stillgelegt (nichts wird
     gelöscht); ein `console=ttyS0` in der GRUB-Konfiguration wird gemeldet.
4. **Bestätigung:** Zum Starten **alle** Ziel-Platten eintippen. Ein Trockenlauf zeigt jeden Schritt ohne Änderung.

```bash
pbsgui rescue vm-snapshots --server pbs.example.org --datastore main --auth-id user@pbs!tok --vmid 100
PBSGUI_TOKEN_SECRET=… PBSGUI_LUKS_PASSPHRASE=… pbsgui rescue vm-run --server pbs.example.org --datastore main \
  --auth-id user@pbs!tok --vmid 100 --map scsi0=/dev/nvme0n1 --map scsi1=/dev/sdb \
  --confirm-disk /dev/nvme0n1 --confirm-disk /dev/sdb --dry-run      # erst ansehen, dann ohne --dry-run
# Reparaturen abschalten: --no-grow --no-initramfs --no-bootloader --no-network --no-services
```

**Grenzen:** Nur Linux-Gäste (Windows und Solaris werden abgelehnt). Kleinere Ziel-Platten als das Abbild gehen nicht (dafür das
Dateimodus-Backup nehmen). Die Reparatur ist nur für Debian und Ubuntu eingebaut; bei anderen Systemen wird das Abbild geschrieben und
auf die manuellen Schritte hingewiesen. **Getestet:** in QEMU mit UEFI: ein VM-Backup mit LUKS, LVM und zwei Platten auf größere Platten
wiederhergestellt, bootet, Dateien stimmen, Root von 3,5 auf 7,1 GiB gewachsen. Nicht getestet: ein echtes, von Proxmox VE selbst
erzeugtes Backup, BIOS-Gäste und Secure Boot.

## Ohne Assistent: Kommandozeile

```bash
pbsgui rescue disks                                        # mögliche Ziel-Platten
pbsgui rescue keys                                         # Schlüsseldateien auf USB-Sticks suchen
pbsgui rescue snapshots --server pbs.example.org --datastore main --auth-id user@pbs!tok --backup-id mein-pc
PBSGUI_TOKEN_SECRET=… PBSGUI_LUKS_PASSPHRASE=… \
  pbsgui rescue run --server pbs.example.org --datastore main --auth-id user@pbs!tok --backup-id mein-pc \
  --keyfile /media/pbsgui/sdb1/key.json --disk /dev/sda --dry-run      # erst ansehen, dann ohne --dry-run
# Verschlüsselung wählen: --encryption keep (Standard) | new (PBSGUI_NEW_LUKS_PASSPHRASE setzen) | none
# Kleinere Ziel-Platte als das Original: --shrink (Größe anpassen); interaktiv wird es automatisch angeboten
```

## Wenn etwas nicht klappt

| Meldung | Ursache und Abhilfe |
|---|---|
| „Kein Vollbackup mit Wiederherstellungsdaten gefunden“ | Backup-ID/Namespace prüfen. Nur Vollbackups ab pbsgui 0.2 (`--mode full`) enthalten die Layout-Daten. |
| „missing key“ | Das Backup ist verschlüsselt: im ersten Schritt die exportierte Schlüsseldatei angeben. |
| „LUKS-Header … fehlt im Backup“ | Das Backup war unverschlüsselt; der Header wird nur in verschlüsselte Backups gelegt. Ausweg: „neu verschlüsseln“ oder „ohne Verschlüsselung“ wählen. |
| „Neue Verschlüsselung braucht eine getrennte /boot-Partition“ | GRUB kann ein verschlüsseltes Root-Dateisystem ohne eigenes `/boot` nicht lesen: „wie im Original“ oder „ohne“ wählen. |
| „Ziel-Platte zu klein“ | Die neue Platte ist kleiner als die alte: „Größe anpassen“ ankreuzen bzw. `--shrink` angeben. |
| „Ziel-Platte zu klein, auch mit ‚Größe anpassen‘ … mindestens X GiB nötig“ | Die belegten Daten plus Reserve passen nicht: größere Platte nehmen (mindestens X GiB) oder vorher im Original aufräumen und neu sichern. |
| Keine Ziel-Platte angeboten | Die Platte ist eingehängt oder ist der Rettungsstick. `lsblk` und `umount` prüfen. |
| System bootet nicht (neue Hardware) | Rettungsstick erneut starten, `pbsgui rescue` nicht erneut ausführen, sondern manuell: Ziel einhängen, `chroot`, `update-initramfs -u -k all`, `grub-install`, `update-grub`. |

## Getestet

Der ganze Weg wurde in QEMU mit UEFI-Firmware durchgespielt: Vollbackup eines Debian-13-Testsystems (GPT, ESP,
getrenntes `/boot`, LUKS, LVM, ext4, Swap) in einen PBS-Datastore, Wiederherstellung per ISO-Assistent (mit
simulierten Tastendrücken, daher stammen die Bilder aus diesem Lauf) auf eine leere Platte, danach Boot der
wiederhergestellten Platte über den Fallback-Bootloader, Entsperren per LUKS-Passphrase. Geprüft wurden Prüfsummen,
Rechte, Besitzer, Links und dass **alle UUIDs und PARTUUIDs identisch** sind. Die Bilder zeigen Testdaten
(Test-Server, Test-Token).

Die drei Verschlüsselungs-Varianten sind einzeln getestet: **wie im Original** (komplett über den Assistenten),
**ohne Verschlüsselung** (System bootet ohne Passphrase-Abfrage, `crypttab` deaktiviert) und **neu verschlüsseln**
(System bootet mit der neuen Passphrase, `crypttab` zeigt auf die neue UUID); die beiden letzten per Kommandozeile in
der ISO. Zusätzlich prüft ein Test mit echten Loop-Geräten alle drei Varianten.

Nicht getestet: echte Hardware, BIOS-Boot der ISO, Secure Boot, WLAN, andere Dateisysteme als ext4/vfat/swap im
VM-Test (der Plan-Code kennt xfs und btrfs und ist per Unit-Test abgedeckt).

## Grenzen

- Dateibasiertes Backup, **kein Abbild**: laufende Datenbanken können inkonsistent sein.
- Secure Boot und TPM-gebundene LUKS-Schlüssel (`systemd-cryptenroll`) werden nicht übernommen.
- Version 1: eine System-Platte; btrfs-Subvolumes, mdadm-RAID, ZFS und weitere Platten sind nicht abgedeckt
  (sie werden ausgelassen und müssen von Hand wiederhergestellt werden).
