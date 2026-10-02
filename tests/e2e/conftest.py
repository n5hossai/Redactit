"""Browser tests: skipped, with the reason, wherever they cannot run.

They need the `e2e` dependency group (`uv run --group e2e ...`) and its Chromium
(`playwright install chromium`). Playwright talks to the browser over pipes, but its
event loop needs a local socket pair, so these tests lift the suite's socket ban; the
pages themselves are answered by Playwright and nothing reaches the network.
"""

import os
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
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        browserkit.unavailable("needs the e2e group: uv run --group e2e")
    with sync_playwright() as p:
        if not os.environ.get("REDACTIT_E2E_CHROMIUM") and not Path(p.chromium.executable_path).exists():
            browserkit.unavailable("Chromium not installed: uv run --group e2e playwright install chromium")
        yield p


@pytest.fixture
def needs_registry():
    if not browserkit.registry_allowed():
        browserkit.unavailable(browserkit.REGISTRY_REASON)


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
