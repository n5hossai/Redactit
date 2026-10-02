"""Attach to chat (redactit/attach): the worker hands the side panel's last redacted file,
as the host returned it, to the chat tab's content script, which inserts it as it would a
redacted drop. The caller names a job and never sends bytes.

The scripted host (stubhost.js) stands behind the worker, so these run on every OS.
"""

import base64
import io
import json
import time

import pytest

pytest.importorskip("playwright")

import browserkit  # noqa: E402
from browserkit import page_view, wait_for  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

NAME = "Priya Okafor"
CHAT = "https://claude.ai/new"


def png(text: str, shade: int) -> bytes:
    img = Image.new("RGB", (400, 120), (shade, shade, shade))
    ImageDraw.Draw(img).text((20, 40), text, fill="black")
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


ORIGINAL = png(f"Patient: {NAME}", 245)
REDACTED = png("Patient: ████████", 240)  # what the scripted host hands back


def prepared(setup, tmp_path, **build):
    """A chat tab and the panel, with one PNG redacted through the panel for that chat."""
    s = setup(**build)
    s.browser.stub_host("redact", files={"image": base64.b64encode(REDACTED).decode()})
    chat = s.browser.open(CHAT)
    panel = s.browser.panel()
    path = tmp_path / f"{NAME} scan.png"
    path.write_bytes(ORIGINAL)
    s.browser.drop_files(panel, "#stage", [path])
    job = wait_for(lambda: s.browser.evaluate("attachable && attachable.job"), timeout=30)
    tab = s.browser.evaluate("chrome.tabs.query({url: 'https://claude.ai/*'}).then(([t]) => t.id)")
    return s, chat, panel, job, tab


def attach(s, panel, job, tab):
    return s.browser.api(panel, {"type": "redactit/attach", "job": job, "tabId": tab})


def test_attach_puts_the_redacted_file_into_the_chat_once(setup, tmp_path):
    s, chat, panel, job, tab = prepared(setup, tmp_path)
    assert attach(s, panel, job, tab) == {"ok": True}
    wait_for(lambda: chat.evaluate("window.__attachments.length") == 1)
    meta = chat.evaluate("(f => ({name: f.name, type: f.type}))(window.__attachments[0])")
    assert meta == {"name": "redacted-1.png", "type": "image/png"}
    assert base64.b64decode(chat.evaluate("window.__read(0)")) == REDACTED  # the host's bytes, exactly
    changes = [e for e in chat.evaluate("window.__seen") if e["type"] == "change"]
    assert changes and all(not e["trusted"] and e["files"] == [dict(meta, size=len(REDACTED))] for e in changes)
    assert NAME not in page_view(chat)
    again = attach(s, panel, job, tab)  # attached copies are not kept
    assert (again["ok"], again["code"]) == (False, "expired")
    assert chat.evaluate("window.__attachments.length") == 1


def test_an_unknown_replaced_or_timed_out_job_is_refused(setup, tmp_path):
    s, chat, panel, job, tab = prepared(setup, tmp_path, attach_keep_ms=3000)
    assert attach(s, panel, "j-unknown", tab)["code"] == "expired"
    path = tmp_path / "second.png"
    path.write_bytes(ORIGINAL)
    s.browser.drop_files(panel, "#stage", [path])  # the panel's next file replaces the first
    second = wait_for(lambda: (j := s.browser.evaluate("attachable && attachable.job")) != job and j, timeout=30)
    assert attach(s, panel, job, tab)["code"] == "expired"
    time.sleep(3.2)
    late = attach(s, panel, second, tab)
    assert (late["ok"], late["code"]) == (False, "expired") and "no longer available" in late["message"]
    assert chat.evaluate("window.__attachments.length") == 0


def test_a_content_script_cannot_attach(setup, tmp_path):
    s, chat, panel, job, tab = prepared(setup, tmp_path)
    message = {"type": "redactit/attach", "job": job, "tabId": tab}
    answer = browserkit.content_script_eval(s.browser.context, chat, f"chrome.runtime.sendMessage({json.dumps(message)})")
    assert (answer["ok"], answer["code"]) == (False, "not_allowed")
    assert chat.evaluate("window.__attachments.length") == 0
    assert attach(s, panel, job, tab) == {"ok": True}  # still there for the panel


def test_a_tab_that_left_the_allowed_sites_is_refused(setup, tmp_path):
    s, chat, panel, job, tab = prepared(setup, tmp_path)
    chat.goto(browserkit.HELPER)  # the same tab, now on a site Redactit does not serve
    answer = attach(s, panel, job, tab)
    assert (answer["ok"], answer["code"]) == (False, "not_allowed_site")
    assert attach(s, panel, job, 999_999)["code"] == "not_allowed_site"  # no such tab


def test_without_a_working_adapter_attach_fails_and_inserts_nothing(setup, tmp_path):
    s = setup(adapter_check_ms=1000)
    s.browser.stub_host("redact", files={"image": base64.b64encode(REDACTED).decode()})
    other = s.browser.open("https://chatgpt.com/")  # the stand-in has none of chatgpt.com's selectors
    panel = s.browser.panel()
    tab = s.browser.evaluate("chrome.tabs.query({url: 'https://chatgpt.com/*'}).then(([t]) => t.id)")
    wait_for(lambda: s.browser.evaluate(f"pageAdapters.get({tab})?.active === false"))
    path = tmp_path / "scan.png"
    path.write_bytes(ORIGINAL)
    s.browser.drop_files(panel, "#stage", [path])
    job = wait_for(lambda: s.browser.evaluate("attachable && attachable.job"), timeout=30)
    answer = attach(s, panel, job, tab)
    assert (answer["ok"], answer["code"]) == (False, "insert_failed")
    assert other.evaluate("window.__attachments.length") == 0
    assert other.evaluate("document.getElementById('upload').files.length") == 0
