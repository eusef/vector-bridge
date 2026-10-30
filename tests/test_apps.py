"""App listing (DOCS/app-listing.md): validation, the store, and the HTTP routes."""

import base64
import hashlib
import http.client
import json
import struct
import threading
import zlib

import pytest

from vector_bridge import apps, apsetup
from vector_bridge.tuner import TunerApp, serve


def png(side: int = 64, h: int | None = None) -> bytes:
    """A real grey PNG, `side` wide and `h` (default `side`) tall."""
    h = side if h is None else h

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"".join(b"\x00" + b"\x80" * side for _ in range(h))
    return (apps.PNG_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", side, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


GOOD = {"app_id": "vector-swl-radio", "name": "Vector Radio"}


# ---- validation ---------------------------------------------------------------


@pytest.mark.parametrize("body,error", [
    ({"name": "x"}, "bad_app_id"),
    ({"app_id": "VectorComm", "name": "x"}, "bad_app_id"),  # upper case
    ({"app_id": "-x", "name": "x"}, "bad_app_id"),
    ({"app_id": "a" * 49, "name": "x"}, "bad_app_id"),
    ({"app_id": "x"}, "bad_name"),
    ({"app_id": "x", "name": "   "}, "bad_name"),
    ({"app_id": "x", "name": "a" * 33}, "bad_name"),
    ({"app_id": "x", "name": "a\nb"}, "bad_name"),
    ({"app_id": "x", "name": "a\u202eb"}, "bad_name"),  # bidi override
    ({**GOOD, "launch_url": "javascript:alert(1)"}, "bad_launch_url"),
    ({**GOOD, "launch_url": "JavaScript:alert(1)"}, "bad_launch_url"),
    ({**GOOD, "launch_url": " javascript:alert(1)"}, "bad_launch_url"),
    ({**GOOD, "launch_url": "java\tscript:alert(1)"}, "bad_launch_url"),
    ({**GOOD, "launch_url": "data:text/html,x"}, "bad_launch_url"),
    ({**GOOD, "launch_url": "/relative"}, "bad_launch_url"),
    ({**GOOD, "launch_url": "https://x/" + "a" * 503}, "bad_launch_url"),
    ({**GOOD, "launch_url": 5}, "bad_launch_url"),
    ({**GOOD, "note": "a" * 61}, "bad_note"),
    ({**GOOD, "note": "a\x00"}, "bad_note"),
    ({**GOOD, "icon_png": "not base64!"}, "bad_icon"),
    ({**GOOD, "icon_png": b64(b"GIF89a")}, "bad_icon"),
    ({**GOOD, "icon_png": b64(png(32))}, "bad_icon"),  # too small
    ({**GOOD, "icon_png": b64(png(257))}, "bad_icon"),  # too big
    ({**GOOD, "icon_png": b64(png(64, 80))}, "bad_icon"),  # not square
    ({**GOOD, "icon_png": b64(png(64)[:-5])}, "bad_icon"),  # cut short
    ({**GOOD, "icon_png": b64(png(64) + b"<script>")}, "bad_icon"),  # trailing bytes
    ({**GOOD, "icon_sha256": "abc"}, "bad_icon"),
])
def test_bad_descriptions_name_the_field(body, error):
    assert apps.validate(body) == (None, error)


@pytest.mark.parametrize("url", ["https://swl.tail1234.ts.net/", "http://127.0.0.1:8737/", "vectorcomm://", None])
def test_good_launch_links_are_kept_as_given(url):
    rec, err = apps.validate({**GOOD, "launch_url": url, "note": " Open it on the Mac running it "})
    assert err is None and rec["launch_url"] == url and rec["note"] == "Open it on the Mac running it"


def test_icon_too_large_is_refused():
    big = png(256)
    big = big[:-12] + struct.pack(">I", 30000) + b"tEXt" + b"\x00" * 30000 + b"\x00\x00\x00\x00" + big[-12:]
    assert apps.validate({**GOOD, "icon_png": b64(big)}) == (None, "bad_icon")


# ---- the store ----------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


LIVE = [{"addr": "10.0.0.5:5000", "role": "monitor", "client": "vector-swl-radio"},
        {"addr": "10.0.0.7:6000", "role": "peer", "client": "VectorComm"},
        {"addr": "10.0.0.8:7000", "role": "peer", "client": ""}]


def test_rows_follow_live_streams_and_only_matching_ip_is_described():
    store = apps.AppStore()
    icon = png(64)
    assert store.put("10.0.0.5", {**GOOD, "launch_url": "https://x/", "icon_png": b64(icon)}) == (200, {"ok": True})
    assert store.put("10.0.0.9", {"app_id": "vectorcable", "name": "VectorCable"})[0] == 200  # no stream: no row
    rows = store.rows(LIVE)
    sha = hashlib.sha256(icon).hexdigest()
    assert rows == [
        {"app_id": "vector-swl-radio", "name": "Vector Radio", "role": "monitor", "addr": "10.0.0.5:5000",
         "launch_url": "https://x/", "note": None, "icon_url": f"/api/apps/icon/{sha[:16]}.png"},
        {"app_id": "VectorComm", "name": "VectorComm", "role": "peer", "addr": "10.0.0.7:6000",
         "launch_url": None, "note": None, "icon_url": None},  # undescribed: its client string, as sent
        {"app_id": None, "name": "Unnamed app", "role": "peer", "addr": "10.0.0.8:7000",
         "launch_url": None, "note": None, "icon_url": None}]
    assert store.icon(sha[:16]) == icon
    assert store.rows([{**LIVE[0], "addr": "10.0.0.6:5000"}])[0]["name"] == "vector-swl-radio"  # another IP


def test_every_post_replaces_the_record_and_a_sha_reuses_the_icon():
    store = apps.AppStore()
    icon = png(64)
    sha = hashlib.sha256(icon).hexdigest()
    store.put("10.0.0.5", {**GOOD, "note": "n", "launch_url": "https://x/", "icon_png": b64(icon)})
    assert store.put("10.0.0.5", {**GOOD, "name": "Renamed", "icon_sha256": sha.upper()}) == (200, {"ok": True})
    row = store.rows(LIVE[:1])[0]
    assert (row["name"], row["note"], row["launch_url"]) == ("Renamed", None, None)
    assert row["icon_url"] == f"/api/apps/icon/{sha[:16]}.png"


def test_an_unknown_icon_sha_asks_for_the_png():
    store = apps.AppStore()
    assert store.put("10.0.0.5", {**GOOD, "icon_sha256": "a" * 64}) == (200, {"ok": True, "icon_needed": True})
    assert store.rows(LIVE[:1])[0]["icon_url"] is None


def test_a_description_older_than_180_s_is_not_shown_and_its_icon_goes():
    clock = Clock()
    store = apps.AppStore(clock=clock)
    icon = png(64)
    store.put("10.0.0.5", {**GOOD, "icon_png": b64(icon)})
    clock.t += apps.FRESH_S + 1
    assert store.rows(LIVE[:1])[0]["name"] == "vector-swl-radio"
    store.put("10.0.0.9", {"app_id": "other", "name": "Other"})  # any POST prunes
    assert store.icon(hashlib.sha256(icon).hexdigest()[:16]) is None


def test_icon_lookup_needs_16_hex_digits():
    assert apps.AppStore().icon("../../etc/passwd") is None


# ---- HTTP ---------------------------------------------------------------------


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "ap-unsecured")
    status = {"ok": True, "clients": [{"addr": "127.0.0.1:5000", "role": "monitor", "client": "vector-swl-radio"}]}
    a = TunerApp(lambda req: status if req == {"cmd": "status"} else None, mixer_fn=lambda card: [])
    httpd = serve(a, bind="127.0.0.1", port=0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield httpd.server_address[1], status, tmp_path
    finally:
        httpd.shutdown()
        httpd.server_close()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        conn.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
        r = conn.getresponse()
        return r.status, dict(r.getheaders()), r.read()
    finally:
        conn.close()


def test_http_post_lists_the_app_and_serves_its_icon(server):
    port, _, _ = server
    icon = png(64)
    code, _, body = call(port, "POST", "/api/apps", {**GOOD, "launch_url": "https://x/", "icon_png": b64(icon)})
    assert (code, json.loads(body)) == (200, {"ok": True, "listed": True})
    code, _, body = call(port, "GET", "/api/apps")
    row = json.loads(body)["apps"][0]
    assert code == 200 and row["name"] == "Vector Radio" and row["launch_url"] == "https://x/"
    code, headers, got = call(port, "GET", row["icon_url"])
    assert code == 200 and got == icon
    assert headers["Content-Type"] == "image/png" and headers["X-Content-Type-Options"] == "nosniff"
    assert "immutable" in headers["Cache-Control"]
    assert call(port, "GET", "/api/apps/icon/0000000000000000.png")[0] == 404


def test_http_post_before_the_stream_is_stored_but_not_listed(server):
    port, status, _ = server
    code, _, body = call(port, "POST", "/api/apps", {"app_id": "vectorcable", "name": "VectorCable"})
    assert (code, json.loads(body)) == (200, {"ok": True, "listed": False})
    status["clients"].append({"addr": "127.0.0.1:6000", "role": "monitor", "client": "vectorcable"})
    assert json.loads(call(port, "GET", "/api/apps")[2])["apps"][1]["name"] == "VectorCable"


@pytest.mark.parametrize("body,code,error", [
    (b"[1]", 400, "bad_json"),
    (b"{not json", 400, "bad_json"),
    ({"app_id": "x", "name": "", "launch_url": None}, 400, "bad_name"),
    (b"{" + b" " * (apps.MAX_BODY + 1) + b"}", 413, "too_large"),
])
def test_http_post_refusals(server, body, code, error):
    port, _, _ = server
    got, _, reply = call(port, "POST", "/api/apps", body)
    assert (got, json.loads(reply)["error"]) == (code, error)


def test_http_post_from_a_foreign_origin_is_refused(server):
    port, _, _ = server
    got, _, reply = call(port, "POST", "/api/apps", GOOD, {"Origin": "http://evil.example"})
    assert (got, json.loads(reply)["error"]) == (403, "forbidden_origin")


def test_http_post_while_the_access_point_is_open_says_wifi_open(server):
    port, _, tmp = server
    (tmp / "ap-unsecured").write_text("")
    got, _, reply = call(port, "POST", "/api/apps", GOOD)
    assert (got, json.loads(reply)["error"]) == (403, "wifi_open")
