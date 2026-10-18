"""Liste der erkannten Laufwerke mit Haken (Vollbackup)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from ..core.modes import mount_choices


class MountList(QListWidget):
    """Zeigt lokale Laufwerke; Haken = wird mitgesichert. Bind-Mount-Duplikate sind gesperrt."""

    def load(self, selected: list[str] | None) -> None:
        self.clear()
        for m, checked in mount_choices(selected):
            item = QListWidgetItem(f"{m.mountpoint}   ({m.fstype}, {m.kind})")
            item.setData(Qt.ItemDataRole.UserRole, m.mountpoint)
            flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
            if m.bind:
                flags &= ~Qt.ItemFlag.ItemIsEnabled
                item.setToolTip("Duplikat eines bereits eingehängten Verzeichnisses: wird nicht doppelt gesichert")
            elif m.usb:
                item.setToolTip("USB-Laufwerk: nur mit ausdrücklichem Haken")
            item.setFlags(flags)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.addItem(item)
        if self.count() == 0:
            self.addItem(QListWidgetItem("Keine weiteren lokalen Laufwerke erkannt (nur das Root-Dateisystem)."))
            self.item(0).setFlags(Qt.ItemFlag.NoItemFlags)

    def selected(self) -> list[str]:
        out = []
        for i in range(self.count()):
            it = self.item(i)
            mp = it.data(Qt.ItemDataRole.UserRole)
            if mp and it.checkState() == Qt.CheckState.Checked:
                out.append(mp)
        return out
