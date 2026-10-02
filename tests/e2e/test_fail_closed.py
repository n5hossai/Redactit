"""Fail closed (THREAT_MODEL T7): with no host, a killed host, a host stuck warming, an
engine that cannot start or a host that breaks the protocol, a paste or upload is blocked,
the user is told why, and the page sees nothing of it (T5).

These use no models: the hosts here are missing or stand-ins (fakehost.py).
"""

import os
import signal
import sys
import time

import pytest

pytest.importorskip("playwright")

from browserkit import notice_text, page_view, wait_for  # noqa: E402

# Synthetic values only.
NAME, EMAIL, CARD = "Priya Okafor", "priya.okafor@northwind.com", "4111 1111 1111 1111"
SECRETS = [NAME, "Okafor", EMAIL, CARD, CARD.replace(" ", "")]
PASTE = f"Please email {NAME} at {EMAIL}; she paid with card {CARD}."
CHAT = "https://claude.ai/new"


def assert_page_saw_nothing(page):
    """No raw value anywhere the site's scripts could look, and no paste, drop or file
    event reached them at all."""
    view = page_view(page)
    assert not [s for s in SECRETS if s in view]
    seen = page.evaluate("window.__seen")
    assert not [e for e in seen if e["type"] in ("paste", "drop", "change", "beforeinput")], seen
    assert page.evaluate("window.__attachments.length") == 0
    assert page.evaluate("document.getElementById('upload').files.length") == 0


def notice(s, page, contains: str) -> str:
    return wait_for(lambda: (t := notice_text(s.browser.context, page)) and contains in t and t, timeout=30)


def kill(pid: int) -> None:
    os.kill(pid, signal.SIGTERM if sys.platform == "win32" else signal.SIGKILL)


@pytest.fixture
def secret_file(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text(PASTE + "\n", encoding="utf-8")
    return path


def test_without_a_host_the_paste_and_the_upload_are_blocked(setup, secret_file):
    s = setup(host=None)  # never registered: Chrome cannot find it
    page = s.browser.open(CHAT)
    s.browser.paste(page, ".ProseMirror", PASTE)
    assert s.browser.wait_recent(1)[0]["code"] == "host_missing"
    assert "Nothing was sent" in notice(s, page, "not installed")
    page.set_input_files("#upload", str(secret_file))
    assert s.browser.wait_recent(2)[0] == {"kind": "txt", "site": "claude.ai", "held": True, "code": "host_missing"}
    assert page.locator(".ProseMirror").inner_text() == ""
    assert_page_saw_nothing(page)


def test_a_host_killed_mid_request_blocks_the_paste_and_the_upload(setup, needs_registry, secret_file):
    s = setup(host="hang")  # takes requests, never answers
    page = s.browser.open(CHAT)
    reg = s.registration

    s.browser.paste(page, ".ProseMirror", PASTE)
    wait_for(lambda: [f for f in reg.frames() if f["type"] == "chunk"])  # the host holds the paste
    kill(reg.pid())
    assert s.browser.wait_recent(1)[0]["code"] in ("host_exited", "host_down")
    notice(s, page, "stopped")

    reg.pid_file.unlink()
    page.set_input_files("#upload", str(secret_file))  # starts a new host, killed the same way
    wait_for(lambda: len([f for f in reg.frames() if f["type"] == "chunk"]) == 2)
    kill(reg.pid())
    assert s.browser.wait_recent(2)[0]["code"] in ("host_exited", "host_down")
    assert_page_saw_nothing(page)


def test_a_paste_held_past_the_warm_up_limit_is_blocked(setup, needs_registry):
    s = setup(host="warming", warm_hold_ms=1500)  # 30 s in the real build
    page = s.browser.open(CHAT)
    s.browser.paste(page, ".ProseMirror", PASTE)
    assert s.browser.wait_recent(1, timeout=30)[0] == {"kind": "text", "site": "claude.ai", "held": True,
                                                       "code": "warming_timeout"}
    notice(s, page, "to start")
    types = [f["type"] for f in s.registration.frames()]
    assert types == ["redact_text", "chunk", "cancel"]  # sent at once, then withdrawn at the deadline
    assert_page_saw_nothing(page)


def test_cancel_from_the_panel_blocks_the_paste_and_tells_the_host(setup, needs_registry):
    s = setup(host="hang")
    page = s.browser.open(CHAT)
    s.browser.paste(page, ".ProseMirror", PASTE)
    wait_for(lambda: [f for f in s.registration.frames() if f["type"] == "chunk"])
    job = s.browser.evaluate("[...jobs.keys()][0]")
    assert s.browser.api(s.browser.panel(), {"type": "redactit/cancel", "job": job}) == {"ok": True}
    assert s.browser.wait_recent(1)[0]["code"] == "cancelled"
    wait_for(lambda: [f for f in s.registration.frames() if f == {"type": "cancel", "id": job}])
    assert_page_saw_nothing(page)


def test_keep_ready_starts_the_host_at_page_load_and_reconnects_after_a_crash(setup, needs_registry):
    s = setup(host="hang")
    s.browser.open(CHAT)
    time.sleep(2)
    assert not s.registration.pid_file.exists()  # by default the host waits for the first paste
    s.browser.evaluate("chrome.storage.local.set({keepReady: true})")
    s.browser.open(CHAT)  # a page load with "Keep Redactit ready" on
    first = s.registration.pid()
    s.registration.pid_file.unlink()
    kill(first)
    assert s.registration.pid() != first  # started again with no paste waiting
    status = s.browser.api(s.browser.panel(), {"type": "redactit/status"})
    assert status["settings"]["keepReady"] is True
    assert status["state"] in ("starting", "warming", "ready-text", "ready-all")


@pytest.mark.parametrize(("mode", "code", "says"), [
    ("engine-error", "engine_unavailable", "engine could not start"),
    ("garbage", "protocol", "could not check"),
])
def test_an_engine_or_protocol_error_blocks_the_paste(setup, needs_registry, mode, code, says):
    s = setup(host=mode)
    page = s.browser.open(CHAT)
    s.browser.paste(page, ".ProseMirror", PASTE)
    assert s.browser.wait_recent(1)[0]["code"] == code
    notice(s, page, says)
    assert_page_saw_nothing(page)
