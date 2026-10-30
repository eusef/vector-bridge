"""scripts/image/prepare-golden.sh against stub system commands and a fake root (roadmap 1.5):
everything bench-only is gone, SSH is off with a locked password (R-D4), leftover Wi-Fi keyfiles
are deleted rather than refused, and first boot is armed only when the scrub finished."""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "image" / "prepare-golden.sh"

STUB = """#!/bin/bash
echo "$(basename "$0") $*" >> "$STUB_LOG"
case "$(basename "$0") $*" in
  "id -u") echo 0 ;;
  "findmnt "*) echo ext4 ;;
  "getent passwd 1000") echo "vectorbridge:x:1000:1000::/home/vectorbridge:/bin/bash" ;;
  "dpkg-query "*) printf 'libhamlib-utils\t4.6.2-1~bpo13+1\thamlib\t4.6.2-1~bpo13+1\n' ;;
  "systemctl is-enabled ${STUB_DISABLED:-none}") exit 1 ;;
  # Both daemons rewrite their state on the way down (seen on the 2.0 capture).
  "systemctl stop tailscaled.service") mkdir -p "$VB_GOLDEN_ROOT/var/lib/tailscale"
    echo '{"PrivateID": "bench"}' > "$VB_GOLDEN_ROOT/var/lib/tailscale/tailscaled.log.conf" ;;
  "systemctl stop NetworkManager.service") echo "[seen-bssids]" > "$VB_GOLDEN_ROOT/var/lib/NetworkManager/seen-bssids" ;;
esac
exit 0
"""
COMMANDS = ["id", "findmnt", "getent", "dpkg-query", "systemctl", "mount", "journalctl", "nmcli", "usermod", "poweroff", "sync"]


def card(tmp_path):
    """A golden card's worth of bench state, under a fake root."""
    root, app = tmp_path / "root", tmp_path / "persist" / "vector-bridge"
    files = {
        app / "config.toml": "kept",
        app / "wizard.json": "{}",
        app / "admin-pin.json": "{}",
        app / "app-tokens.json": "{}",
        app / "ssh.json": "{}",
        app / "ssh-request.json": "{}",
        app / "wifi-country.json": '{"country": "US"}',
        app / "networks" / "HomeNet.nmconnection": "[wifi]\nssid=HomeNet\n",
        app.parent / "tailscale" / "tailscaled.state": "bench node key",
        root / "etc/sudoers.d/vector-bridge-agent-deploy": "NOPASSWD",
        root / "usr/local/sbin/vb-agent-enter": "",
        root / "usr/local/sbin/vb-agent-install": "",
        root / "var/lib/tailscale/tailscaled.state": "bench node key, live copy",
        root / "var/lib/NetworkManager/internal-3bf06720-wlan0.lease": "ADDRESS=192.0.2.10\n",
        root / "var/lib/NetworkManager/dnsmasq-wlan0.leases": "1 02:00:00:00:00:01 10.42.0.210 phone *\n",
        root / "var/lib/NetworkManager/timestamps": "[timestamps]\n",
        root / "var/lib/NetworkManager/secret_key": "bench",
        root / "var/lib/NetworkManager/NetworkManager-intern.conf": "kept",
        root / "home/vectorbridge/.ssh/known_hosts": "bench hosts",
        root / "home/vectorbridge/vb-deploy/setup-tailscale.sh": "",
        root / "home/vectorbridge/.ssh/authorized_keys": "ssh-ed25519 bench-mac",
        root / "home/vectorbridge/.bash_history": "",
        root / "home/vectorbridge/vb-enter.sh": "",
        root / "home/vectorbridge/vb-golden.sh": "",
        root / "home/vectorbridge/vb-golden.log": "",
        root / "home/vectorbridge/vector-bridge/README.md": "",
        root / "root/.ssh/authorized_keys": "ssh-ed25519 bench-mac",
        root / "boot/firmware/ssh": "",
        root / "boot/firmware/cmdline.txt": "console=tty1 root=PARTUUID=648a39af-02 rootwait "
                                            "ds=nocloud;i=rpi-imager-1789869836927 cfg80211.ieee80211_regdom=US\n",
        root / "boot/firmware/vector-bridge-wifi.txt": "passphrase",
        root / "etc/ssh/ssh_host_ed25519_key": "golden",
        root / "etc/shadow-": "vectorbridge:$y$bench:20000:0:99999:7:::\n",
        root / "etc/gshadow-": "sudo:*::vectorbridge\n",
        root / "etc/NetworkManager/system-connections/old.nmconnection": "type=wifi\n",
        root / "etc/netplan/90-NM-old.yaml": "network:\n  wifis:\n",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root, app


def run(tmp_path, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in COMMANDS:
        (bin_dir / name).write_text(STUB)
        (bin_dir / name).chmod(0o755)
    root, app = card(tmp_path)
    log = tmp_path / "log"
    log.touch()
    r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, check=False, env={
        "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log), "VB_GOLDEN_ROOT": str(root),
        "VECTOR_BRIDGE_PERSIST": str(tmp_path / "persist"), **env})
    return r, log.read_text().splitlines(), root, app


def test_scrubs_every_bench_item_and_arms_first_boot(tmp_path):
    r, calls, root, app = run(tmp_path)
    assert r.returncode == 0, r.stderr
    home = root / "home/vectorbridge"
    for gone in (home / ".ssh/authorized_keys", root / "root/.ssh/authorized_keys", home / ".bash_history",
                 home / "vb-enter.sh", home / "vb-golden.sh", home / "vb-golden.log", home / "vector-bridge",
                 home / ".ssh", home / "vb-deploy", app.parent / "tailscale",
                 root / "etc/sudoers.d/vector-bridge-agent-deploy", root / "usr/local/sbin/vb-agent-enter",
                 root / "usr/local/sbin/vb-agent-install", root / "var/lib/tailscale",
                 root / "boot/firmware/ssh", root / "boot/firmware/vector-bridge-wifi.txt",
                 root / "etc/ssh/ssh_host_ed25519_key", root / "etc/shadow-", root / "etc/gshadow-",
                 app / "wizard.json", app / "admin-pin.json", app / "app-tokens.json", app / "ssh.json",
                 app / "ssh-request.json", app / "wifi-country.json"):
        assert not gone.exists(), gone
    # No Wi-Fi country (R-D7) and no cloud-init seed pointer: the rest of the command line is untouched.
    assert (root / "boot/firmware/cmdline.txt").read_text() == "console=tty1 root=PARTUUID=648a39af-02 rootwait\n"
    assert (app / "config.toml").read_text() == "kept"
    # Leftover keyfiles are deleted, not refused.
    assert not (app / "networks/HomeNet.nmconnection").exists()
    assert not (root / "etc/NetworkManager/system-connections/old.nmconnection").exists()
    assert not (root / "etc/netplan/90-NM-old.yaml").exists()
    # SSH off at the next boot, and no password to log in with until the owner sets one.
    assert "systemctl disable ssh.service" in calls and "usermod -p ! vectorbridge" in calls
    # This session may be SSH: nothing is stopped until the Wi-Fi profiles (and so a Wi-Fi session) are gone,
    # and SSH itself never is.
    stops = [i for i, c in enumerate(calls) if c.startswith(("systemctl stop", "systemctl disable --now"))]
    assert stops and min(stops) > calls.index("nmcli -t -f NAME,TYPE connection show")
    assert not any(calls[i].startswith(("systemctl stop ssh", "systemctl disable --now ssh")) for i in stops)
    # Daemon state goes after its daemon stops (the stub rewrites it, as the real ones do); NM's config stays.
    assert {"systemctl stop tailscaled.service", "systemctl stop NetworkManager.service"} <= set(calls)
    assert calls.index("systemctl mask --runtime NetworkManager.service") < calls.index("systemctl stop NetworkManager.service")
    assert sorted(p.name for p in (root / "var/lib/NetworkManager").iterdir()) == ["NetworkManager-intern.conf"]
    assert (root / "etc/vector-bridge-firstboot").exists() and calls[-1] == "poweroff "


def test_a_boot_partition_wifi_file_refuses_and_leaves_first_boot_unarmed(tmp_path):
    (tmp_path / "root/boot/firmware").mkdir(parents=True)
    (tmp_path / "root/boot/firmware/custom.toml").write_text('[wlan]\nssid = "HomeNet"\n')
    r, calls, root, _ = run(tmp_path)
    assert r.returncode == 4
    assert "custom.toml" in (root / "boot/firmware/prepare-golden-refused.txt").read_text()
    assert not (root / "etc/vector-bridge-firstboot").exists() and "poweroff " not in calls
    assert not any(c.startswith("systemctl stop") for c in calls)  # the refusal still reaches a tailnet session


def test_refuses_without_the_ssh_units(tmp_path):
    r, calls, root, app = run(tmp_path, STUB_DISABLED="vb-ssh-apply.path")
    assert r.returncode == 1 and "vb-ssh-apply.path is not installed" in r.stderr
    assert (app / "admin-pin.json").exists() and (root / "home/vectorbridge/.ssh/authorized_keys").exists()
    assert "usermod -p ! vectorbridge" not in calls


def test_refuses_without_avahi_daemon(tmp_path):
    r, calls, root, app = run(tmp_path, STUB_DISABLED="avahi-daemon.service")
    assert r.returncode == 1 and "avahi-daemon.service is not installed" in r.stderr
    assert "poweroff " not in calls


def test_removes_the_imager_cloud_init_seed_and_turns_cloud_init_off(tmp_path):
    seed = {
        "boot/firmware/user-data": "#cloud-config\nuser:\n  passwd: $y$bench\n  ssh_authorized_keys:\n    - ssh-ed25519 bench-mac\n",
        "boot/firmware/meta-data": "instance-id: rpi-imager-1789869836927\n",
        "boot/firmware/network-config": "network:\n  wifis:\n",
        "var/lib/cloud/instances/rpi-imager-1789869836927/user-data.txt": "passwd: $y$bench",
        "var/log/cloud-init.log": "", "var/log/cloud-init.log.1.gz": "", "var/log/cloud-init-output.log": "",
        "etc/cloud/cloud.cfg": "",
    }
    for rel, text in seed.items():
        (tmp_path / "root" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "root" / rel).write_text(text)
    r, _, root, _ = run(tmp_path)
    assert r.returncode == 0, r.stderr  # the seed's Wi-Fi section is removed, not refused
    for rel in seed:
        if rel != "etc/cloud/cloud.cfg":
            assert not (root / rel).exists(), rel
    assert not (root / "var/lib/cloud").exists()
    assert (root / "etc/cloud/cloud-init.disabled").exists() and (root / "etc/cloud/cloud.cfg").exists()


def test_clears_login_records_and_install_logs_and_ships_utc(tmp_path):
    bench = {
        "var/log/wtmp": "bench login", "var/log/wtmp.db": "bench login", "var/log/btmp": "bad login",
        "var/log/lastlog": "bench login", "home/vectorbridge/install.log": "passphrase=golden",
        "home/vectorbridge/.sudo_as_admin_successful": "", "home/vectorbridge/.bashrc": "kept",
        "etc/timezone": "America/Denver\n",
        "etc/localtime": "MST",
    }
    for rel, text in bench.items():
        (tmp_path / "root" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "root" / rel).write_text(text)
    r, _, root, _ = run(tmp_path)
    assert r.returncode == 0, r.stderr
    for f in ("wtmp", "wtmp.db", "btmp", "lastlog"):
        assert (root / "var/log" / f).read_text() == "", f
    home = root / "home/vectorbridge"
    assert not (home / "install.log").exists() and not (home / ".sudo_as_admin_successful").exists()
    assert (home / ".bashrc").read_text() == "kept"
    assert (root / "etc/timezone").read_text() == "Etc/UTC\n"
    assert os.readlink(root / "etc/localtime") == "../usr/share/zoneinfo/Etc/UTC"


def test_writes_the_package_manifest_for_the_source_offer(tmp_path):
    r, calls, root, _ = run(tmp_path)
    assert r.returncode == 0, r.stderr
    manifest = (root / "boot/firmware/vector-bridge-packages.txt").read_text()
    assert manifest == "libhamlib-utils\t4.6.2-1~bpo13+1\thamlib\t4.6.2-1~bpo13+1\n"
    assert any(c.startswith("dpkg-query -W -f=") and "${source:Package}" in c for c in calls)
