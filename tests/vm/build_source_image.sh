#!/bin/sh
# Baut ein Debian-13-Testsystem als Rohdatei-Image: GPT, ESP, /boot ext4, LUKS -> LVM -> root ext4 + swap.
# Enthält pbsgui und proxmox-backup-client. Nur als root, nur auf Loop-Geräten. Für die VM-Tests.
# Aufruf: tests/vm/build_source_image.sh IMAGE.img PBSGUI.deb
set -eu
IMG=${1:?Image-Pfad}; DEB=${2:?pbsgui-.deb}; SIZE=${SIZE:-6G}; PASS=${PASS:-vmtest-passphrase}
PATH=$PATH:/usr/sbin:/sbin
VG=vmvg; MAP=vmcrypt; MP=/mnt/vmsrc
[ "$(id -u)" = 0 ] || { echo "root nötig" >&2; exit 1; }
LOOP=""
cleanup() {
  set +e
  umount -R -l "$MP" 2>/dev/null
  vgchange -an "$VG" >/dev/null 2>&1
  cryptsetup close "$MAP" 2>/dev/null
  [ -n "$LOOP" ] && losetup -d "$LOOP"
}
trap cleanup EXIT

rm -f "$IMG"; truncate -s "$SIZE" "$IMG"
LOOP=$(losetup -f --show -P "$IMG")
case "$LOOP" in /dev/loop*) ;; *) echo "Sicherheitsprüfung: kein Loop-Gerät: $LOOP" >&2; exit 1;; esac
sfdisk "$LOOP" <<LAYOUT
label: gpt
unit: sectors

start=2048, size=524288, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B
size=1048576, type=0FC63DAF-8483-4772-8E79-3D69D8477DE4
type=0FC63DAF-8483-4772-8E79-3D69D8477DE4
LAYOUT
partprobe "$LOOP"; udevadm settle
mkfs.vfat -F 32 -n ESP "${LOOP}p1" >/dev/null
mkfs.ext4 -F -q -L boot "${LOOP}p2"
printf '%s' "$PASS" | cryptsetup luksFormat --batch-mode --type luks2 --pbkdf pbkdf2 --pbkdf-force-iterations 1000 --key-file=- "${LOOP}p3"
printf '%s' "$PASS" | cryptsetup open --key-file=- "${LOOP}p3" "$MAP"
pvcreate -y "/dev/mapper/$MAP" >/dev/null
vgcreate "$VG" "/dev/mapper/$MAP" >/dev/null
lvcreate -y -L 3500M -n root "$VG" >/dev/null
lvcreate -y -L 256M -n swap "$VG" >/dev/null
udevadm settle
mkfs.ext4 -F -q -L root "/dev/$VG/root"
mkswap -L swap "/dev/$VG/swap" >/dev/null

mkdir -p "$MP"
mount "/dev/$VG/root" "$MP"
mkdir -p "$MP/boot"; mount "${LOOP}p2" "$MP/boot"
mkdir -p "$MP/boot/efi"; mount "${LOOP}p1" "$MP/boot/efi"

debootstrap --variant=minbase --cache-dir=/var/cache/pbsgui-debootstrap \
  --include=systemd-sysv,linux-image-amd64,grub-efi-amd64,cryptsetup-initramfs,lvm2,e2fsprogs,dosfstools,iproute2,ca-certificates,curl,python3,python3-textual,gdisk,efibootmgr,netbase,less,nano,kmod,udev,dbus,systemd-resolved,passwd,login,util-linux,mount,coreutils \
  trixie "$MP" http://deb.debian.org/debian >/dev/null

uuid() { blkid -s UUID -o value "$1"; }
cat > "$MP/etc/fstab" <<FSTAB
UUID=$(uuid /dev/$VG/root) / ext4 errors=remount-ro 0 1
UUID=$(uuid ${LOOP}p2) /boot ext4 defaults 0 2
UUID=$(uuid ${LOOP}p1) /boot/efi vfat umask=0077 0 1
UUID=$(uuid /dev/$VG/swap) none swap sw 0 0
FSTAB
echo "$MAP UUID=$(uuid ${LOOP}p3) none luks" > "$MP/etc/crypttab"
echo quell-pc > "$MP/etc/hostname"
printf '127.0.0.1 localhost\n127.0.1.1 quell-pc\n' > "$MP/etc/hosts"
mkdir -p "$MP/etc/systemd/network" "$MP/etc/systemd/system/serial-getty@ttyS0.service.d"
printf '[Match]\nName=en*\n\n[Network]\nDHCP=yes\n' > "$MP/etc/systemd/network/20-wired.network"
printf '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root --keep-baud 115200,57600,38400,9600 %%I $TERM\n' \
  > "$MP/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf"
cat >> "$MP/etc/default/grub" <<GRUBCFG
GRUB_CMDLINE_LINUX="console=tty0 console=ttyS0,115200n8"
GRUB_TERMINAL="console serial"
GRUB_SERIAL_COMMAND="serial --speed=115200 --unit=0 --word=8 --parity=no --stop=1"
GRUB_TIMEOUT=2
GRUBCFG

# Proxmox-Client-Repository und pbsgui im Image installieren
curl -fsSL https://enterprise.proxmox.com/debian/proxmox-archive-keyring-trixie.gpg -o "$MP/usr/share/keyrings/proxmox-archive-keyring.gpg"
echo "deb [signed-by=/usr/share/keyrings/proxmox-archive-keyring.gpg] http://download.proxmox.com/debian/pbs-client trixie main" > "$MP/etc/apt/sources.list.d/pbs-client.list"
cp "$DEB" "$MP/root/pbsgui.deb"
cp /etc/resolv.conf "$MP/etc/resolv.conf"
for d in dev dev/pts proc sys; do mount --bind "/$d" "$MP/$d"; done
mount -t tmpfs tmpfs "$MP/run"
chroot "$MP" sh -eu <<'CHROOT'
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq proxmox-backup-client /root/pbsgui.deb >/dev/null
rm /root/pbsgui.deb
passwd -d root
useradd -m -u 1000 -s /bin/bash vmuser
systemctl enable systemd-networkd systemd-resolved serial-getty@ttyS0 >/dev/null 2>&1
echo "ORIGINAL-$(date +%s)-$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')" > /root/marker.txt
dd if=/dev/urandom of=/home/vmuser/data.bin bs=1M count=20 status=none
echo "Hallo aus dem Quellsystem" > /home/vmuser/notiz.txt
ln -s notiz.txt /home/vmuser/link.txt
chown -R 1000:1000 /home/vmuser; chmod 640 /home/vmuser/data.bin
(cd / && sha256sum root/marker.txt home/vmuser/data.bin home/vmuser/notiz.txt) > /root/checksums.txt
update-initramfs -u -k all
grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable --no-nvram
update-grub
CHROOT
umount "$MP/run"
ln -sf /run/systemd/resolve/stub-resolv.conf "$MP/etc/resolv.conf"
sync
lsblk -J -b -o NAME,KNAME,PATH,SIZE,TYPE,FSTYPE,UUID,PARTUUID,LABEL,MOUNTPOINTS "$LOOP" > "${IMG%.img}.lsblk.json"
echo "Quell-Image fertig: $IMG"
