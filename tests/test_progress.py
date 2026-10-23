"""Backup-Fortschritt: Lesezähler des Clients -> Prozent, Rate, Restzeit."""
import time
from types import SimpleNamespace

import pytest

from pbsgui.core import progress as pg
from pbsgui.core.client import PbsClient
from pbsgui.core.models import Profile, Snapshot
from pbsgui.core.progress import Progress, Sampler, decode, describe, encode, estimate_total, fmt_bytes, fmt_duration, read_rchar


def test_roundtrip_and_ignore_other_lines():
    p = Progress(5_000_000_000, 10_000_000_000, 40e6, 125.0, "letztes Backup")
    assert decode(encode(p)) == p
    assert decode("Upload directory '/' …") is None and decode(pg.PREFIX + "{kaputt") is None
    assert decode(pg.PREFIX + '{"unbekannt": 1}') is None


def test_percent_eta_and_cap():
    p = Progress(2_500_000_000, 10_000_000_000, 50e6, 60)
    assert p.percent == 25.0 and p.eta == pytest.approx(150.0)
    assert Progress(10_500_000_000, 10_000_000_000, 50e6, 60).percent == 99.0           # nie 100 % vor dem Ende
    assert Progress(1, None, 1e6, 1).percent is None and Progress(1, None, 1e6, 1).eta is None
    assert Progress(1, 100, 0.0, 1).eta is None                                          # Rate 0: keine Restzeit


def test_formatting_in_german():
    assert fmt_bytes(999) == "999 B" and fmt_bytes(1_500_000) == "1,5 MB" and fmt_bytes(24_500_000_000) == "24,5 GB"
    assert fmt_duration(42) == "42 s" and fmt_duration(360) == "6 min" and fmt_duration(3900) == "1 h 5 min"
    text = describe(Progress(9_100_000_000, 24_500_000_000, 39.4e6, 300, "letztes Backup"))
    assert text == "37 %  ·  9,1 GB von 24,5 GB  ·  39,4 MB/s  ·  noch ca. 6 min  (letztes Backup)"
    assert "Gesamtmenge unbekannt (erstes Backup)" in describe(Progress(1_000_000_000, None, 20e6, 50))


def test_read_rchar_from_proc_format(tmp_path):
    (tmp_path / "123").mkdir()
    (tmp_path / "123/io").write_text("rchar: 4242\nwchar: 1\nsyscr: 5\n")
    assert read_rchar(123, str(tmp_path)) == 4242
    assert read_rchar(999, str(tmp_path)) is None                  # Prozess weg / keine Rechte


def test_sampler_reports_progress_and_smoothed_rate():
    values = iter([100, 1100, 2100, 3100, 4100])                   # erster Wert = Startwert, gelesen vor dem ersten Intervall
    got = []
    s = Sampler(1, 10_000, "letztes Backup", got.append, interval=0.02, reader=lambda _pid: next(values, 4100))
    s.start()
    deadline = time.time() + 3
    while len(got) < 4 and time.time() < deadline:
        time.sleep(0.02)
    s.stop()
    assert [g.done for g in got[:4]] == [1000, 2000, 3000, 4000]    # ab dem Startwert gerechnet, nicht ab Prozessbeginn
    assert got[3].percent == 40.0 and got[3].rate > 0 and got[3].basis == "letztes Backup"


def test_sampler_without_total_has_no_percent():
    got = []
    s = Sampler(1, None, "", got.append, interval=0.02, reader=lambda _pid: 5)
    s.start()
    time.sleep(0.2)
    s.stop()
    assert got and got[0].percent is None and got[0].basis == ""


def test_estimate_total_prefers_last_backup_then_used_space(tmp_path):
    prof = Profile(name="p", server="s", datastore="d", auth_id="a", backup_id="pc")
    snaps = [Snapshot("host", "pc", 100, 5_000), Snapshot("host", "pc", 200, 7_000), Snapshot("host", "anderer", 300, 9_999),
             Snapshot("host", "pc", 50, 0)]
    assert estimate_total(prof, snaps) == (7_000, "letztes Backup")                    # neuestes eigenes Backup
    assert estimate_total(prof, []) == (None, "")                                      # Home, erstes Backup: unbekannt
    prof.backup_mode = "full"
    total, basis = estimate_total(prof, [])
    assert total and total > 0 and basis == "Schätzung aus belegtem Platz"


def test_client_backup_reports_progress(fake_env, profile, monkeypatch):
    """Der Client-Wrapper startet den Sampler mit der Größe des letzten Backups und meldet über on_progress."""
    PbsClient(profile, "x").backup()                                   # erstes Backup legt einen Snapshot (Größe 1024) an
    seen = []
    monkeypatch.setattr(pg, "read_rchar", lambda pid, io_root="/proc": 5)
    monkeypatch.setattr(pg.Sampler, "__init__", (lambda orig: lambda self, pid, total, basis, cb, **kw:
                                                  orig(self, pid, total, basis, cb, interval=0.01, reader=lambda _p: 5))(pg.Sampler.__init__))
    lines = []
    code = PbsClient(profile, "x").backup(lines.append, seen.append)
    assert code == 0 and any("End Time" in l or "done" in l for l in lines)
    assert not any(l.startswith(pg.PREFIX) for l in lines)           # Fortschritt kommt nur über on_progress


def test_runner_emits_progress_lines_only_when_asked(fake_env, profile, paths, monkeypatch):
    from pbsgui.core import runner as r
    from pbsgui.core.secrets import FileStore
    store = FileStore(paths.creds)
    store.put(profile.slug, "token", "t")
    sent = []

    def fake_backup(self, on_line=lambda _l: None, on_progress=None):
        sent.append(on_progress is not None)
        if on_progress:
            on_progress(Progress(1, 2, 1.0, 1.0))
        return 0

    monkeypatch.setattr(r.PbsClient, "backup", fake_backup)
    lines = []
    assert r.run_profile(profile, paths, store, lines.append, progress=False) == 0 and sent == [False]
    assert r.run_profile(profile, paths, store, lines.append, progress=True) == 0 and sent == [False, True]
    assert any(l.startswith(pg.PREFIX) for l in lines)
    monkeypatch.setenv("PBSGUI_PROGRESS", "1")                        # wie bei pkexec/pbsgui-run aus der Oberfläche
    r.run_profile(profile, paths, store, lines.append)
    assert sent[-1] is True
    monkeypatch.delenv("PBSGUI_PROGRESS")
    r.run_profile(profile, paths, store, lines.append)                # Timer/Journal: keine Fortschrittszeilen
    assert sent[-1] is False


def test_sampler_counts_data_read_during_the_first_interval():
    """Regression (echter Server): Daten der ersten Sekunde dürfen nicht im Startwert verschwinden."""
    calls = iter([50, 250, 450])                                   # Start = 50; bis zum ersten Tick schon 200 gelesen
    got = []
    s = Sampler(1, 1000, "x", got.append, interval=0.02, reader=lambda _p: next(calls, 450))
    s.start()
    time.sleep(0.15)
    s.stop()
    assert got[0].done == 200


# -- Anzeigen --------------------------------------------------------------------------------------
def prog_lines(*pcts, total=1000):
    return [encode(Progress(int(total * p / 100), total, 5e6, i + 1.0, "letztes Backup")) for i, p in enumerate(pcts)]


def test_cli_shows_progress_every_ten_percent_in_logs_and_hides_raw_lines(fake_env, profile, paths, monkeypatch, capsys):
    from pbsgui.cli.main import main
    from pbsgui.core import manager as M
    from pbsgui.core.secrets import FileStore
    monkeypatch.setenv("XDG_CONFIG_HOME", str(paths.config.parent))
    monkeypatch.setenv("XDG_STATE_HOME", str(paths.state.parent))
    monkeypatch.setattr(M, "best_store", lambda scope, d: FileStore(d))
    mgr = M.ProfileManager()
    mgr.save(profile, token="t")

    def fake_run(self, p, on_line=print):
        for line in ["Starte", *prog_lines(1, 4, 9, 11, 14, 25, 26, 55, 99), "Backup erfolgreich"]:
            on_line(line)
        return 0

    monkeypatch.setattr(M.ProfileManager, "run_backup", fake_run)
    assert main(["backup", "Laptop"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "Starte" and out[-1] == "Backup erfolgreich"
    shown = [l for l in out if "%" in l]
    assert [l.split("%")[0].strip() for l in shown] == ["1", "11", "25", "55", "99"]  # nur alle 10 Prozentpunkte (keine TTY)
    assert not any(pg.PREFIX in l for l in out)                                       # rohe Zeilen nie sichtbar


def test_gui_progress_bar_and_log_separation(fake_env, profile, paths):
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from pbsgui.core.manager import ProfileManager
    from pbsgui.core.secrets import FileStore
    from pbsgui.gui.window import MainWindow
    app = QApplication.instance() or QApplication([])
    mgr = ProfileManager(paths, FileStore(paths.creds))
    mgr.save(profile, token="t")
    w = MainWindow(mgr)
    w.backup_line("Upload directory '/' …")
    w.backup_line(prog_lines(42)[0])
    assert w.bar.value() == 42 and w.bar.maximum() == 100 and "42 %" in w.bar_text.text()
    assert w.log.toPlainText() == "Upload directory '/' …"                            # Fortschritt steht nicht im Log
    w.backup_line(encode(Progress(5_000_000, None, 1e6, 3.0)))                        # unbekannte Gesamtmenge
    assert w.bar.maximum() == 0 and "erstes Backup" in w.bar_text.text()              # laufender Balken


async def test_tui_progress_bar_updates(fake_env, profile, paths, monkeypatch, tmp_path):
    from textual.widgets import ProgressBar, Static
    from pbsgui.core import manager as M
    from pbsgui.core.manager import ProfileManager
    from pbsgui.core.secrets import FileStore
    from pbsgui.tui.app import PbsTui
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    mgr = ProfileManager(paths, FileStore(paths.creds))
    mgr.save(profile, token="t")
    async with PbsTui(mgr).run_test(size=(140, 40)) as pilot:
        await pilot.pause()
        app = pilot.app
        app.backup_line("Hallo aus dem Backup")
        app.backup_line(prog_lines(60)[0])
        await pilot.pause()
        bar = app.screen.query_one("#bar", ProgressBar)
        assert bar.progress == 60 and "60 %" in str(app.screen.query_one("#bar-text", Static).render())
