#!/usr/bin/env bash
# Write imager/os_list.json for Raspberry Pi Imager (ROADMAP 4.2) from a built release.
#
#   scripts/image/os-list.sh <version> [<release-dir>]
#
# Reads vector-bridge-<version>.img (kept by xz -k) and vector-bridge-<version>.img.xz from
# <release-dir> (default release/), and writes imager/os_list.json with the sizes and
# checksums Imager verifies. The URLs are the fixed GitHub Release download links on the
# public repository (PUBLISHING.md), tag v<version>; the icon is attached to the same release.
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
    "description": "Radio bridge for a Raspberry Pi Zero 2 W: digital-mode audio and rig control "
                   "over Wi-Fi for VectorComm, WSJT-X and JS8Call. Pi Zero 2 W only.",
    "icon": f"{base}/vector-bridge-icon.png",
    "url": f"{base}/vector-bridge-{ver}.img.xz",
    "extract_size": int(isz),
    "extract_sha256": isha,
    "image_download_size": int(xsz),
    "image_download_sha256": xsha,
    "release_date": date,
    "init_format": "none",
    "devices": ["pi3-64bit"],
}
with open(out, "w") as f:
    json.dump({"os_list": [entry]}, f, indent=2)
    f.write("\n")
print(out)
PY
