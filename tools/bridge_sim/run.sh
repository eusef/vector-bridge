#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# bridge_sim launcher: the VectorBridge code of this checkout, on a developer machine, with no Pi and no radio.
# Test harness only: the bridge service never imports it, and client apps never ship it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"  # tools/bridge_sim sits two levels below the repository root

# The repository's own .venv when it has one; otherwise a venv of the sim's own with numpy, the
# bridge's only runtime dependency (pyproject.toml). The bridge itself is never installed: it is
# imported from this checkout's src/ through PYTHONPATH.
if [ -x "$REPO/.venv/bin/python" ]; then
    PY="$REPO/.venv/bin/python"
else
    PY="$HERE/.venv/bin/python"
    if [ ! -x "$PY" ]; then
        BASE="${BRIDGE_SIM_PYTHON:-python3}"
        "$BASE" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
            || { echo "bridge_sim: needs Python 3.11 or newer ($BASE is too old)" >&2; exit 2; }
        "$BASE" -m venv "$HERE/.venv" >&2
    fi
    if ! "$PY" -s -c 'import numpy' 2>/dev/null; then
        "$PY" -m pip install --quiet --disable-pip-version-check "numpy>=1.24" >&2
    fi
fi
"$PY" -s -c 'import sys, numpy; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
    || { echo "bridge_sim: $PY needs Python 3.11 or newer with numpy" >&2; exit 2; }

if [ "${1:-}" = "--prepare" ]; then  # selftest.sh: settle the Python, print its path, start nothing
    echo "$PY"
    exit 0
fi

# -s skips the user site; PYTHONPATH puts this checkout's src/ ahead of anything installed in the venv.
PYTHONPATH="$REPO/src" exec "$PY" -s "$HERE/sim.py" "$@"
