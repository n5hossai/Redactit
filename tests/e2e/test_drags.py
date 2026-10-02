"""Drags that begin inside the page are left to it; nothing from outside passes with them
(THREAT_MODEL T5). A page can keep `dragend` from reaching Redactit by removing the drag's
source, so the in-page drag is never simply assumed over: a later drop from the desktop,
of files or of text, is still intercepted.

No host is installed: anything Redactit intercepts is blocked, so the page holding a raw
value means it went past Redactit.
"""

import pytest

pytest.importorskip("playwright")

from browserkit import page_view, wait_for  # noqa: E402

NAME = "Alice Okafor"
SECRET = f"{NAME} 555-0100\n"
CHAT = "https://claude.ai/new"

# A draggable chip whose dragstart removes it, as a page may, so `dragend` goes to a node
# that is no longer in the document; and a spot that accepts no drop.
STUCK_DRAG = """() => {
  const chip = document.createElement('div');
  chip.id = 'chip';
  chip.draggable = true;
  chip.textContent = 'drag me';
  chip.style.cssText = 'width: 120px; height: 30px; background: #ccc';
  document.body.prepend(chip);
  chip.addEventListener('dragstart', (e) => {
    e.dataTransfer.setData('text/plain', 'chip');
    setTimeout(() => chip.remove(), 0);
  });
  const nowhere = document.createElement('div');
  nowhere.id = 'nowhere';
  nowhere.style.cssText = 'width: 200px; height: 60px; background: #eee';
  document.body.append(nowhere);
  window.__dragend = 0;
  document.addEventListener('dragend', () => { window.__dragend += 1; }, true);
}"""


def start_stuck_drag(page):
    """A real in-page drag, released where nothing takes it; its dragend never reaches the page."""
    page.bring_to_front()
    page.evaluate(STUCK_DRAG)
    page.hover("#chip")
    page.mouse.down()
    box = page.locator("#nowhere").bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + 20, steps=5)
    page.mouse.up()
    page.wait_for_timeout(300)
    assert page.evaluate("!document.getElementById('chip') && window.__dragend === 0")


def drop_text(context, page, selector, text):
    """Text dragged in from another application: no dragstart in this page."""
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    cdp = context.new_cdp_session(page)
    data = {"items": [{"mimeType": "text/plain", "data": text}], "dragOperationsMask": 1}
    for kind in ("dragEnter", "dragOver", "drop"):
        cdp.send("Input.dispatchDragEvent", {"type": kind, "x": x, "y": y, "data": data})
    cdp.detach()


def test_a_desktop_file_drop_after_an_unfinished_page_drag_is_intercepted(setup, tmp_path):
    s = setup(host=None)
    page = s.browser.open(CHAT)
    start_stuck_drag(page)
    path = tmp_path / f"{NAME}.txt"
    path.write_bytes(SECRET.encode())
    s.browser.drop_files(page, "#dropzone", [path])
    assert s.browser.wait_recent(1)[0]["code"] == "host_missing"
    page.wait_for_timeout(300)
    assert page.evaluate("window.__attachments.length") == 0
    assert NAME not in page_view(page)


def test_a_text_drop_from_outside_after_an_unfinished_page_drag_is_intercepted(setup):
    s = setup(host=None)
    page = s.browser.open(CHAT)
    start_stuck_drag(page)
    drop_text(s.browser.context, page, "#dropzone", SECRET)
    assert s.browser.wait_recent(1)[0]["code"] == "host_missing"
    page.wait_for_timeout(300)
    assert NAME not in page_view(page)


def test_a_drag_within_the_page_still_reaches_it(setup):
    s = setup(host=None)
    page = s.browser.open(CHAT)
    page.bring_to_front()
    page.evaluate("""() => {
      const chip = document.createElement('div');
      chip.id = 'chip';
      chip.draggable = true;
      chip.textContent = 'drag me';
      document.body.prepend(chip);
      chip.addEventListener('dragstart', (e) => e.dataTransfer.setData('text/plain', 'chip'));
    }""")
    page.drag_and_drop("#chip", "#dropzone")
    drops = wait_for(lambda: [e for e in page.evaluate("window.__seen") if e["type"] == "drop"])
    assert drops[0]["trusted"] and drops[0]["text"] == "chip"
    assert s.browser.recent() == []
