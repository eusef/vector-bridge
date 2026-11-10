#!/usr/bin/env bash
# Write imager/os_list.json for Raspberry Pi Imager (ROADMAP 4.2) from a built release.
#
#   scripts/image/os-list.sh <version> [<release-dir>]
#
# Reads vector-bridge-<version>.img (kept by xz -k) and vector-bridge-<version>.img.xz from
# <release-dir> (default release/), and writes imager/os_list.json with the sizes and
# checksums Imager verifies. The URLs are the fixed GitHub Release download links on the
# public repository (PUBLISHING.md), tag v<version>; the icon is attached to the same release.
# The imager.devices block lists the boards the image supports, so Imager's Device page
# offers them instead of opening empty (ROADMAP 4.14, 4.19). The entries are copied from
# Raspberry Pi's own list (downloads.raspberrypi.com/os_list_imagingutility_v4.json,
# checked 2026-11-09); the image's "devices" tags name the 64-bit tag of each board.
# The Pi 5 entry is "exclusive": a Pi 5 sees only images that carry "pi5-64bit".
set -euo pipefail
VERSION=${1:?version, e.g. 0.2.0}
REPO=$(git rev-parse --show-toplevel)
DIR=${2:-$REPO/release}
IMG="$DIR/vector-bridge-$VERSION.img"
XZ="$IMG.xz"
for f in "$IMG" "$XZ"; do [ -f "$f" ] || { echo "missing $f" >&2; exit 1; }; done
BASE="https://github.com/eusef/vector-bridge/releases/download/v$VERSION"
size() { stat -Lf %z "$1" 2>/dev/null || stat -Lc %s "$1"; }
sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
python3 - "$REPO/imager/os_list.json" "$VERSION" "$BASE" "$(size "$IMG")" "$(sha "$IMG")" \
  "$(size "$XZ")" "$(sha "$XZ")" "$(date -u +%Y-%m-%d)" <<'PY'
import json, sys
out, ver, base, isz, isha, xsz, xsha, date = sys.argv[1:]
entry = {
    "name": f"VectorBridge {ver}",
    "description": "Radio bridge for a Raspberry Pi: digital-mode audio and rig control over Wi-Fi "
                   "for VectorComm, WSJT-X and JS8Call. For the Pi Zero 2 W, Pi 3, Pi 4 and Pi 5.",
    "icon": f"{base}/vector-bridge-icon.png",
    "url": f"{base}/vector-bridge-{ver}.img.xz",
    "extract_size": int(isz),
    "extract_sha256": isha,
    "image_download_size": int(xsz),
    "image_download_sha256": xsha,
    "release_date": date,
    "init_format": "none",
    "devices": ["pi3-64bit", "pi4-64bit", "pi5-64bit"],
}
icons = "https://downloads.raspberrypi.com/imager/icons"
def board(name, tags, icon, description, matching="inclusive"):
    entry = {"name": name, "tags": tags, "default": False, "icon": f"{icons}/{icon}",
             "description": description, "matching_type": matching, "capabilities": []}
    if matching == "exclusive":
        del entry["default"]  # Raspberry Pi's Pi 5 entry has no "default" key
    return entry
devices = [
    board("Raspberry Pi 5", ["pi5-64bit", "pi5-32bit"], "RPi_5.png",
          "Raspberry Pi 5, 500 / 500+, and Compute Module 5", "exclusive"),
    board("Raspberry Pi 4", ["pi4-64bit", "pi4-32bit"], "RPi_4.png",
          "Raspberry Pi 4 Model B, 400, and Compute Module 4 / 4S"),
    board("Raspberry Pi 3", ["pi3-64bit", "pi3-32bit"], "RPi_3.png",
          "Raspberry Pi 3 Model A+ / B / B+ and Compute Module 3 / 3+"),
    board("Raspberry Pi Zero 2 W", ["pi3-64bit", "pi3-32bit"], "RPi_Zero_2_W.png",
          "Raspberry Pi Zero 2 W"),
]
with open(out, "w") as f:
    json.dump({"imager": {"devices": devices}, "os_list": [entry]}, f, indent=2)
    f.write("\n")
print(out)
PY
