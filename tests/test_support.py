import http.client
import json
import threading
import time

from vector_bridge import apsetup, readiness, support, version_text
from vector_bridge.tuner import TunerApp, serve


def test_bundle_lists_every_part_and_survives_a_broken_one():
    def broken():
        raise RuntimeError("rigctld gone")
    text = support.bundle([("Status", lambda: {"rx_peak": 9}), ("Hamlib", broken), ("Log", lambda: "line one\n")])
    assert text.startswith(f"VectorBridge support bundle, version {version_text()}")
    assert "===== Status =====\n" + json.dumps({"rx_peak": 9}, indent=1) in text
    assert "===== Hamlib =====\nunavailable: RuntimeError('rigctld gone')" in text
    assert "===== Log =====\nline one\n" in text


def test_journal_asks_for_the_bridge_units_only(monkeypatch):
    seen = []
    monkeypatch.setattr(support, "_run", lambda argv: seen.append(argv) or "ok")
    support.journal()
    units = [seen[0][i + 1] for i, a in enumerate(seen[0]) if a == "-u"]
    assert units == ["vector-bridge", "vector-bridge-tuner"] and "-b" in seen[0]


def test_fault_bundles_keep_only_the_newest(tmp_path):
    for i in range(7):
        support.save_fault(f"fault {i}", logs_dir=tmp_path, keep=5)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [f"fault-{n:04d}.txt" for n in range(3, 8)]
    assert (tmp_path / "fault-0007.txt").read_text() == "fault 6"


def test_fault_bundles_saved_at_once_get_their_own_numbers(tmp_path):
    threads = [threading.Thread(target=support.save_fault, args=(f"fault {i}",), kwargs={"logs_dir": tmp_path, "keep": 10})
               for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(p.name for p in tmp_path.iterdir()) == [f"fault-{n:04d}.txt" for n in range(1, 7)]


def test_tuner_bundle_carries_status_readiness_and_extra_parts():
    app = TunerApp(lambda req: {"rx_peak": 1200, "peer": None} if req["cmd"] == "status" else None,
                   mixer_fn=lambda card: [], support_parts=[("Config file", lambda: "tx_gain = 40\n")])
    text = app.support()
    for heading in ("Uptime", "Audio service status", "Readiness", "Transmit checks", "Setup", "Config file"):
        assert f"===== {heading} =====" in text
    assert "tx_gain = 40" in text


def test_monitor_hands_a_new_alert_to_the_fault_hook():
    states = iter([readiness.OK, readiness.FAIL])
    got = []

    def gather(history):
        return readiness.Inputs(statuses=[{"rx_rms": 900, "peer": None}], service_up=True,
                                cat_alive=next(states) == readiness.OK)
    mon = readiness.Monitor(gather)
    mon.on_alert = got.append
    mon.poll()
    mon.poll()
    for _ in range(50):
        if got:
            break
        time.sleep(0.02)
    assert [c.key for c in got[0]] == ["cat"]


def _get_support(tmp_path, monkeypatch, open_ap):
    monkeypatch.setattr(apsetup, "UNSECURED", tmp_path / "ap-unsecured")
    monkeypatch.setattr(apsetup, "ap_profile_is_open", lambda: open_ap)
    httpd = serve(TunerApp(lambda req: {"ok": True}, mixer_fn=lambda card: []), bind="127.0.0.1", port=0, docs_dir=tmp_path)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        conn.request("GET", "/api/support")
        r = conn.getresponse()
        return r.status, r.getheader("Location"), r.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_support_bundle_downloads_on_a_secured_access_point(tmp_path, monkeypatch):
    status, _, body = _get_support(tmp_path, monkeypatch, open_ap=False)
    assert status == 200 and body.startswith("VectorBridge support bundle")


def test_support_bundle_is_refused_on_an_open_access_point(tmp_path, monkeypatch):
    status, location, body = _get_support(tmp_path, monkeypatch, open_ap=True)
    assert (status, location) == (302, "/wifi") and "support bundle" not in body
