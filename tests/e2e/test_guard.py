"""The page's own ways around interception are closed: the main-world guard
(extension/content/guard.js).

No host is needed: everything here is refused or intercepted before one would be asked.
"""

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
