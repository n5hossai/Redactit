"""Review follows the policy the host loaded (protocol 2): a page's result is held for the
side panel when the mode is "always", or "low_confidence" with a decision marked for
review, and never when it is "off". Nothing reaches the page until the panel approves.

The hosts here are stand-ins (fakehost.py) that answer with fixed redacted text under a
fixed review mode, so each case is exact; the real host's counts are tested in
tests/test_host.py and tests/unit/test_native.py.
"""

import pytest

pytest.importorskip("playwright")

from browserkit import notice_text, page_view, wait_for  # noqa: E402

NAME, EMAIL = "Priya Okafor", "priya.okafor@northwind.com"
PASTE = f"Please email {NAME} at {EMAIL}."
REDACTED = "Please email [PERSON_1] at [EMAIL_1]."  # what every stand-in host answers
CHAT = "https://claude.ai/new"


@pytest.mark.parametrize(("mode", "announced", "reason", "approve"), [
    ("review-always", "always", "always", True),  # held even with no decision marked
    ("review-low", "low_confidence", "low_confidence", False),
])
def test_a_result_the_policy_sends_to_review_waits_for_the_panel(setup, needs_registry, mode, announced, reason,
                                                                  approve):
    s = setup(host=mode)
    b = s.browser
    chat = b.open(CHAT)
    panel = b.panel()
    editor = chat.locator(".ProseMirror")
    b.paste(chat, ".ProseMirror", PASTE)
    reviews = wait_for(lambda: b.api(panel, {"type": "redactit/review-list"}))
    assert [(r["kind"], r["site"], r["reason"]) for r in reviews] == [("text", "claude.ai", reason)]
    assert b.api(panel, {"type": "redactit/status"})["reviewMode"] == announced
    assert "review" in wait_for(lambda: (t := notice_text(b.context, chat)) and "review" in t and t)
    assert b.api(panel, {"type": "redactit/review-get", "job": reviews[0]["job"]})["text"] == REDACTED
    assert editor.inner_text() == ""  # nothing reaches the page while the review is open
    assert b.api(panel, {"type": "redactit/review-decide", "job": reviews[0]["job"], "approve": approve}) == {"ok": True}
    if approve:
        assert wait_for(lambda: editor.inner_text()) == REDACTED
    else:
        assert wait_for(lambda: b.recent()[0]["code"] == "review_rejected")
        notice = wait_for(lambda: (t := notice_text(b.context, chat)) and "rejected" in t and t)
        assert "Nothing was sent" in notice and editor.inner_text() == ""
    assert not [x for x in (NAME, EMAIL) if x in page_view(chat)]


@pytest.mark.parametrize(("mode", "announced"), [
    ("review-low-clear", "low_confidence"),  # nothing marked for review
    ("review-off", "off"),  # marked, but the policy reviews nothing
])
def test_a_result_the_policy_does_not_send_to_review_goes_straight_in(setup, needs_registry, mode, announced):
    s = setup(host=mode)
    b = s.browser
    chat = b.open(CHAT)
    b.paste(chat, ".ProseMirror", PASTE)
    assert wait_for(lambda: chat.locator(".ProseMirror").inner_text()) == REDACTED
    panel = b.panel()
    assert b.api(panel, {"type": "redactit/review-list"}) == []
    assert b.api(panel, {"type": "redactit/status"})["reviewMode"] == announced
