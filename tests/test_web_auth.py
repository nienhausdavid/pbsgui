"""Web-Oberfläche: Passphrase, Rate Limiting, Sperren, Honeypot, Sitzungen (ohne Netzwerk, mit steuerbarer Uhr)."""
import re

import pytest

from pbsgui.web import auth


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock():
    return Clock()


def test_wordlist_is_complete_and_passphrase_has_expected_shape():
    words = auth.load_words()
    assert len(words) == 7776 and len(set(words)) == 7776
    p = auth.generate_passphrase()
    assert re.fullmatch(r"[a-z]+\d\d(-[a-z]+\d\d){3}", p)          # jedes Wort mit der Zahl direkt dahinter
    assert auth.generate_passphrase() != auth.generate_passphrase()
    assert auth.entropy_bits() > 75


def test_passphrase_check_tolerates_typing_but_nothing_else():
    assert auth.check_passphrase("  Abacus Grind-lemon 47  ", "abacus-grind-lemon-47")
    assert not auth.check_passphrase("abacus-grind-lemon-48", "abacus-grind-lemon-47")
    assert not auth.check_passphrase("", "abacus-grind-lemon-47")


def test_client_key_groups_ipv6_by_64_and_unmaps_ipv4():
    assert auth.client_key("203.0.113.7") == "203.0.113.7"
    assert auth.client_key("2001:db8:1:2:aaaa:bbbb:cccc:dddd") == auth.client_key("2001:db8:1:2::1")
    assert auth.client_key("2001:db8:1:3::1") != auth.client_key("2001:db8:1:2::1")
    assert auth.client_key("::ffff:203.0.113.7") == "203.0.113.7"


def test_sliding_window_limits_and_recovers(clock):
    w = auth.SlidingWindow(3, 10, clock)
    assert [w.allow("a") for _ in range(4)] == [True, True, True, False]
    assert w.allow("b")                        # andere Adresse unabhängig
    clock.advance(10.1)
    assert w.allow("a")


def make_guard(clock, **kw):
    kw.setdefault("fails_before_lock", 3)
    return auth.Guard("tisch-wolke-12", clock=clock, wall=clock, **kw)


def test_wrong_passwords_lock_the_address_with_growing_duration(clock):
    g = make_guard(clock, lock_seconds=100)
    for _ in range(3):
        assert g.check_login("1.2.3.4", "falsch") == (False, "wrong")
    assert g.check_login("1.2.3.4", "tisch-wolke-12") == (False, "locked")     # auch das richtige Passwort wird abgewiesen
    assert 99 < g.blocked_for("1.2.3.4") <= 100
    assert g.check_login("5.6.7.8", "tisch-wolke-12") == (True, "")            # andere Adresse nicht betroffen
    clock.advance(101)
    for _ in range(3):
        g.check_login("1.2.3.4", "falsch")
    assert 199 < g.blocked_for("1.2.3.4") <= 200                               # zweite Sperre doppelt so lang


def test_successful_login_resets_failures(clock):
    g = make_guard(clock)
    g.check_login("1.2.3.4", "x"); g.check_login("1.2.3.4", "x")
    assert g.check_login("1.2.3.4", "tisch-wolke-12")[0]
    g.check_login("1.2.3.4", "x"); g.check_login("1.2.3.4", "x")
    assert g.blocked_for("1.2.3.4") == 0


def test_too_many_failures_overall_shut_the_web_ui_down(clock):
    stopped = []
    g = make_guard(clock, fails_before_lock=1000, on_shutdown=stopped.append)
    for i in range(auth.Guard.GLOBAL_FAIL_LIMIT):
        g.check_login(f"10.0.{i // 250}.{i % 250}", "falsch")                  # verteilter Angriff von vielen Adressen
    assert g.shut_down and stopped
    assert g.check_login("1.1.1.1", "tisch-wolke-12") == (False, "down")       # selbst das richtige Passwort geht nicht mehr


def test_honeypot_bans_and_escalates(clock):
    g = make_guard(clock, honeypot_ban_seconds=3600)
    assert g.honeypot("9.9.9.9", "GET /wp-login.php") == 3600
    assert g.blocked_for("9.9.9.9") > 3500
    assert g.check_login("9.9.9.9", "tisch-wolke-12") == (False, "locked")
    clock.advance(3601)
    assert g.blocked_for("9.9.9.9") == 0
    assert g.honeypot("9.9.9.9", "GET /.env") == 7200                           # zweiter Treffer: doppelt so lang
    kinds = [e.kind for e in g.events]
    assert kinds.count("honeypot") == 2 and "ban" in kinds


@pytest.mark.parametrize("path", ["/wp-login.php", "/admin", "/admin/", "/.env", "/phpmyadmin/index.php", "/login", "/cgi-bin/x",
                                  "/WP-LOGIN.PHP?x=1", "/.git/config"])
def test_decoy_paths_are_honeypots(path):
    assert auth.is_honeypot_path(path)


@pytest.mark.parametrize("path", ["/", "/api/login", "/api/state", "/index.html", "/static/app.js"])
def test_real_paths_are_not_honeypots(path):
    assert not auth.is_honeypot_path(path)


def test_sessions_are_single_bound_and_expire(clock):
    s = auth.Sessions(idle=100, total=1000, clock=clock)
    a = s.create("1.2.3.4")
    assert s.get(a.token, "1.2.3.4") is a
    assert s.get(a.token, "6.6.6.6") is None                                    # andere Adresse
    b = s.create("1.2.3.4")                                                     # neue Anmeldung beendet die alte
    assert s.get(a.token, "1.2.3.4") is None and s.get(b.token, "1.2.3.4")
    clock.advance(101)
    assert s.get(b.token, "1.2.3.4") is None                                    # Leerlauf
    c = s.create("1.2.3.4")
    for _ in range(11):                                                         # aktiv bleiben: Leerlauf wird immer zurückgesetzt
        clock.advance(90)
        assert s.get(c.token, "1.2.3.4")
    clock.advance(20)                                                           # jetzt 1010 s seit der Anmeldung
    assert s.get(c.token, "1.2.3.4") is None                                    # Gesamtzeit
    assert s.get("", "1.2.3.4") is None
