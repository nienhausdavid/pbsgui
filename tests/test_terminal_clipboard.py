"""Kopieren über SSH: Terminal erkennen, OSC 52 senden oder den Text zum Markieren zeigen."""
import os
import pty

import pytest

from pbsgui.core import clipboard
from pbsgui.core.clipboard import over_ssh
from pbsgui.tui import terminal
from pbsgui.tui.terminal import NO, UNKNOWN, YES, clipboard_support, parse


def test_over_ssh_reads_the_ssh_variables():
    assert over_ssh({"SSH_CONNECTION": "a b c d"}) and over_ssh({"SSH_TTY": "/dev/pts/1"})
    assert not over_ssh({})


def test_parse_xtversion_and_da2():
    assert parse(b"\x1bP>|WezTerm 20240203\x1b\\\x1b[>1;277;0c\x1b[?65;1c") == ("WezTerm 20240203", "1;277;0")
    assert parse(b"\x1b[>65;7600;1c\x1b[?65;1;9c") == ("", "65;7600;1")
    assert parse(b"") == ("", "")


@pytest.mark.parametrize("term, xt, da2, want", [
    ("xterm-kitty", "", "", YES),
    ("xterm-256color", "kitty(0.39.1)", "1;4000;39", YES),
    ("xterm-256color", "WezTerm 20240203", "1;277;0", YES),
    ("xterm-ghostty", "", "", YES),
    ("xterm-256color", "iTerm2 3.5.10", "0;95;0", UNKNOWN),           # Einstellung „Applications may access clipboard“ nötig
    ("tmux-256color", "tmux 3.4", "84;0;0", UNKNOWN),
    ("xterm-256color", "VTE(8001)", "61;8001;1", NO),                   # GNOME Terminal (VTE 0.80, so gemessen)
    ("xterm-256color", "", "65;7600;1", NO),                            # ältere VTE ohne XTVERSION
    ("xterm-256color", "", "1;95;0", NO),                               # Terminal.app
    ("xterm-256color", "", "", UNKNOWN),
])
def test_clipboard_support(term, xt, da2, want):
    assert clipboard_support(term, xt, da2) == want


def test_query_reads_the_answers_from_a_terminal():
    master, slave = pty.openpty()
    os.write(master, b"\x1bP>|foot(1.20.2)\x1b\\\x1b[>1;12002;0c\x1b[?62;4c")    # die Antworten liegen schon bereit
    try:
        assert terminal.query(timeout=1, fd_in=slave, fd_out=slave) == ("foot(1.20.2)", "1;12002;0")
    finally:
        os.close(master), os.close(slave)


@pytest.mark.parametrize("support, osc52, shown", [(YES, True, False), (NO, False, True), (UNKNOWN, True, True)])
async def test_copy_over_ssh_depends_on_the_terminal(monkeypatch, support, osc52, shown):
    from pbsgui.tui.app import PbsTui
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    monkeypatch.setattr(clipboard, "copy_text", lambda text, **kw: pytest.fail("per SSH nie die Programme des entfernten Rechners"))
    monkeypatch.setattr(terminal, "clipboard_support", lambda: support)
    sent, showed, result = [], [], []
    monkeypatch.setattr(PbsTui, "copy_to_clipboard", lambda self, text: sent.append(text))
    monkeypatch.setattr(PbsTui, "show_for_copy", lambda self, text: showed.append(text))
    async with PbsTui().run_test(size=(150, 50)) as pilot:
        pilot.app.copy_text_out("geheim", result.append)
    assert bool(sent) == osc52 and bool(showed) == shown and result == [shown]


async def test_copy_without_display_uses_the_terminal(monkeypatch):
    from pbsgui.tui.app import PbsTui
    monkeypatch.delenv("WAYLAND_DISPLAY")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(clipboard, "copy_text", lambda text, **kw: pytest.fail("ohne Anzeige kein wl-copy"))
    monkeypatch.setattr(terminal, "clipboard_support", lambda: YES)
    sent = []
    monkeypatch.setattr(PbsTui, "copy_to_clipboard", lambda self, text: sent.append(text))
    async with PbsTui().run_test(size=(150, 50)) as pilot:
        pilot.app.copy_text_out("geheim")
    assert sent == ["geheim"]
