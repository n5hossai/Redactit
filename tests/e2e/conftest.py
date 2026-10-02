"""Browser tests: skipped, with the reason, wherever they cannot run.

They need the `e2e` dependency group (`uv run --group e2e ...`) and its Chromium
(`playwright install chromium`). Playwright talks to the browser over pipes, but its
event loop needs a local socket pair, so these tests lift the suite's socket ban; the
pages themselves are answered by Playwright and nothing reaches the network.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # hostkit, shared with tests/test_host.py

import browserkit  # noqa: E402


def pytest_collection_modifyitems(items):
    for item in items:
        if Path(str(item.fspath)).parent == Path(__file__).parent:
            item.add_marker(pytest.mark.enable_socket)


@pytest.fixture(scope="session")
def playwright():
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs the e2e group: uv run --group e2e")
    with sync_api.sync_playwright() as p:
        try:
            p.chromium.executable_path  # noqa: B018 - raises if Playwright's browsers are missing
        except Exception:  # noqa: BLE001
            pytest.skip("Chromium not installed: uv run --group e2e playwright install chromium")
        yield p


@pytest.fixture
def needs_registry():
    if not browserkit.registry_allowed():
        pytest.skip("Windows Chromium finds native hosts only in the registry; "
                    "set REDACTIT_E2E_REGISTRY=1 to let this test add and remove an HKCU key")


@pytest.fixture
def setup(playwright, tmp_path):
    """Call with host=None (unregistered), 'real' or a fakehost mode, plus build options."""
    made = []

    def make(host=None, **build):
        s = browserkit.Setup(playwright, tmp_path / f"setup{len(made)}", host=host, **build)
        made.append(s)
        return s

    yield make
    for s in reversed(made):
        s.close()
