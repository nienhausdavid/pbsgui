import os
import sys
from pathlib import Path

import pytest

from pbsgui.core.models import Profile
from pbsgui.core.paths import Paths

FAKE = str(Path(__file__).parent / "fake_pbs_client.py")


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_PBS_DIR", str(tmp_path / "fake"))
    monkeypatch.setenv("PBSGUI_CLIENT", FAKE)
    monkeypatch.delenv("FAKE_PBS_EXPECT_PASSWORD", raising=False)
    monkeypatch.delenv("FAKE_PBS_FAIL", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    return tmp_path / "fake"


@pytest.fixture
def paths(tmp_path):
    return Paths("user", tmp_path / "cfg", tmp_path / "units", tmp_path / "cfg/creds", tmp_path / "state")


@pytest.fixture
def profile():
    return Profile(name="Laptop", server="pbs.example", datastore="store1",
                   auth_id="pbsgui-laptop@pbs!backup", fingerprint="AA:BB",
                   backup_id="laptop", sources=["/home/david", "/etc"], excludes=["*.tmp"], encrypt=False)


FAKE_MOUNTS = None


@pytest.fixture
def fake_mounts(monkeypatch):
    """Feste Mount-Tabelle: /boot (intern), /data (intern), USB unter /media, Bind-Duplikat."""
    from pbsgui.core import modes
    mounts = [modes.MountInfo("/boot", "ext4", "/dev/sda2"), modes.MountInfo("/data", "xfs", "/dev/sdb1"),
              modes.MountInfo("/media/david/STICK", "vfat", "/dev/sdc1", usb=True),
              modes.MountInfo("/srv/dup", "xfs", "/dev/sdb1", bind=True)]
    monkeypatch.setattr(modes, "detect_mounts", lambda *a, **k: mounts)
    monkeypatch.setattr("pbsgui.cli.main.detect_mounts", lambda *a, **k: mounts)
    return mounts
