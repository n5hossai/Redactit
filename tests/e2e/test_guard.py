"""The page's own ways around interception are closed, and real values have no path to a
page: the main-world guard (extension/content/guard.js) and T6 in the service worker.

No host is needed: everything here is refused or intercepted before one would be asked.
"""

import json

import pytest

pytest.importorskip("playwright")

import browserkit  # noqa: E402
from browserkit import notice_text, page_view, wait_for  # noqa: E402

NAME, EMAIL = "Priya Okafor", "priya.okafor@northwind.com"
PASTE = f"Please email {NAME} at {EMAIL}."
CHAT = "https://claude.ai/new"


def test_the_page_cannot_read_the_clipboard_or_open_files_itself(setup):
    """With clipboard permission granted, so the browser itself would allow the read, the
    page's own reads and file pickers are refused, and it cannot undo the guard. A paste
    still goes to Redactit (blocked here, as no host is installed)."""
    s = setup(host=None)
    b = s.browser
    for origin in ("https://claude.ai", browserkit.HELPER.rstrip("/")):
        b.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=origin)
    b.copy(PASTE)
    helper = b.open(browserkit.HELPER)  # an origin without the guard
    helper.bring_to_front()
    assert helper.evaluate("navigator.clipboard.readText()") == PASTE
    chat = b.open(CHAT)
    chat.bring_to_front()
    assert chat.evaluate(browserkit.GUARD_PROBE) == browserkit.GUARD_REFUSED
    b.paste(chat, ".ProseMirror", PASTE)
    assert b.wait_recent(1)[0]["code"] == "host_missing"
    wait_for(lambda: "Nothing was sent" in notice_text(b.context, chat))
    assert not [v for v in (NAME, EMAIL) if v in page_view(chat)]


# How a page could reach a window in the task that creates it, and whether the guard is
# already in that window (THREAT_MODEL §5). Chromium runs the content scripts at once in a
# new frame or popup with no document to load; one that loads a document gets them only
# when that document starts, and until then (in the creating task) its window, which the
# document goes on to use, is one the guard has not reached.
NEW_WINDOWS = {
    "iframe": ("const f = document.createElement('iframe'); document.body.append(f);"
               " return f.contentWindow;", True),
    "iframe_about_blank": ("const f = document.createElement('iframe'); f.src = 'about:blank';"
                           " document.body.append(f); return f.contentWindow;", True),
    "popup": ("return window.open('');", True),
    "iframe_srcdoc": ("const f = document.createElement('iframe'); f.srcdoc = '<p>x';"
                      " document.body.append(f); return f.contentWindow;", False),
    "iframe_blob": ("const f = document.createElement('iframe');"
                    " f.src = URL.createObjectURL(new Blob(['<p>x'], {type: 'text/html'}));"
                    " document.body.append(f); return f.contentWindow;", False),
    "popup_same_origin": ("return window.open('/elsewhere');", False),
}


@pytest.mark.parametrize("kind", list(NEW_WINDOWS))
def test_which_new_windows_have_the_guard_in_the_task_that_makes_them(setup, kind):
    """Records Chromium's behaviour, which THREAT_MODEL §5 states. If a False case turns
    True, Chromium now injects earlier and §5 can drop that residual risk; if a True case
    turns False, a gap has opened and §5 is wrong."""
    code, guarded = NEW_WINDOWS[kind]
    s = setup(host=None)
    chat = s.browser.open(CHAT)
    chat.bring_to_front()
    found = chat.evaluate(f"""() => {{
      const w = (() => {{ {code} }})();
      return [w.HTMLElement.prototype.click, w.EventTarget.prototype.dispatchEvent, w.showOpenFilePicker,
              w.Clipboard.prototype.readText].map((f) => !String(f).includes('[native code]'));
    }}""")
    assert found == [guarded] * 4


def test_a_worker_has_no_clipboard_and_no_file_pickers(setup):
    """Why workers are no way around the guard: they have neither API to call."""
    s = setup(host=None)
    chat = s.browser.open(CHAT)
    assert chat.evaluate("""() => new Promise((resolve) => {
      const src = 'postMessage([typeof navigator.clipboard, typeof self.showOpenFilePicker])';
      const w = new Worker(URL.createObjectURL(new Blob([src], {type: 'text/javascript'})));
      w.onmessage = (e) => resolve(e.data);
    })""") == ["undefined", "undefined"]


def test_a_paste_listener_a_page_adds_to_a_new_frame_runs_after_redactit(setup):
    """A new frame with no document to load gets intercept.js at once, so its listener on
    the frame's window comes before one the page adds in the same task."""
    s = setup(host=None)
    b = s.browser
    chat = b.open(CHAT)
    chat.evaluate("""() => {
      window.__early = [];
      const f = document.createElement('iframe');
      f.id = 'ed';
      document.body.prepend(f);
      const w = f.contentWindow;
      w.addEventListener('paste', (e) => window.__early.push(e.clipboardData.getData('text/plain')), true);
      w.document.designMode = 'on';
    }""")
    b.copy(PASTE)
    chat.bring_to_front()
    chat.frame_locator("#ed").locator("body").click()
    chat.keyboard.press("Control+V")
    assert b.wait_recent(1)[0]["code"] == "host_missing"
    assert chat.evaluate("window.__early") == []


def test_a_content_script_cannot_reach_real_values(setup):
    """T6: from the content script's own world (as a page that took it over would be),
    re-mapping, the review queue and a remap job are all refused."""
    s = setup(host=None)
    chat = s.browser.open(CHAT)
    tab = s.browser.evaluate("chrome.tabs.query({url: 'https://claude.ai/*'}).then(([t]) => t.id)")
    for message in ({"type": "redactit/remap", "text": "[PERSON_1]", "tabId": tab},
                    {"type": "redactit/review-list"}, {"type": "redactit/review-get", "job": "x"},
                    {"type": "redactit/review-decide", "job": "x", "approve": True}):
        answer = browserkit.content_script_eval(s.browser.context, chat,
                                                f"chrome.runtime.sendMessage({json.dumps(message)})")
        assert answer["ok"] is False and answer["code"] == "not_allowed", message["type"]
    port = browserkit.content_script_eval(s.browser.context, chat, """new Promise((resolve) => {
        const port = chrome.runtime.connect({name: 'redactit/job'});
        port.onMessage.addListener((m) => resolve(m));
        port.postMessage({op: 'start', kind: 'remap', size: 10, total: 1});
    })""")
    assert port["op"] == "blocked" and port["code"] == "extension_error"
    # The same request from the side panel's page is allowed (and fails only for want of a host).
    panel = s.browser.panel()
    answer = s.browser.api(panel, {"type": "redactit/remap", "text": "[PERSON_1]", "tabId": tab})
    assert answer["ok"] is False and answer["code"] == "host_missing"
