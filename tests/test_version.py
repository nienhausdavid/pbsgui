"""Versions-Konsistenz: eine Quelle (pyproject), CHANGELOG und Paket stimmen überein."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pyproject_version() -> str:
    return re.search(r'^version = "(.*)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)


def test_semver_format():
    assert re.fullmatch(r"\d+\.\d+\.\d+", pyproject_version())


def test_changelog_has_entry_for_current_version_and_unreleased_section():
    text = (ROOT / "CHANGELOG.md").read_text()
    assert "## [Unreleased]" in text
    assert re.search(rf"^## \[{re.escape(pyproject_version())}\] - \d{{4}}-\d{{2}}-\d{{2}}$", text, re.M)
    assert f"[{pyproject_version()}]: https://github.com/" in text
    versions = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", text, re.M)
    assert versions[0] == pyproject_version()                                    # neueste Version steht oben


def test_package_version_matches_pyproject(monkeypatch):
    import pbsgui
    assert pbsgui.__version__ == pyproject_version()


def test_cli_prints_version():
    out = subprocess.run([sys.executable, "-m", "pbsgui.cli.main", "--version"], capture_output=True, text=True,
                         cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0 and out.stdout.strip() == f"pbsgui {pyproject_version()}"


def test_release_script_rejects_bad_input_without_touching_anything():
    r = subprocess.run(["sh", str(ROOT / "scripts/release.sh"), "1.2"], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode != 0 and "Keine gültige Version" in r.stderr
    assert pyproject_version() in (ROOT / "pyproject.toml").read_text()


def test_english_changelog_mirrors_the_german_one():
    de = (ROOT / "CHANGELOG.md").read_text()
    en = (ROOT / "CHANGELOG.en.md").read_text()
    heads = lambda t: re.findall(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?$", t, re.M)
    assert heads(de) == heads(en)                                              # gleiche Versionen und Daten
    assert re.findall(r"^\[[^\]]+\]: \S+$", de, re.M) == re.findall(r"^\[[^\]]+\]: \S+$", en, re.M)
