"""The QR encoder (checked module for module against segno 1.6.6 for versions 1 to 10 and all
eight masks on 2026-09-22; the fixture below is that output) and the Wi-Fi code it serves."""
import threading

import pytest
from test_portal import get

from vector_bridge import apsetup, qr
from vector_bridge.tuner import TunerApp, serve

V1_A_MASK0 = """\
#######..#.##.#######
#.....#..###..#.....#
#.###.#.##.##.#.###.#
#.###.#..#.#..#.###.#
#.###.#...#.#.#.###.#
#.....#.....#.#.....#
#######.#.#.#.#######
........##.##........
###.########.##...#..
..#.##.#..#...#...##.
....#.#####.#...#...#
##.#.#...##...#...#..
##..####....#.#.#.#.#
........##.#.#.#.#.##
#######.#..#.###.####
#.....#.######.###...
#.###.#.#.##.###.##.#
#.###.#...#...#...##.
#.###.#.##..#...#...#
#.....#.##....#...##.
#######.##..#.#.#.###"""


def core(matrix):
    q = qr.QUIET
    return "\n".join("".join("#" if x else "." for x in row[q:-q]) for row in matrix[q:-q])


def test_version_1_matches_the_reference_encoder():
    assert core(qr.encode("a", version=1, mask=0)) == V1_A_MASK0


def test_reed_solomon_codewords_have_zero_syndromes():
    words = qr._codewords(b"WIFI:T:WPA;S:VectorBridge-K7ABC;P:correct horse;;", 3)
    for i in range(15):
        acc = 0
        for w in words:
            acc = qr._mul(acc, qr._EXP[i]) ^ w
        assert acc == 0


@pytest.mark.parametrize(("length", "version"), [(17, 1), (18, 2), (106, 5), (107, 6), (134, 6), (135, 7), (271, 10)])
def test_smallest_version_that_fits(length, version):
    assert len(qr.encode("x" * length)) == version * 4 + 17 + 2 * qr.QUIET


def test_too_long_is_refused():
    with pytest.raises(ValueError):
        qr.encode("x" * 272)


def test_every_mask_is_a_valid_symbol_and_the_chosen_one_is_the_cheapest():
    text = "WIFI:T:WPA;S:VectorBridge-K7ABC;P:correct horse battery;;"
    scores = [qr._penalty([[int(x) for x in row] for row in qr.encode(text, mask=k)]) for k in range(8)]
    assert qr.encode(text) == qr.encode(text, mask=scores.index(min(scores)))


def test_wifi_string_escapes_the_special_characters():
    assert qr.wifi_string("Ab;c", 'p"a:s\\s,') == 'WIFI:T:WPA;S:Ab\\;c;P:p\\"a\\:s\\\\s\\,;;'
    assert qr.wifi_string("Open", None) == "WIFI:T:nopass;S:Open;;"


def test_svg_is_one_path_that_scales():
    s = qr.svg(qr.encode("a"))
    assert s.startswith("<svg") and 'viewBox="0 0 29 29"' in s and s.count("<path") == 1


def test_read_wifi_txt(tmp_path):
    p = tmp_path / "vector-bridge-wifi.txt"
    p.write_text("VectorBridge Wi-Fi\n\nNetwork:    VectorBridge-K7ABC\nPassphrase: correct horse\n\nChange them at http://vectorbridge/wifi\n")
    assert apsetup.read_wifi_txt(p) == ("VectorBridge-K7ABC", "correct horse")
    p.write_text("VectorBridge Wi-Fi\n\nNetwork:    VectorBridge-EF12\nPassphrase: none yet, the network is open\n")
    assert apsetup.read_wifi_txt(p) == ("VectorBridge-EF12", None)
    assert apsetup.read_wifi_txt(tmp_path / "missing") == ("", None)


@pytest.fixture
def server(tmp_path, monkeypatch):
    txt = tmp_path / "vector-bridge-wifi.txt"
    txt.write_text("Network:    VectorBridge-K7ABC\nPassphrase: correct horse\n")
    monkeypatch.setattr(apsetup, "WIFI_TXT", txt)
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "ap-unsecured")
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: False)
    httpd = serve(TunerApp(lambda req: {"ok": True}), bind="127.0.0.1", port=0, docs_dir=tmp_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_code_is_served_only_while_the_network_is_secured(server):
    code, headers, body = get(server + "/wifi-qr.svg")
    assert code == 200 and headers["Content-Type"] == "image/svg+xml" and body.startswith(b"<svg")
    apsetup.UNSECURED.touch()
    assert get(server + "/wifi-qr.svg")[0] == 404
