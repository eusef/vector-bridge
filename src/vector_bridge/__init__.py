"""VectorBridge: transparent network audio bridge for the KX2."""

__version__ = "0.2.2"  # the release image this code ships in; keep pyproject.toml's version the same

try:  # written by tools/deploy_pi.sh and scripts/install.sh at build time; absent in a source checkout
    from ._build import COMMIT
except ImportError:
    COMMIT = ""


def version_text() -> str:
    """For people: "0.2.0 (e2e8228)", or the bare version when no build stamp exists."""
    return f"{__version__} ({COMMIT})" if COMMIT else __version__
