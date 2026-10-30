"""A USB Wi-Fi adapter takes over the bridge's network: picking the radio, the 5 GHz channel, and the
watcher that moves the network when an adapter is plugged in or pulled out."""
import pytest

from vector_bridge import alerts, manage, wifiradio
from vector_bridge.wifiradio import Active, Radio

# `iw phy phy1 info` from the Panda AC600 (MT7610U) on the bench Pi, regulatory domain US, trimmed.
PANDA = """Wiphy phy1
	Supported interface modes:
		 * managed
		 * AP
		 * monitor
	Band 1:
		Frequencies:
			* 2412.0 MHz [1] (8.0 dBm)
			* 2467.0 MHz [12] (disabled)
	Band 2:
		Frequencies:
			* 5180.0 MHz [36] (11.0 dBm)
			* 5260.0 MHz [52] (11.0 dBm) (radar detection)
			* 5745.0 MHz [149] (13.0 dBm)
			* 5825.0 MHz [165] (14.0 dBm)
			* 5845.0 MHz [169] (6.0 dBm) (no IR)
	valid interface combinations:
		 * #{ managed } <= 2048, #{ AP } <= 8,
"""
# No country set: every 5 GHz channel is listen-only.
WORLD = PANDA.replace("(11.0 dBm)\n", "(11.0 dBm) (no IR)\n").replace("(13.0 dBm)\n", "(13.0 dBm) (no IR)\n") \
    .replace("(14.0 dBm)\n", "(14.0 dBm) (no IR)\n")
CLIENT_ONLY = PANDA.replace("\t\t * AP\n", "")
BUILT_IN = "Wiphy phy0\n\tSupported interface modes:\n\t\t * managed\n\t\t * AP\n\tBand 1:\n\t\tFrequencies:\n\t\t\t* 2412.0 MHz [1] (20.0 dBm)\n"


@pytest.mark.parametrize("text,expected", [
    (PANDA, (True, (149, 165, 36))),
    (PANDA.replace("[149] (13.0 dBm)", "[149] (13.0 dBm) (disabled)"), (True, (165, 36))),
    (WORLD, (True, ())),              # 2.4 GHz until a country allows 5 GHz
    (CLIENT_ONLY, (False, (149, 165, 36))),   # "AP" in the combinations list does not count
    (BUILT_IN, (True, ())),
    ("* 5745 MHz [149] (30.0 dBm)\n* 5180 MHz [36] (17.0 dBm) (passive scanning)\n", (False, (149,))),  # older iw
])
def test_parse_phy(text, expected):
    assert wifiradio.parse_phy(text) == expected


def make_net(tmp_path, *devs):
    """A /sys/class/net with (name, bus, phy) wireless interfaces and a wired one."""
    net = tmp_path / "net"
    for name, bus, phy in devs:
        hw = tmp_path / "devices" / bus / name
        hw.mkdir(parents=True)
        d = net / name
        (d / "wireless").mkdir(parents=True)
        (d / "phy80211").mkdir()
        (d / "phy80211" / "name").write_text(phy + "\n")
        (d / "device").symlink_to(hw)
        (d / "operstate").write_text("down\n")
    (net / "eth0").mkdir(parents=True)
    return net


PHYS = {"phy0": BUILT_IN, "phy1": PANDA, "phy2": CLIENT_ONLY, "phy3": WORLD}


def found(tmp_path, *devs):
    return wifiradio.radios(make_net(tmp_path, *devs), lambda phy: wifiradio.parse_phy(PHYS[phy]))


def test_a_usb_adapter_is_preferred_and_runs_5_ghz(tmp_path):
    rs = found(tmp_path, ("wlan0", "mmc1/mmc1:0001", "phy0"), ("wlan1", "usb1/1-1/1-1.3", "phy1"))
    assert rs == [Radio("wlan0", False, True, ()), Radio("wlan1", True, True, (149, 165, 36))]
    assert wifiradio.best(rs) == rs[1]
    assert wifiradio.best(rs).band == "a"


def test_an_adapter_that_cannot_be_an_access_point_is_not_used(tmp_path):
    rs = found(tmp_path, ("wlan0", "mmc1/mmc1:0001", "phy0"), ("wlan1", "usb1/1-1/1-1.3", "phy2"))
    assert wifiradio.best(rs).name == "wlan0"


def test_a_2_4_ghz_only_adapter_still_beats_the_built_in_radio(tmp_path):
    rs = found(tmp_path, ("wlan0", "mmc1/mmc1:0001", "phy0"), ("wlan1", "usb1/1-1/1-1.2", "phy3"))
    assert wifiradio.best(rs) == Radio("wlan1", True, True, ())
    assert wifiradio.best(rs).band == "bg"


def test_is_usb(tmp_path):
    net = make_net(tmp_path, ("wlan0", "mmc1/mmc1:0001", "phy0"), ("wlan1", "usb1/1-1/1-1.3", "phy1"))
    assert (wifiradio.is_usb("wlan0", net), wifiradio.is_usb("wlan1", net), wifiradio.is_usb("", net)) == (False, True, False)


def test_no_sysfs_means_no_radios(tmp_path):
    assert wifiradio.radios(tmp_path / "missing") == []


def test_the_status_light_sees_an_access_point_on_the_adapter(tmp_path):
    net = make_net(tmp_path, ("wlan0", "mmc1/mmc1:0001", "phy0"), ("wlan1", "usb1/1-1/1-1.3", "phy1"))
    assert not alerts.wlan_up(net)
    (net / "eth0" / "operstate").write_text("up\n")    # wired does not count
    assert not alerts.wlan_up(net)
    (net / "wlan1" / "operstate").write_text("up\n")
    assert alerts.wlan_up(net)


class R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_the_access_point_takes_the_quietest_channel_and_falls_back_to_2_4_ghz(monkeypatch):
    calls = []

    def nmcli(*a, **k):
        calls.append(" ".join(a))
        if "list" in a:     # the scan: two networks on 149, one on 36, none on 165
            return R(out="149\n36\n149\n6\n")
        if "show" in a:
            return R(out="wpa-psk\n")
        return R(1, err="Error: Connection activation failed") if a[:2] == ("connection", "up") and len(calls) == 4 else R()
    monkeypatch.setattr(manage, "_nmcli", nmcli)
    assert wifiradio.bring_up(manage.AP_CON, Radio("wlan1", True, True, (149, 165, 36))).returncode == 0
    wpa2 = " 802-11-wireless-security.pmf disable"     # no WPA3 on the adapter: the built-in radio cannot match it
    assert calls == [
        "-g 802-11-wireless-security.key-mgmt connection show vector-bridge-ap",
        "-t -f CHAN device wifi list ifname wlan1 --rescan yes",
        "connection modify --temporary vector-bridge-ap connection.interface-name wlan1 802-11-wireless.band a 802-11-wireless.channel 165" + wpa2,
        "connection up vector-bridge-ap",
        "connection modify --temporary vector-bridge-ap connection.interface-name wlan1 802-11-wireless.band bg 802-11-wireless.channel 6" + wpa2,
        "connection up vector-bridge-ap"]


def test_an_open_access_point_gets_no_security_setting(monkeypatch):
    calls = []
    monkeypatch.setattr(manage, "_nmcli", lambda *a, **k: calls.append(" ".join(a)) or R())   # key-mgmt reads empty
    assert wifiradio.bring_up(manage.AP_CON, Radio("wlan0", False)).returncode == 0
    assert not [c for c in calls if "security" in c and "modify" in c]


def test_a_refused_profile_change_is_the_error_and_nothing_is_brought_up(monkeypatch):
    # nmcli 1.52 refuses channel "0" and changes nothing; "up" then fails on the old interface name.
    calls, refused = [], []

    def nmcli(*a, **k):
        calls.append(a[:2])
        if a[:2] == ("connection", "modify"):
            refused[:] = ["0"] if "0" in a else []
            if refused:
                return R(2, err="Error: failed to modify 802-11-wireless.channel: '0' is not a valid channel.")
        if a[:2] == ("connection", "up") and refused:
            return R(4, err="Error: Connection activation failed: No suitable device found for this connection "
                            "(device wlan0 not available because profile is not compatible with device (mismatching interface name)).")
        return R()
    monkeypatch.setattr(manage, "_nmcli", nmcli)
    assert wifiradio.bring_up(manage.AP_CON, Radio("wlan0", False)).returncode == 0     # 2.4 GHz only: channel 6
    assert calls == [("-g", "802-11-wireless-security.key-mgmt"), ("connection", "modify"), ("connection", "up")]
    calls.clear()
    monkeypatch.setattr(manage, "_nmcli", lambda *a, **k: calls.append(a[:2]) or R(10, err="Error: unknown connection 'Cafe'."))
    r = wifiradio.bring_up("Cafe", Radio("wlan1", True))
    assert (r.returncode, calls) == (10, [("connection", "modify")])


def test_active_reads_wifi_connections_only(monkeypatch):
    out = ("a17e:802-11-wireless:wlan0:activated:vector-bridge-ap\n"
           "c006:tun:tailscale0:activated:tailscale0\n"
           "736c:802-11-wireless:wlan1:activating:Cafe\\: upstairs\n")
    monkeypatch.setattr(manage, "_nmcli", lambda *a, **k: R(out=out))
    assert wifiradio.active() == [Active("a17e", "wlan0", "activated", "vector-bridge-ap"),
                                  Active("736c", "wlan1", "activating", "Cafe: upstairs")]


def test_a_just_plugged_adapter_is_not_ready_until_networkmanager_is(monkeypatch):
    out = "wlan0:connected\nwlan1:unavailable\np2p-dev-wlan0:disconnected\neth0:unmanaged\n"
    monkeypatch.setattr(manage, "_nmcli", lambda *a, **k: R(out=out))
    assert wifiradio.ready_devices() == {"wlan0", "p2p-dev-wlan0"}


def test_the_lock_is_not_taken_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(wifiradio, "LOCK", tmp_path / "lock")
    with wifiradio.locked() as mine:
        assert mine
        with wifiradio.locked(blocking=False) as other:
            assert other is False
    with wifiradio.locked(blocking=False) as again:
        assert again


# ---- the watcher ----------------------------------------------------------------

BUILT = Radio("wlan0", False, True, ())
PANDA_R = Radio("wlan1", True, True, (149,))
AP_ON_WLAN0 = Active("ap-uuid", "wlan0", "activated", manage.AP_CON)
AP_ON_WLAN1 = Active("ap-uuid", "wlan1", "activated", manage.AP_CON)
HOME_ON_WLAN0 = Active("home-uuid", "wlan0", "activated", "HomeNet")


class Bench:
    """The Pi as the watcher sees it: which radios are plugged in, what is on the air, what each move did."""

    def __init__(self):
        self.radios = [BUILT]
        self.acts = []
        self.moves = []
        self.refuse = set()     # (connection, radio) pairs that fail to come up
        self.band = "bg"
        self.t = 0.0
        self.downs, self.skips, self.on_air_at_up = [], [], []
        self.country, self.regs = ("", "US"), []     # (saved, kernel command line), and each `iw reg set`
        self.w = wifiradio.Watcher(radios_fn=lambda: self.radios, ready_fn=lambda: {r.name for r in self.radios},
                                   active_fn=lambda: self.acts, up_fn=self.up, band_fn=lambda con: self.band,
                                   clock=lambda: self.t, down_fn=self.down, skip_fn=lambda names: self.skips.append(set(names)),
                                   country_fn=lambda: self.country, reg_fn=self.regs.append)

    def down(self, dev):
        self.downs.append(dev)
        self.acts = [a for a in self.acts if a.device != dev]

    def up(self, con, radio, is_ap):
        self.moves.append((con, radio.name, radio.band if is_ap else None))
        self.on_air_at_up.append(sorted(a.device for a in self.acts))
        if (con, radio.name) in self.refuse:
            return R(1, err="Error: Connection activation failed")
        name = manage.AP_CON if is_ap else "HomeNet"
        self.acts = [Active(con, radio.name, "activated", name)]
        self.band = radio.band
        return R()


@pytest.fixture
def bench():
    return Bench()


def test_plugging_in_an_adapter_moves_the_access_point_to_5_ghz(bench):
    bench.acts = [AP_ON_WLAN0]
    bench.w.tick()
    assert bench.moves == []
    bench.radios = [BUILT, PANDA_R]
    bench.w.tick()
    assert bench.moves == [("ap-uuid", "wlan1", "a")]
    # Off the built-in radio before it comes up on the adapter: both cannot hold 10.42.0.1 at once.
    assert bench.downs == ["wlan0"] and bench.on_air_at_up == [[]]
    bench.w.tick()
    assert len(bench.moves) == 1    # settled


def test_unplugging_it_moves_the_access_point_back_to_the_built_in_radio(bench):
    bench.radios, bench.acts, bench.band = [BUILT, PANDA_R], [AP_ON_WLAN1], "a"
    bench.w.tick()
    bench.radios, bench.acts = [BUILT], []
    bench.w.tick()
    assert bench.moves == [("ap-uuid", "wlan0", "bg")]


def test_a_joined_network_moves_to_the_adapter_and_back(bench):
    bench.acts = [HOME_ON_WLAN0]
    bench.radios = [BUILT, PANDA_R]
    bench.w.tick()
    assert bench.moves == [("home-uuid", "wlan1", None)]
    bench.radios, bench.acts = [BUILT], []
    bench.w.tick()
    assert bench.moves[-1] == ("home-uuid", "wlan0", None)


def test_a_network_the_adapter_cannot_join_falls_back_to_the_access_point_on_it(bench):
    bench.acts, bench.radios = [HOME_ON_WLAN0], [BUILT, PANDA_R]
    bench.refuse = {("home-uuid", "wlan1")}
    bench.w.tick()
    assert bench.moves == [("home-uuid", "wlan1", None), (manage.AP_CON, "wlan1", "a")]
    bench.w.tick()
    assert len(bench.moves) == 2


def test_an_adapter_that_refuses_the_access_point_is_skipped_until_replugged(bench):
    bench.acts, bench.radios = [AP_ON_WLAN0], [BUILT, PANDA_R]
    bench.refuse = {("ap-uuid", "wlan1")}
    bench.acts = [AP_ON_WLAN0]
    bench.w.tick()
    assert bench.moves == [("ap-uuid", "wlan1", "a"), (manage.AP_CON, "wlan0", "bg")]
    assert bench.skips[-1] == {"wlan1"}     # wifijoin passes it over too
    bench.w.tick()
    assert len(bench.moves) == 2            # no retry loop
    bench.radios = [BUILT]
    bench.w.tick()
    assert bench.skips[-1] == set()
    bench.radios, bench.refuse = [BUILT, PANDA_R], set()
    bench.w.tick()
    assert bench.moves[-1][1] == "wlan1"    # plugged in again: tried again


def test_an_access_point_autoconnected_on_the_adapter_at_2_4_ghz_is_moved_to_5_ghz(bench):
    bench.radios, bench.acts, bench.band = [BUILT, PANDA_R], [AP_ON_WLAN1], "bg"
    bench.w.tick()
    assert bench.moves == [("ap-uuid", "wlan1", "a")]
    bench.w.tick()
    assert len(bench.moves) == 1


PANDA_NO_5 = Radio("wlan1", True, True, ())     # the same adapter while no country allows 5 GHz


def test_a_saved_country_is_set_in_the_kernel_and_moves_the_access_point_to_5_ghz(bench):
    bench.radios, bench.acts, bench.band, bench.country = [BUILT, PANDA_NO_5], [AP_ON_WLAN1], "bg", ("", "")
    bench.w.tick()
    bench.w.tick()
    assert bench.moves == [] and bench.regs == []     # no country: 2.4 GHz is right
    bench.country = ("DE", "")                         # the owner picks one on the Wi-Fi page
    bench.w.tick()
    assert bench.regs == ["DE"] and bench.moves == []  # the kernel takes it in the background
    bench.radios = [BUILT, PANDA_R]                    # iw now lists 5 GHz channels
    bench.w.tick()
    assert bench.moves == [("ap-uuid", "wlan1", "a")]
    bench.w.tick()
    assert bench.regs == ["DE"] and len(bench.moves) == 1


def test_a_country_saved_before_the_watcher_starts_is_set_at_its_first_look(bench):
    bench.country = ("CA", "US")
    bench.w.tick()
    assert bench.regs == ["CA"]


def test_no_country_means_no_5_ghz_channels_whatever_iw_lists(monkeypatch):
    monkeypatch.setattr(wifiradio, "_phy_cache", {})
    monkeypatch.setattr(wifiradio.subprocess, "run", lambda *a, **k: R(out=PANDA))
    monkeypatch.setattr(wifiradio.wificountry, "current", lambda: "")
    assert wifiradio._phy_info("phy1") == (True, ())
    monkeypatch.setattr(wifiradio.wificountry, "current", lambda: "US")
    assert wifiradio._phy_info("phy1") == (True, (149, 165, 36))


def test_nothing_on_the_air_brings_the_access_point_up_after_a_while(bench):
    bench.w.tick()
    bench.t = wifiradio.IDLE_S - 1
    bench.w.tick()
    assert bench.moves == []
    bench.t = wifiradio.IDLE_S + 1
    bench.w.tick()
    assert bench.moves == [(manage.AP_CON, "wlan0", "bg")]


def test_a_connection_still_coming_up_is_left_alone(bench):
    bench.radios, bench.acts = [BUILT, PANDA_R], [Active("home-uuid", "wlan0", "activating", "HomeNet")]
    bench.w.tick()
    assert bench.moves == []


def test_the_built_in_radio_is_never_skipped(bench):
    bench.refuse = {(manage.AP_CON, "wlan0")}
    bench.w.tick()
    bench.t = wifiradio.IDLE_S + 1
    bench.w.tick()
    assert bench.moves == [(manage.AP_CON, "wlan0", "bg")] and bench.skips == []
    bench.refuse = set()
    bench.t = 2 * wifiradio.IDLE_S
    bench.w.tick()
    bench.t = 3 * wifiradio.IDLE_S + 2
    bench.w.tick()      # the idle timer tries it again
    assert bench.moves[-1] == (manage.AP_CON, "wlan0", "bg") and len(bench.moves) == 2
    bench.radios = [BUILT, PANDA_R]
    bench.w.tick()
    assert bench.moves[-1] == (manage.AP_CON, "wlan1", "a")


def test_the_built_in_radio_sits_idle_while_the_adapter_carries_the_network(bench):
    bench.radios = [BUILT, PANDA_R]
    bench.acts = [Active("home-uuid", "wlan1", "activated", "HomeNet"), AP_ON_WLAN0]  # NM autoconnected the AP
    bench.w.tick()
    assert bench.downs == ["wlan0"] and bench.moves == []


def test_pick_passes_over_a_skipped_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(wifiradio, "radios", lambda: [BUILT, PANDA_R])
    monkeypatch.setattr(wifiradio, "ready_devices", lambda: {"wlan0", "wlan1"})
    assert wifiradio.pick().name == "wlan1"
    wifiradio.SKIP.write_text("wlan1\n")
    assert wifiradio.pick().name == "wlan0"


def test_a_deleted_access_point_profile_does_not_skip_the_adapter(bench):
    bench.radios = [BUILT, PANDA_R]
    bench.w.up_fn = lambda con, radio, is_ap: bench.moves.append(radio.name) or R(10, err="Error: unknown connection 'vector-bridge-ap'.")
    bench.w.tick()
    bench.t = wifiradio.IDLE_S + 1
    bench.w.tick()
    assert bench.moves == ["wlan1"] and bench.skips == []
