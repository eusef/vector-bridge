"""Captive portal: probes and foreign hosts redirect to the home screen; docs come off the card."""

import threading
import urllib.request

import pytest

from vector_bridge import portal
from vector_bridge.manage import ap_connection_argv, device_id, device_serial_suffix
from vector_bridge.tuner import TunerApp, serve


@pytest.fixture
def server(tmp_path):
    (tmp_path / "setup.md").write_text("# Field setup\n\nPlug the `data` jack.\n\n- one\n- two\n")
    httpd = serve(TunerApp(lambda req: {"ok": True}), bind="127.0.0.1", port=0, docs_dir=tmp_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def get(url, host=None):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), b""


@pytest.mark.parametrize("path", ["/hotspot-detect.html", "/generate_204", "/connecttest.txt"])
def test_connectivity_probe_redirects_to_home(server, path):
    status, headers, _ = get(server + path)
    assert status == 302 and headers["Location"] == "http://vectorbridge/"


def test_foreign_host_redirects_but_our_names_and_addresses_are_served(server):
    assert get(server + "/", host="www.example.com")[0] == 302
    assert get(server + "/", host="vectorbridge")[0] == 200
    assert get(server + "/", host="10.42.0.1")[0] == 200


def test_docs_are_served_from_the_card(server):
    status, _, index = get(server + "/docs/", host="vectorbridge")
    assert status == 200 and b'href="/docs/setup"' in index and b"Field setup" in index
    status, _, page = get(server + "/docs/setup", host="vectorbridge")
    assert status == 200 and b"<h1>Field setup</h1>" in page and b"<code>data</code>" in page and b"<li>two</li>" in page
    assert get(server + "/docs/../etc/passwd", host="vectorbridge")[0] == 404


def test_ssid_suffix_comes_from_the_pi_serial_and_falls_back_to_the_mac(tmp_path):
    (tmp_path / "serial").write_bytes(b"10000000abcdef12\x00")
    assert device_serial_suffix(tmp_path / "serial", wlan="nonexistent0") == "EF12"
    assert portal.HOSTNAME == "vectorbridge"


def test_device_id_is_a_stable_hash_of_the_serial_not_the_serial(tmp_path):
    (tmp_path / "serial").write_bytes(b"10000000abcdef12\x00")
    did = device_id(tmp_path / "serial", wlan="nonexistent0")
    assert did == device_id(tmp_path / "serial", wlan="nonexistent0") and len(did) == 16 and "abcdef12" not in did
    (tmp_path / "other").write_bytes(b"10000000abcdef13\x00")
    assert device_id(tmp_path / "other", wlan="nonexistent0") != did
    assert device_id(tmp_path / "missing", wlan="nonexistent0") is None


def test_ap_profile_is_shared_mode_at_a_pinned_address_and_boot_default():
    argv = ap_connection_argv("VectorBridge-EF12", "pw")
    kv = dict(zip(argv[2::2], argv[3::2]))  # after "connection add": alternating key, value
    assert kv["ipv4.method"] == "shared" and kv["ipv4.addresses"] == "10.42.0.1/24"
    assert kv["connection.autoconnect"] == "yes" and kv["connection.autoconnect-priority"] == "100"
    assert kv["wifi-sec.pmf"] == "disable"     # WPA2 only: no WPA3 the built-in radio could not match
    assert kv["802-11-wireless.channel"] == "6"     # never "auto": it chose 13, off the air under US rules


@pytest.mark.parametrize("host,ours", [
    ("vectorbridge", True), ("VectorBridge.local:80", True), ("vectorbridge.lan", True),
    ("10.42.0.1", True), ("127.0.0.1:8080", True), ("[::1]:8080", True), ("localhost", True),
    ("evil.example", False), ("vectorbridge.evil.example", False), ("", False), (None, False), ("[bad", False),
])
def test_is_ours_accepts_only_local_names_and_addresses_for_writes(host, ours):
    assert portal.is_ours(host) is ours
