"""Admin PIN (roadmap 1.1): the hash on disk, sessions, lockout, and the gate on every POST."""

import http.client
import json
import stat
import threading
import time

import pytest

from vector_bridge import admin, apsetup
from vector_bridge.cli import main
from vector_bridge.tuner import TunerApp, serve

pytestmark = pytest.mark.admin_pin


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(tmp_path, clock):
    return admin.Admin(tmp_path / "admin-pin.json", clock=clock)


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("pin,ok", [("1234", True), ("12345678", True), ("123", False), ("123456789", False),
                                    ("12a4", False), ("١٢٣٤", False), (1234, False), (None, False), ("", False)])
def test_a_pin_is_4_to_8_ascii_digits(pin, ok):
    assert admin.valid_pin(pin) is ok


def test_the_first_pin_needs_no_session_and_is_stored_only_as_a_hash(gate):
    assert not gate.pin_set()
    code, out = gate.set_pin({"pin": "4821"}, authorized=False)
    assert code == 200 and out["ok"] and out["token"]
    assert gate.pin_set()
    text = gate.path.read_text()
    assert "4821" not in text and json.loads(text)["iterations"] == admin.ITERATIONS
    assert stat.S_IMODE(gate.path.stat().st_mode) == 0o600
    assert gate.authorized(bearer(out["token"]))


def test_a_bad_first_pin_is_refused_and_nothing_is_saved(gate):
    assert gate.set_pin({"pin": "12"}, authorized=False)[0] == 400
    assert not gate.pin_set()


def test_login_takes_the_right_pin_and_refuses_a_wrong_one(gate):
    gate.set_pin({"pin": "4821"}, authorized=False)
    assert gate.login({"pin": "0000"}) == (401, {"ok": False, "error": "bad_pin"})
    code, out = gate.login({"pin": "4821"})
    assert code == 200 and gate.authorized(bearer(out["token"]))
    assert gate.authorized({"Cookie": f"other=1; {admin.COOKIE}={out['token']}"})  # the browser's path
    assert not gate.authorized(bearer("f" * 32))
    assert not gate.authorized({})


def test_login_with_no_pin_set_says_so(gate):
    assert gate.login({"pin": "1234"}) == (409, {"ok": False, "error": "pin_unset"})


def test_wrong_pins_lock_login_with_a_doubling_wait(gate, clock):
    gate.set_pin({"pin": "4821"}, authorized=False)
    for _ in range(admin.FREE_TRIES):
        assert gate.login({"pin": "0000"})[0] == 401
    code, out = gate.login({"pin": "4821"})  # even the right PIN waits out the lock
    assert (code, out["error"], out["retry_s"]) == (429, "locked", admin.LOCK_BASE_S + 1)
    clock.t += admin.LOCK_BASE_S + 1
    assert gate.login({"pin": "0000"})[0] == 401  # the sixth wrong PIN: twice as long
    assert gate.login({"pin": "4821"})[1]["retry_s"] == 2 * admin.LOCK_BASE_S + 1
    clock.t += 2 * admin.LOCK_BASE_S + 1
    assert gate.login({"pin": "4821"})[0] == 200
    assert gate.login({"pin": "0000"})[0] == 401  # a success starts the count again
    assert gate.login({"pin": "4821"})[0] == 200


def test_wrong_pins_sent_at_once_get_no_more_than_the_free_tries(gate, monkeypatch):
    gate.set_pin({"pin": "4821"}, authorized=False)
    slow = admin._hash
    monkeypatch.setattr(admin, "_hash", lambda *a: (time.sleep(0.2), slow(*a))[1])  # every try overlaps
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(gate.login({"pin": "0000"})[0])) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes.count(401) == admin.FREE_TRIES and codes.count(429) == 20 - admin.FREE_TRIES


def test_the_lockout_stops_at_an_hour(gate, clock):
    gate.set_pin({"pin": "4821"}, authorized=False)
    for _ in range(20):
        gate.login({"pin": "0000"})
        clock.t += admin.LOCK_MAX_S + 1
    gate.login({"pin": "0000"})
    assert gate.login({"pin": "4821"})[1]["retry_s"] == admin.LOCK_MAX_S + 1


def test_a_session_ends_after_12_hours_unused_but_use_keeps_it(gate, clock):
    token = gate.set_pin({"pin": "4821"}, authorized=False)[1]["token"]
    clock.t += admin.IDLE_S - 1
    assert gate.authorized(bearer(token))  # used: the 12 h start again
    clock.t += admin.IDLE_S - 1
    assert gate.authorized(bearer(token))
    clock.t += admin.IDLE_S + 1
    assert not gate.authorized(bearer(token))


def test_changing_the_pin_needs_a_session_and_ends_every_other_one(gate):
    first = gate.set_pin({"pin": "4821"}, authorized=False)[1]["token"]
    app = gate.login({"pin": "4821"})[1]["token"]
    assert gate.set_pin({"pin": "9999"}, authorized=False) == (401, {"ok": False, "error": "pin_required", "pin_set": True})
    code, out = gate.set_pin({"pin": "9999"}, authorized=gate.authorized(bearer(first)))
    assert code == 200 and gate.authorized(bearer(out["token"]))
    assert not gate.authorized(bearer(first)) and not gate.authorized(bearer(app))
    assert gate.login({"pin": "4821"})[0] == 401 and gate.login({"pin": "9999"})[0] == 200


def test_a_console_reset_ends_every_session_in_the_running_server(gate):
    token = gate.set_pin({"pin": "4821"}, authorized=False)[1]["token"]
    assert admin.reset(gate.path) is True
    assert not gate.pin_set() and not gate.authorized(bearer(token))
    assert admin.reset(gate.path) is False


def test_another_pin_file_written_behind_the_servers_back_ends_its_sessions(gate, tmp_path):
    token = gate.set_pin({"pin": "4821"}, authorized=False)[1]["token"]
    other = admin.Admin(tmp_path / "other.json")
    other.set_pin({"pin": "5555"}, authorized=False)
    gate.path.write_text(other.path.read_text())
    assert not gate.authorized(bearer(token))


def test_sessions_are_capped_dropping_the_least_recently_used(gate, clock):
    tokens = [gate.set_pin({"pin": "4821"}, authorized=False)[1]["token"]]
    for _ in range(admin.MAX_SESSIONS):
        clock.t += 1
        tokens.append(gate.login({"pin": "4821"})[1]["token"])
    assert not gate.authorized(bearer(tokens[0]))
    assert all(gate.authorized(bearer(t)) for t in tokens[1:])


# ---- app tokens (roadmap 1.8) ---------------------------------------------------------


def test_an_app_token_outlives_a_restart_and_idle_time_but_is_kept_only_as_a_hash(gate, clock, tmp_path):
    gate.set_pin({"pin": "4821"}, authorized=False)
    code, out = gate.login({"pin": "4821", "app": "VectorComm"})
    assert code == 200 and out["app"] == "VectorComm"
    token = out["token"]
    assert token not in gate.tokens_path.read_text()
    assert stat.S_IMODE(gate.tokens_path.stat().st_mode) == 0o600
    clock.t += 10 * admin.IDLE_S
    restarted = admin.Admin(gate.path, clock=clock)
    assert restarted.authorized(bearer(token))


def test_a_pin_change_ends_app_tokens_even_if_the_file_is_left_behind(gate, monkeypatch):
    first = gate.set_pin({"pin": "4821"}, authorized=False)[1]["token"]
    app = gate.login({"pin": "4821", "app": "VectorCable"})[1]["token"]
    kept = gate.tokens_path.read_text()
    gate.set_pin({"pin": "9999"}, authorized=gate.authorized(bearer(first)))
    assert not gate.authorized(bearer(app))
    gate.tokens_path.write_text(kept)  # a tidy-up that failed: the old salt still ends it
    assert not gate.authorized(bearer(app))


def test_a_console_reset_ends_app_tokens_and_removes_their_file(gate):
    gate.set_pin({"pin": "4821"}, authorized=False)
    app = gate.login({"pin": "4821", "app": "SWL-RADIO"})[1]["token"]
    assert admin.reset(gate.path) is True
    assert not gate.tokens_path.exists() and not gate.authorized(bearer(app))
    gate.set_pin({"pin": "4821"}, authorized=False)  # even the same PIN again
    assert not gate.authorized(bearer(app))


def test_an_app_token_can_be_listed_and_revoked_without_touching_the_others(gate):
    gate.set_pin({"pin": "4821"}, authorized=False)
    comm = gate.login({"pin": "4821", "app": "VectorComm"})[1]["token"]
    cable = gate.login({"pin": "4821", "app": "VectorCable"})[1]["token"]
    rows = gate.app_tokens()["tokens"]
    assert [r["app"] for r in rows] == ["VectorComm", "VectorCable"] and set(rows[0]) == {"id", "app", "issued"}
    assert gate.revoke({"revoke": rows[0]["id"]}) == (200, {"ok": True, "revoked": rows[0]["id"]})
    assert not gate.authorized(bearer(comm)) and gate.authorized(bearer(cable))
    assert gate.revoke({"revoke": rows[0]["id"]})[1]["error"] == "no_such_token"


def test_app_tokens_are_capped_dropping_the_oldest(gate):
    gate.set_pin({"pin": "4821"}, authorized=False)
    tokens = [gate.login({"pin": "4821", "app": f"app{i}"})[1]["token"] for i in range(admin.MAX_APP_TOKENS + 1)]
    assert not gate.authorized(bearer(tokens[0]))
    assert all(gate.authorized(bearer(t)) for t in tokens[1:])


def test_a_clock_stepping_back_does_not_evict_the_newest_app_token(gate, monkeypatch):
    gate.set_pin({"pin": "4821"}, authorized=False)
    for i in range(admin.MAX_APP_TOKENS):
        gate.login({"pin": "4821", "app": f"app{i}"})
    monkeypatch.setattr(admin.time, "time", lambda: 0)  # no RTC: fake-hwclock after a power pull
    newest = gate.login({"pin": "4821", "app": "late"})[1]["token"]
    assert gate.authorized(bearer(newest))


@pytest.mark.parametrize("app", ["","   ", 7, "x" * (admin.APP_NAME_MAX + 1), "bad\nname"])
def test_an_unusable_app_name_is_refused_and_issues_nothing(gate, app):
    gate.set_pin({"pin": "4821"}, authorized=False)
    assert gate.login({"pin": "4821", "app": app})[1]["error"] == "bad_app"
    assert not gate.tokens_path.exists()


def test_cli_reset_and_status(capsys):
    gate = admin.Admin()  # admin.PIN_PATH, in tmp for tests
    gate.set_pin({"pin": "4821"}, authorized=False)
    assert main(["pin", "status"]) == 0 and "set" in capsys.readouterr().out
    assert main(["pin", "reset"]) == 0
    assert "next save" in capsys.readouterr().out
    assert not gate.pin_set()
    assert main(["pin", "reset"]) == 0 and "no admin PIN" in capsys.readouterr().out


# ---- the gate on the web server -----------------------------------------------------


@pytest.fixture
def server(tmp_path):
    sent = []

    def control(req):
        sent.append(req)
        return {"ok": True}

    gate = admin.Admin(tmp_path / "admin-pin.json")
    httpd = serve(TunerApp(control, mixer_fn=lambda card: [], device_id_fn=lambda: "0123456789abcdef"),
                  bind="127.0.0.1", port=0,
                  docs_dir=tmp_path, admin=gate)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1], gate, sent
    finally:
        httpd.shutdown()
        httpd.server_close()


def request(port, method, path, body=None, headers=None):
    """(status, response headers, parsed body) for one request with exactly these headers."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=None if body is None else json.dumps(body).encode(),
                     headers={"Content-Type": "application/json", **(headers or {})})
        r = conn.getresponse()
        raw = r.read()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        return r.status, r.headers, parsed
    finally:
        conn.close()


# Every POST route, with a body its handler would act on.
POSTS = [
    ("/api/gain", {"rx": 40}),
    ("/api/tx_audio_cap", {"seconds": 120}),
    ("/api/rigctl", {"access": "all"}),
    ("/api/reset", {"scope": "all", "confirm": "RESET"}),
    ("/api/wizard", {"action": "advance"}),
    ("/api/ladder", {"action": "key", "owner": "app"}),
    ("/api/ladder", {"action": "heartbeat", "owner": "app"}),
    ("/api/ladder", {"action": "unkey", "owner": "app"}),
    ("/api/ladder", {"action": "tune", "freq_hz": 14074000, "mode": "PKTUSB"}),
    ("/api/ladder", {"action": "override", "gates": ["drive", "carrier"]}),
    ("/api/alerts", {"events": {"ptt": {"sound": False}}}),
    ("/api/wifi", {"suffix": "X", "passphrase": "12345678"}),
    ("/api/join", {"action": "ap"}),
    ("/api/ssh", {"action": "off"}),
    ("/api/wifi_country", {"country": "US"}),
    ("/api/apps", {"app_id": "x", "name": "X"}),
]


@pytest.mark.parametrize("pin_set", [False, True])
@pytest.mark.parametrize("path,body", POSTS)
def test_every_post_without_a_session_is_refused_before_its_handler(server, path, body, pin_set):
    port, gate, sent = server
    if pin_set:
        gate.set_pin({"pin": "4821"}, authorized=False)
    before = gate.path.read_bytes() if pin_set else None
    for headers in ({}, bearer("0" * 32), {"Cookie": f"{admin.COOKIE}=nope"}):
        status, _, out = request(port, "POST", path, body, headers)
        assert (status, out) == (401, {"ok": False, "error": "pin_required", "pin_set": pin_set})
    assert sent == []  # the audio service was never asked
    assert (gate.path.read_bytes() if pin_set else None) == before


def test_a_bearer_token_from_login_opens_the_posts(server):
    port, gate, sent = server
    gate.set_pin({"pin": "4821"}, authorized=False)
    status, _, out = request(port, "POST", "/api/login", {"pin": "4821"})
    assert status == 200
    status, _, _ = request(port, "POST", "/api/gain", {"rx": 40}, bearer(out["token"]))
    assert status == 200 and {"cmd": "set_gain", "rx": 40} in sent


def test_ssh_takes_a_pin_session_never_an_app_token(server, tmp_path, monkeypatch):
    from vector_bridge import sshswitch
    monkeypatch.setattr(sshswitch, "PENDING", tmp_path / "ssh-request.json")
    monkeypatch.setattr(sshswitch.persist, "save", lambda p, d, **k: p.write_text(d))
    port, gate, _ = server
    gate.set_pin({"pin": "4821"}, authorized=False)
    app = gate.login({"pin": "4821", "app": "VectorCable"})[1]["token"]
    status, _, out = request(port, "POST", "/api/ssh", {"action": "off"}, bearer(app))
    assert (status, out["error"]) == (403, "pin_session_required") and not sshswitch.PENDING.exists()
    session = gate.login({"pin": "4821"})[1]["token"]
    assert request(port, "POST", "/api/ssh", {"action": "off"}, bearer(session))[0] == 200
    assert sshswitch.PENDING.exists()


def test_a_logged_in_post_saves_the_wifi_country(server, tmp_path, monkeypatch):
    from vector_bridge import wificountry
    monkeypatch.setattr(wificountry, "SAVED", tmp_path / "wifi-country.json")
    monkeypatch.setattr(wificountry.persist, "save", lambda p, d, **k: p.write_text(d))
    port, gate, _ = server
    gate.set_pin({"pin": "4821"}, authorized=False)
    token = gate.login({"pin": "4821"})[1]["token"]
    status, _, out = request(port, "POST", "/api/wifi_country", {"country": "JP"}, bearer(token))
    assert (status, out) == (200, {"ok": True, "country": "JP"}) and wificountry.saved() == "JP"


def test_login_sets_a_strict_httponly_cookie_that_opens_the_posts(server):
    port, gate, sent = server
    gate.set_pin({"pin": "4821"}, authorized=False)
    status, headers, _ = request(port, "POST", "/api/login", {"pin": "4821"})
    cookie = headers["Set-Cookie"]
    assert status == 200 and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    status, _, _ = request(port, "POST", "/api/gain", {"rx": 40}, {"Cookie": cookie.split(";")[0]})
    assert status == 200 and {"cmd": "set_gain", "rx": 40} in sent


def test_a_wrong_pin_sets_no_cookie(server):
    port, gate, _ = server
    gate.set_pin({"pin": "4821"}, authorized=False)
    status, headers, out = request(port, "POST", "/api/login", {"pin": "1111"})
    assert (status, out["error"]) == (401, "bad_pin") and "Set-Cookie" not in headers


def test_a_fresh_card_creates_its_pin_over_http_then_needs_it(server):
    port, _, _ = server
    status, headers, out = request(port, "POST", "/api/pin", {"pin": "4821"})
    assert status == 200 and "Set-Cookie" in headers
    assert request(port, "POST", "/api/gain", {"rx": 40}, bearer(out["token"]))[0] == 200
    assert request(port, "POST", "/api/pin", {"pin": "1111"})[0] == 401  # no taking it over
    assert request(port, "POST", "/api/gain", {"rx": 40})[0] == 401


def test_get_pin_reports_set_and_unlocked(server):
    port, gate, _ = server
    assert request(port, "GET", "/api/pin")[2] == {"ok": True, "set": False, "unlocked": False}
    token = gate.set_pin({"pin": "4821"}, authorized=False)[1]["token"]
    assert request(port, "GET", "/api/pin")[2] == {"ok": True, "set": True, "unlocked": False}
    assert request(port, "GET", "/api/pin", headers=bearer(token))[2] == {"ok": True, "set": True, "unlocked": True}


def test_gets_stay_open(server):
    port, gate, _ = server
    gate.set_pin({"pin": "4821"}, authorized=False)
    for path in ("/api/wizard", "/api/alerts", "/api/battery", "/api/mode"):  # /api/wifi runs nmcli
        assert request(port, "GET", path)[0] == 200


def test_the_cross_site_check_still_comes_first(server):
    port, gate, _ = server
    token = gate.set_pin({"pin": "4821"}, authorized=False)[1]["token"]
    status, _, out = request(port, "POST", "/api/gain", {"rx": 40}, {"Origin": "http://evil.example", **bearer(token)})
    assert (status, out["error"]) == (403, "forbidden_origin")
    status, _, out = request(port, "POST", "/api/login", {"pin": "4821"}, {"Host": "evil.example"})
    assert (status, out["error"]) == (403, "forbidden_origin")


def test_every_page_carries_the_pin_prompt(server):
    port, _, _ = server
    for path in ("/", "/tuner", "/alerts"):
        status, _, page = request(port, "GET", path)
        assert status == 200 and page.count(b"window.vbAdmin = {") == 1


def test_the_first_boot_wifi_form_needs_no_pin_but_nothing_else_opens(server, tmp_path, monkeypatch):
    port, gate, _ = server
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "ap-unsecured")
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: False)
    monkeypatch.setattr(apsetup, "current_ssid", lambda: "VectorBridge-EF12")
    monkeypatch.setattr(apsetup, "PENDING", tmp_path / "ap-pending.json", raising=False)
    apsetup.UNSECURED.touch()
    status, _, out = request(port, "POST", "/api/wifi", {"suffix": "X", "passphrase": "12345678"})
    assert (status, out["ok"]) == (200, True)
    assert request(port, "POST", "/api/pin", {"pin": "4821"})[2]["error"] == "wifi_open"
    assert not gate.pin_set()


def test_status_carries_the_device_id_even_when_the_audio_service_is_down(tmp_path):
    app = TunerApp(lambda req: None, mixer_fn=lambda card: [], device_id_fn=lambda: "0123456789abcdef")
    assert app.status() == (503, {"ok": False, "error": "service_unreachable", "device_id": "0123456789abcdef"})
    app.control_fn = lambda req: {"ok": True}
    assert app.status()[1]["device_id"] == "0123456789abcdef"


def test_an_app_logs_in_over_http_gets_no_cookie_and_its_token_opens_the_posts(server):
    port, gate, _ = server
    gate.set_pin({"pin": "4821"}, authorized=False)
    status, headers, out = request(port, "POST", "/api/login", {"pin": "4821", "app": "VectorComm"})
    assert status == 200 and "Set-Cookie" not in headers
    assert out["app"] == "VectorComm" and out["device_id"] == "0123456789abcdef"
    assert request(port, "POST", "/api/gain", {"rx": 40}, bearer(out["token"]))[0] == 200
    browser = request(port, "POST", "/api/login", {"pin": "4821"})[2]
    assert browser["device_id"] == "0123456789abcdef" and "app" not in browser


def test_the_token_list_and_revoke_need_a_session(server):
    port, gate, _ = server
    owner = gate.set_pin({"pin": "4821"}, authorized=False)[1]["token"]
    app = request(port, "POST", "/api/login", {"pin": "4821", "app": "VectorCable"})[2]["token"]
    assert request(port, "GET", "/api/tokens")[0] == 401
    rows = request(port, "GET", "/api/tokens", headers=bearer(owner))[2]["tokens"]
    assert [r["app"] for r in rows] == ["VectorCable"]
    assert request(port, "POST", "/api/tokens", {"revoke": rows[0]["id"]})[0] == 401
    assert request(port, "POST", "/api/tokens", {"revoke": rows[0]["id"]}, bearer(owner))[0] == 200
    assert request(port, "POST", "/api/gain", {"rx": 40}, bearer(app))[0] == 401


def test_port_4532_takes_a_live_app_token_but_not_a_browser_session(gate):
    session = gate.set_pin({"pin": "4821"}, authorized=False)[1]["token"]
    app = gate.login({"pin": "4821", "app": "VectorCable"})[1]["token"]
    assert gate.app_token_valid(app)
    assert not gate.app_token_valid(session) and not gate.app_token_valid("") and not gate.app_token_valid(None)
    gate.revoke({"revoke": gate.app_tokens()["tokens"][0]["id"]})
    assert not gate.app_token_valid(app)
