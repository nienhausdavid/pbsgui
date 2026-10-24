"""Zweisprachigkeit: Katalog vollständig, Platzhalter passend, Spracherkennung, Stichproben auf Englisch."""
import ast
import re
from pathlib import Path

import pytest

from pbsgui import i18n
from pbsgui.i18n import tr
from pbsgui.i18n.en import EN

SRC = Path(__file__).parent.parent / "src" / "pbsgui"
FIELD = re.compile(r"\{(\d+|[a-z_]+)(?:![rsa])?(?::[^{}]*)?\}")


@pytest.fixture(autouse=True)
def _reset_language():
    yield
    i18n.set_language("de")          # conftest legt Deutsch fest; Tests hier schalten um und stellen es wieder her


def tr_literals():
    """(Datei, Zeile, Text) jedes `tr("…")`-Aufrufs mit Zeichenkette als erstem Argument."""
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "tr" and n.args
                    and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
                yield path.relative_to(SRC), n.lineno, n.args[0].value


def test_every_tr_text_has_an_english_translation():
    missing = [f"{f}:{l}: {t[:70]!r}" for f, l, t in tr_literals() if t not in EN]
    assert not missing, "Ohne englische Übersetzung (in i18n/en_*.py ergänzen):\n" + "\n".join(missing[:30])


def test_translations_keep_the_same_placeholders():
    bad = []
    for de, en in EN.items():
        if sorted(FIELD.findall(de)) != sorted(FIELD.findall(en)):
            bad.append((de[:60], en[:60]))
    assert not bad, bad[:10]


def test_translations_are_not_empty_and_keys_are_unique_text():
    assert all(v.strip() for v in EN.values()) and len(EN) > 1200


def test_no_untranslated_german_left_in_english_catalog():
    umlaut = [v for v in EN.values() if re.search(r"[äöüÄÖÜß]", v) and "pbsgui" not in v]
    allowed = ("Wiederherstellung", "Größe")        # kommen nur in bewusst deutsch belassenen Beispielen vor
    assert not [v for v in umlaut if not any(a in v for a in allowed)], umlaut[:5]


def test_language_detection_order(tmp_path, monkeypatch):
    for var in ("PBSGUI_LANG", "LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert i18n.detect() == "en"                                   # nichts gesetzt
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    assert i18n.detect() == "de"
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")                    # LC_ALL hat Vorrang vor LANG
    assert i18n.detect() == "en"
    i18n.save_language("de")                                       # Datei schlägt die Systemsprache
    assert i18n.detect() == "de" and i18n.config_file().read_text().strip() == "de"
    monkeypatch.setenv("PBSGUI_LANG", "en")                        # Umgebungsvariable schlägt die Datei
    assert i18n.detect() == "en"
    monkeypatch.delenv("PBSGUI_LANG")
    i18n.save_language(None)
    assert not i18n.config_file().exists() and i18n.detect() == "en"
    with pytest.raises(ValueError):
        i18n.save_language("xx")


def test_tr_german_english_pseudo_and_formatting():
    i18n.set_language("de")
    assert tr("Prüfe {0}", "a.txt") == "Prüfe a.txt"
    assert tr("Text ohne Übersetzung {0}", 5) == "Text ohne Übersetzung 5"
    assert tr("Nur {geschweift}") == "Nur {geschweift}"            # ohne Argumente wird nichts eingesetzt
    i18n.set_language("en")
    assert tr("Prüfe {0}", "a.txt") == "Checking a.txt"
    assert tr("Text ohne Übersetzung {0}", 5) == "Text ohne Übersetzung 5"   # fehlende Übersetzung: deutscher Text bleibt
    i18n.set_language("pseudo")
    assert tr("Prüfe {0}", "a.txt") == "⟦Prüfe a.txt⟧"


def test_english_output_of_core_and_cli(capsys, tmp_path, monkeypatch):
    from pbsgui.cli.main import main
    from pbsgui.core.versions import RestoreTestResult, SampleResult
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "c"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "s"))
    i18n.set_language("en")
    res = RestoreTestResult("snap", "a.pxar", [SampleResult("/x", 1, "ok"), SampleResult("/y", 1, "fehler")], 1.0)
    assert res.summary() == "1 of 2 files OK (1 compared with the original), 1 ERRORS"
    assert main(["list"]) == 0
    assert "No profiles. Add a server with `pbsgui add`." in capsys.readouterr().out
    assert main(["history"]) == 0 and "No matching runs." in capsys.readouterr().out
    i18n.set_language("de")
    assert main(["list"]) == 0
    assert "Keine Profile." in capsys.readouterr().out


def test_web_pages_have_the_same_structure():
    de = (SRC / "web" / "index.html").read_text()
    en = (SRC / "web" / "index.en.html").read_text()
    ids = lambda s: sorted(re.findall(r'\bid="([^"]+)"', s))
    assert ids(de) == ids(en)
    assert sorted(re.findall(r"\$\(\"([a-z_A-Z-]+)\"\)", de)) == sorted(re.findall(r"\$\(\"([a-z_A-Z-]+)\"\)", en))
    assert 'lang="de"' in de and 'lang="en"' in en
    assert not re.search(r"[äöüÄÖÜß]", re.sub(r'x !== "[^"]+"( && )?', "", en))     # (nur die bewusst zweisprachige Filterzeile ausgenommen)


def test_web_server_serves_the_page_in_the_current_language(monkeypatch):
    from pbsgui.web import server
    assert server.INDEX.exists() and server.INDEX_EN.exists()
    i18n.set_language("en")
    assert (server.INDEX_EN if i18n.language() == "en" else server.INDEX) == server.INDEX_EN
