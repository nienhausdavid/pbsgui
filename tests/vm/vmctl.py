"""Kleine QEMU-Steuerung für die VM-Tests: Serial-Konsole (expect), Monitor (Tasten, Screenshots)."""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from pathlib import Path

OVMF = "/usr/share/ovmf/OVMF.fd"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\r")

# US-Tastaturbelegung (in der Rettungs-ISO vorher per `loadkeys us` einstellen)
SHIFT = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
         "_": "minus", "+": "equal", ":": "semicolon", '"': "apostrophe", "<": "comma", ">": "dot", "?": "slash",
         "{": "bracket_left", "}": "bracket_right", "|": "backslash", "~": "grave_accent"}
PLAIN = {" ": "spc", "\n": "ret", "\t": "tab", "-": "minus", "=": "equal", ".": "dot", ",": "comma", "/": "slash",
         ";": "semicolon", "'": "apostrophe", "[": "bracket_left", "]": "bracket_right", "\\": "backslash",
         "`": "grave_accent"}


def keyname(ch: str) -> str:
    if ch in PLAIN:
        return PLAIN[ch]
    if ch in SHIFT:
        return "shift-" + SHIFT[ch]
    if ch.isupper():
        return "shift-" + ch.lower()
    return ch  # a-z, 0-9


class VM:
    def __init__(self, name: str, workdir: Path, disks: list[Path], iso: Path | None = None, mem: int = 1280,
                 cpus: int = 2, kvm: bool = True):
        self.name, self.dir = name, Path(workdir)
        self.disks, self.iso, self.mem, self.cpus, self.kvm = disks, iso, mem, cpus, kvm
        self.serial_sock, self.mon_sock = self.dir / f"{name}.serial", self.dir / f"{name}.mon"
        self.proc: subprocess.Popen | None = None
        self._ser: socket.socket | None = None
        self.buf = ""
        self.pos = 0
        self.log = open(self.dir / f"{name}.log", "w")

    # -- Start/Stop ------------------------------------------------------------
    def start(self) -> None:
        for s in (self.serial_sock, self.mon_sock):
            s.unlink(missing_ok=True)
        cmd = ["qemu-system-x86_64", "-machine", "q35", "-smp", str(self.cpus), "-m", str(self.mem),
               "-bios", OVMF, "-display", "none", "-vga", "std",
               "-serial", f"unix:{self.serial_sock},server,nowait", "-monitor", f"unix:{self.mon_sock},server,nowait",
               "-nic", "user,model=virtio-net-pci", "-device", "qemu-xhci", "-device", "usb-kbd",
               "-rtc", "base=utc"]
        if self.kvm:
            cmd += ["-enable-kvm", "-cpu", "host"]
        for i, d in enumerate(self.disks):
            cmd += ["-drive", f"file={d},if=virtio,format=raw,index={i}"]
        if self.iso:
            cmd += ["-drive", f"file={self.iso},media=cdrom,readonly=on,if=ide,index=3", "-boot", "d"]
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            if self.serial_sock.exists() and self.mon_sock.exists():
                break
            time.sleep(0.1)
        self._ser = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._ser.connect(str(self.serial_sock))
        self._ser.setblocking(False)

    def stop(self, wait: float = 30) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.monitor("system_powerdown")
                self.proc.wait(timeout=wait)
            except Exception:
                self.proc.kill()
        if self.proc:
            self.proc.wait()
        self.log.close()

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    # -- Monitor ---------------------------------------------------------------
    def monitor(self, cmd: str) -> str:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(self.mon_sock))
        s.settimeout(5)
        try:
            s.recv(4096)
        except OSError:
            pass
        s.sendall(cmd.encode() + b"\n")
        time.sleep(0.15)
        try:
            return s.recv(65536).decode(errors="replace")
        except OSError:
            return ""
        finally:
            s.close()

    def keys(self, text: str, delay: float = 0.06) -> None:
        for ch in text:
            self.monitor(f"sendkey {keyname(ch)}")
            time.sleep(delay)

    def key(self, name: str) -> None:
        self.monitor(f"sendkey {name}")
        time.sleep(0.15)

    def screenshot(self, png: Path) -> None:
        ppm = png.with_suffix(".ppm")
        self.monitor(f"screendump {ppm}")
        time.sleep(0.4)
        with open(png, "wb") as f:
            subprocess.run(["pnmtopng", str(ppm)], stdout=f, check=True)
        ppm.unlink(missing_ok=True)

    # -- Serial ----------------------------------------------------------------
    def _pump(self, timeout: float) -> None:
        import select
        r, _, _ = select.select([self._ser], [], [], timeout)
        if r:
            try:
                data = self._ser.recv(65536)
            except BlockingIOError:
                return
            if data:
                text = ANSI.sub("", data.decode(errors="replace"))
                self.buf += text
                self.log.write(text)
                self.log.flush()

    def expect(self, pattern: str, timeout: float = 120, search_from_last: bool = True) -> re.Match:
        rx = re.compile(pattern, re.S)
        end = time.time() + timeout
        while True:
            m = rx.search(self.buf, self.pos if search_from_last else 0)
            if m:
                self.pos = m.end()
                return m
            if time.time() > end:
                raise TimeoutError(f"{self.name}: '{pattern}' nicht gesehen. Letzte Ausgabe:\n{self.buf[-1500:]}")
            if not self.alive():
                raise RuntimeError(f"{self.name}: QEMU beendet. Letzte Ausgabe:\n{self.buf[-1500:]}")
            self._pump(0.5)

    def send(self, text: str) -> None:
        self._ser.sendall(text.encode())

    def run(self, command: str, timeout: float = 120) -> tuple[str, int]:
        """Befehl in der Root-Shell auf der seriellen Konsole ausführen; liefert (Ausgabe, Exitcode)."""
        start = len(self.buf)
        self.pos = start
        self.send(f'{command}; echo "<<DONE:$?>>"\n')
        m = self.expect(r"<<DONE:(\d+)>>", timeout)
        out = self.buf[start:m.start()]
        return out, int(m.group(1))
