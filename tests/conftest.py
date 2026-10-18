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
