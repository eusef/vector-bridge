import tomllib
from pathlib import Path

import vector_bridge
from vector_bridge import home_page


def test_one_version_in_the_package_and_pyproject():
    root = Path(__file__).resolve().parents[1]
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == vector_bridge.__version__


def test_version_text_carries_the_build_commit(monkeypatch):
    monkeypatch.setattr(vector_bridge, "COMMIT", "e2e8228+")
    assert vector_bridge.version_text() == f"{vector_bridge.__version__} (e2e8228+)"
    monkeypatch.setattr(vector_bridge, "COMMIT", "")
    assert vector_bridge.version_text() == vector_bridge.__version__


def test_home_page_footer_shows_the_version():
    assert f'<p class="ver">VectorBridge {vector_bridge.version_text()}</p>' in home_page.HOME
    assert "__VERSION__" not in home_page.HOME
