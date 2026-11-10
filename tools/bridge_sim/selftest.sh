#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# bridge_sim self-test: exits 0 when the simulator works end to end on this machine, non-zero otherwise.
# Test harness only: the bridge service never imports it, and client apps never ship it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash -n "$HERE/run.sh"
bash -n "$HERE/selftest.sh"
PY="$(bash "$HERE/run.sh" --prepare)"
exec "$PY" -E -s "$HERE/selftest.py"
