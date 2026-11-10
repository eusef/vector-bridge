"""bridge_sim: its own end-to-end self-test passes against this checkout's bridge code.

The self-test starts the sim three times on free ports and drives it over UDP and HTTP, so this
takes about two minutes. See tools/bridge_sim/README.md.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SELFTEST = REPO / "tools" / "bridge_sim" / "selftest.sh"
TAIL_LINES = 40


def tail(*parts) -> str:
    text = "".join(p.decode(errors="replace") if isinstance(p, bytes) else (p or "") for p in parts)
    return "\n".join(text.splitlines()[-TAIL_LINES:])


@pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="bridge_sim runs on macOS or Linux only")
@pytest.mark.skipif(shutil.which("bash") is None, reason="bridge_sim's scripts need bash")
def test_bridge_sim_selftest_passes():
    # Without a repository .venv, run.sh builds its own venv from BRIDGE_SIM_PYTHON: this interpreter
    # already meets the bridge's requires-python, where a bare python3 on PATH may not.
    env = {**os.environ}
    env.setdefault("BRIDGE_SIM_PYTHON", sys.executable)
    try:
        proc = subprocess.run(["bash", str(SELFTEST)], cwd=REPO, env=env, capture_output=True, text=True,
                              timeout=600, check=False)
    except subprocess.TimeoutExpired as e:
        pytest.fail(f"selftest.sh still running after {e.timeout} s; last {TAIL_LINES} lines:\n{tail(e.stdout, e.stderr)}")
    assert proc.returncode == 0, f"selftest.sh exited {proc.returncode}; last {TAIL_LINES} lines:\n{tail(proc.stdout, proc.stderr)}"
