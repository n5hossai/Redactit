"""Pseudonym scopes (PLAN §6, THREAT_MODEL T6): [PERSON_1] means one person within one
chat. A new chat's temporary scope goes to the chat id the site gives that chat, never to
another chat the tab opens, and that link outlives a browser restart.

The scripted host (stubhost.js) records each request's scope, a URL path; these run on
every OS.
"""

import json

import pytest

pytest.importorskip("playwright")

NAME = "Alice Okafor"
PASTE = f"Please call {NAME}."
REPLACE = {NAME: "[PERSON_1]"}
NEW = "https://claude.ai/new"
EXISTING = "https://claude.ai/chat/existing-chat-0001"
BECAME = "https://claude.ai/chat/new-chat-id-0002"  # the id the site gives the new chat


def paste_scope(b, page) -> str:
    """Pastes into the chat and returns the scope the worker asked the host to use."""
    count = len(b.recent())
    b.paste(page, ".ProseMirror", PASTE)
    b.wait_recent(count + 1)
    return [f["scope"] for f in b.stub_frames() if f["type"] == "redact_text"][-1]


def test_a_new_chats_scope_never_goes_to_a_chat_already_in_use(setup):
    s = setup()
    b = s.browser
    b.stub_host("redact", replace=REPLACE)
    assert paste_scope(b, b.open(EXISTING)) == "claude.ai/chat/existing-chat-0001"
    tab = b.open(NEW)
    first = paste_scope(b, tab)
    assert first.startswith("claude.ai/new/")
    tab.goto(EXISTING)  # the user opens an older chat instead of sending
    assert paste_scope(b, tab) == "claude.ai/chat/existing-chat-0001"
    tab.goto(NEW)  # a new chat again: a scope of its own, which its id then keeps
    second = paste_scope(b, tab)
    assert second.startswith("claude.ai/new/") and second != first
    tab.goto(BECAME)
    assert paste_scope(b, tab) == second


def test_a_chat_that_began_as_new_keeps_its_scope_after_a_browser_restart(setup):
    s = setup()
    s.browser.stub_host("redact", replace=REPLACE)
    tab = s.browser.open(NEW)
    temp = paste_scope(s.browser, tab)
    tab.goto(BECAME)
    assert paste_scope(s.browser, tab) == temp
    s.restart_browser()
    b = s.browser
    b.stub_host("redact", replace=REPLACE)
    assert paste_scope(b, b.open(BECAME)) == temp
    stored = json.dumps(b.evaluate("Promise.all([chrome.storage.local.get(null), chrome.storage.session.get(null)])"))
    assert NAME not in stored and "Please" not in stored  # URL paths and ids only
