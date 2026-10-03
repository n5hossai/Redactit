"""The side panel, in Chromium with the real service worker: the face drop zone, getting the
redacted copy out, Redact & copy, status and settings, the review queue and the
re-mapping view (PLAN §7, THREAT_MODEL T6).

Most tests put the scripted host in stubhost.js behind the worker's connectNative, so the
worker's own framing, holds, review and checks run on every OS with no host registered.
The last test drops a PDF through the real host; like test_round_trip.py, it needs the
models, and on Windows REDACTIT_E2E_REGISTRY=1.

Synthetic values only.
"""

import base64
import io
import json

import pytest

pytest.importorskip("playwright")

import browserkit  # noqa: E402
from browserkit import blob_bytes, page_view, wait_for  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

NAME, EMAIL, CARD = "Priya Okafor", "priya.okafor@northwind.com", "4111 1111 1111 1111"
SECRETS = [NAME, "Okafor", EMAIL, CARD, CARD.replace(" ", "")]
PASTE = f"Please email {NAME} at {EMAIL}; she paid with card {CARD}."
REPLACE = {NAME: "[PERSON_1]", EMAIL: "[EMAIL_1]", CARD: "[CREDIT_CARD_1]"}
REDACTED = "Please email [PERSON_1] at [EMAIL_1]; she paid with card [CREDIT_CARD_1]."
LINES = [f"Patient: {NAME}", f"Email: {EMAIL}", f"Card: {CARD}"]
CHAT = "https://claude.ai/new"
DROP_TARGET = "https://drop-target.test/"


def assert_no_raw(text: str) -> None:
    assert not [s for s in SECRETS if s in text]


def picture(lines: list[str], fill: str = "black") -> Image.Image:
    img = Image.new("RGB", (900, 300), (245, 245, 245))
    draw, font = ImageDraw.Draw(img), ImageFont.load_default(size=32)
    for i, line in enumerate(lines):
        draw.text((40, 40 + 80 * i), line, fill=fill, font=font)
    return img


def encoded(images: list[Image.Image], fmt: str) -> bytes:
    out = io.BytesIO()
    images[0].save(out, fmt, save_all=fmt == "PDF", append_images=images[1:])
    return out.getvalue()


ORIGINAL = {"pdf": encoded([picture(LINES), picture(LINES[1:])], "PDF"), "png": encoded([picture(LINES)], "PNG")}
# What the scripted host hands back: bars where the values were.
COVERED = [line.split(":")[0] + ": " + "█" * 12 for line in LINES]
STUB_OUT = {"pdf": encoded([picture(COVERED)], "PDF"), "png": encoded([picture(COVERED)], "PNG")}
MARKDOWN = "## Page 1\n\nPatient: [PERSON_1]\n"


def stub(s, mode="redact", **options) -> None:
    files = {"pdf": base64.b64encode(STUB_OUT["pdf"]).decode(), "image": base64.b64encode(STUB_OUT["png"]).decode()}
    s.browser.stub_host(mode, replace=REPLACE, files=files, markdown=MARKDOWN, **options)


def open_panel(s, chat: bool = True):
    """A chat tab, then the panel (which works for that chat), recording every step the
    panel shows and everything it writes to its console."""
    page = s.browser.open(CHAT) if chat else None
    panel = s.browser.panel()
    panel.console = []
    panel.on("console", lambda m: panel.console.append(m.text))
    panel.evaluate("""() => {
      const watch = (id, into) => {
        const el = document.getElementById(id);
        window[into] = [el.textContent];
        new MutationObserver(() => {
          if (window[into].at(-1) !== el.textContent) window[into].push(el.textContent);
        }).observe(el, {childList: true, characterData: true, subtree: true});
      };
      watch('stepText', '__steps');
      watch('hostPill', '__pills');
    }""")
    return page, panel


def steps(panel) -> list[str]:
    return panel.evaluate("window.__steps")


def step(panel) -> str:
    return panel.inner_text("#stepText")


def drop(s, panel, tmp_path, kind: str, name: str = "Priya Okafor statement"):
    path = tmp_path / f"{name}.{kind}"
    path.write_bytes(ORIGINAL[kind])
    s.browser.drop_files(panel, "#stage", [path])
    return path


def intercept_drag(context, page, selector: str) -> dict:
    """What a drag from `selector` carries, as Chromium hands it to wherever it is dropped:
    the drag is started with real mouse input and caught by DevTools before it leaves."""
    page.bring_to_front()
    cdp = context.new_cdp_session(page)
    got = []
    cdp.on("Input.dragIntercepted", lambda e: got.append(e["data"]))
    cdp.send("Input.setInterceptDrags", {"enabled": True})
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    for i in range(1, 8):
        cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x + 8 * i, "y": y + 8 * i,
                                              "button": "left", "buttons": 1})
        page.wait_for_timeout(50)  # lets the intercepted event arrive
        if got:
            break
    if got:  # ends the page's drag, so the next one can start
        cdp.send("Input.dispatchDragEvent", {"type": "dragCancel", "x": x, "y": y, "data": got[0]})
    cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left"})
    cdp.send("Input.setInterceptDrags", {"enabled": False})
    cdp.detach()
    assert got, f"no drag started from {selector}"
    return got[0]


def deliver_drag(context, page, selector: str, data: dict) -> None:
    box = page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    cdp = context.new_cdp_session(page)
    for kind in ("dragEnter", "dragOver", "drop"):
        cdp.send("Input.dispatchDragEvent", {"type": kind, "x": x, "y": y, "data": data})
    cdp.detach()


# --- the face drop zone ---------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["pdf", "png"])
def test_a_dropped_file_shows_real_progress_then_the_redacted_copy(setup, tmp_path, kind):
    s = setup()
    stub(s, warmMs=600, pages=3, pageMs=250)
    chat, panel = open_panel(s)
    drop(s, panel, tmp_path, kind)
    wait_for(lambda: step(panel).startswith("Redacted in"), timeout=30)

    seen = steps(panel)
    assert any("starting" in t for t in seen)  # held while the host warmed up
    pages = [t for t in seen if t.startswith("Redacting page")]
    if kind == "pdf":
        assert pages == ["Redacting page 1 of 3", "Redacting page 2 of 3", "Redacting page 3 of 3"]
    else:
        assert not pages and "Reading the image and covering what it finds" in seen
    assert panel.get_attribute("#progress", "aria-valuenow") == "100"
    assert "done" in panel.get_attribute("#stage", "class")
    assert panel.text_content("#numOff") == "100"
    assert panel.inner_text("#fileName") == f"Priya Okafor statement.{kind}"
    worker_kind = "image" if kind == "png" else kind
    assert s.browser.recent()[0] == {"kind": worker_kind, "site": "claude.ai", "held": True, "code": "delivered"}

    # The left folder holds the copy, under a neutral name, ready to download.
    assert panel.inner_text("#chipName") == f"redacted-1.{kind}"
    assert panel.get_attribute("#dragOut", "draggable") == "true"
    with panel.expect_download() as download:
        panel.click("#downloadLink")
    assert download.value.suggested_filename == f"redacted-1.{kind}"
    with open(download.value.path(), "rb") as f:
        assert f.read() == STUB_OUT[kind]
    assert panel.is_visible("#copyResultBtn") is (kind == "pdf")  # a PDF's page text can be copied
    assert_no_raw(page_view(chat))  # the chat saw nothing of it
    assert_no_raw(" ".join(panel.console))


def test_getting_the_copy_out_by_attach_and_by_drag(setup, tmp_path):
    """Attach puts the worker's checked copy into the chat, once; a second try is told the
    copy has gone. Dragging was tested as an experiment: Chromium does not hand a File made
    in one page to another; a File added in dragstart arrives at a page's drop target only
    as its name, in text/plain (crbug.com/394955). If that changes, this test fails and
    the panel's drag can carry the file itself. Until then the drag carries DownloadURL
    only: dropped on the desktop it saves the copy, and a chat gets nothing to paste."""
    s = setup()
    stub(s)
    chat, panel = open_panel(s)
    b = s.browser

    def target_page(route):
        route.fulfill(status=200, content_type="text/html", body="""<!doctype html>
            <div id=zone style="width:300px;height:200px">drop here</div><script>
            window.__got = [];
            const zone = document.getElementById('zone');
            zone.addEventListener('dragover', (e) => e.preventDefault());
            zone.addEventListener('drop', (e) => { e.preventDefault(); window.__got.push({
              types: [...e.dataTransfer.types], text: e.dataTransfer.getData('text/plain'),
              files: [...e.dataTransfer.files].map((f) => f.name)}); });
            </script>""")

    b.context.route(f"{DROP_TARGET}**", target_page)
    target = b.open(DROP_TARGET)

    # The experiment: a File added to the drag in an extension page's dragstart.
    panel.evaluate("""() => {
      const probe = document.createElement('div');
      probe.id = 'probe';
      probe.draggable = true;
      probe.style.cssText = 'position:fixed;left:0;top:0;width:120px;height:80px;z-index:9';
      probe.addEventListener('dragstart', (e) => {
        e.dataTransfer.items.add(new File([new Uint8Array([1, 2, 3])], 'redacted-9.png', {type: 'image/png'}));
      });
      document.body.append(probe);
    }""")
    carried = intercept_drag(b.context, panel, "#probe")
    assert carried["items"] == [{"mimeType": "text/plain", "data": "redacted-9.png"}]
    assert not carried.get("files") and not carried.get("filenames")
    deliver_drag(b.context, target, "#zone", carried)
    assert wait_for(lambda: target.evaluate("window.__got")) == [
        {"types": ["text/plain"], "text": "redacted-9.png", "files": []}]
    panel.evaluate("document.getElementById('probe').remove()")

    # The panel's own drag: DownloadURL only, pointing at the copy's blob in the extension.
    drop(s, panel, tmp_path, "png")
    wait_for(lambda: step(panel).startswith("Redacted in"))
    for source in ("#chip", "#dragOut"):
        carried = intercept_drag(b.context, panel, source)
        assert [i["mimeType"] for i in carried["items"]] == ["downloadurl"]
        assert carried["items"][0]["data"].startswith(f"image/png:redacted-1.png:blob:{browserkit.ORIGIN}")
        assert not carried.get("files")
    deliver_drag(b.context, chat, "#dropzone", carried)  # what a drop on the chat would get

    # Attach: the worker hands the chat the copy it checked, under a neutral name.
    panel.bring_to_front()
    panel.click("#attachBtn")
    wait_for(lambda: "Attached to the chat" in panel.inner_text("#resultNote"))
    wait_for(lambda: chat.evaluate("window.__attachments.length") == 1)
    assert chat.evaluate("(f => [f.name, f.type])(window.__attachments[0])") == ["redacted-1.png", "image/png"]
    assert base64.b64decode(chat.evaluate("window.__read(0)")) == STUB_OUT["png"]
    panel.focus("#dragOut")
    panel.keyboard.press("Enter")  # the left folder attaches from the keyboard too; the copy has gone
    wait_for(lambda: "no longer available" in panel.inner_text("#resultNote"))
    assert chat.evaluate("window.__attachments.length") == 1
    seen = chat.evaluate("window.__seen")
    assert not [e for e in seen if e["type"] == "paste"]
    assert all(not e["trusted"] for e in seen if e["type"] == "change")  # only the attach's own event
    # The DownloadURL drag dropped on the chat arrives empty: no text, no markup, no file.
    # The attach's own drop on the composer carries the redacted copy and nothing else.
    for e in (e for e in seen if e["type"] == "drop"):
        assert e["text"] == e["html"] == ""
        assert e["files"] == ([] if e["trusted"] else [{"name": "redacted-1.png", "size": len(STUB_OUT["png"]),
                                                       "type": "image/png"}])
    assert_no_raw(page_view(chat))


def test_redact_and_copy_copies_only_the_redacted_text(setup):
    s = setup()
    stub(s)
    chat, panel = open_panel(s)
    panel.click("#textSection summary")
    panel.fill("#textIn", PASTE)
    panel.click("#redactCopyBtn")
    wait_for(lambda: "copied" in panel.inner_text("#textStatus"))
    assert panel.input_value("#textOut") == REDACTED
    assert s.browser.clipboard() == REDACTED
    assert s.browser.recent()[0] == {"kind": "text", "site": "claude.ai", "held": True, "code": "delivered"}
    assert_no_raw(page_view(chat))
    assert_no_raw(" ".join(panel.console))


def test_status_follows_the_host_and_the_settings(setup):
    s = setup()
    chat, panel = open_panel(s)
    b = s.browser
    assert wait_for(lambda: panel.inner_text("#hostPill") == "Starts when needed")
    assert panel.inner_text("#target") == "Working for claude.ai, the chat in view."
    panel.click("#settingsSection summary")
    assert panel.inner_text("#reviewModeValue") == "Not known yet"  # no policy until the host loads one

    stub(s, warmMs=800, reviewMode="low_confidence")
    b.api(panel, {"type": "redactit/start-host"})
    wait_for(lambda: panel.inner_text("#hostPill") == "Ready")
    assert "Warming up" in panel.evaluate("window.__pills")
    assert panel.get_attribute("#hostPill", "role") == "status"
    assert panel.inner_text("#reviewModeValue") == "When Redactit is unsure"
    assert "unsure about wait here" in panel.inner_text("#reviewModeLine")
    # The review mode is the admin's policy: shown, with nothing in the panel to change it.
    assert panel.locator("#settingsSection input").evaluate_all("els => els.map(e => e.id)") == ["keepReady"]

    panel.check("#keepReady")
    assert wait_for(lambda: b.evaluate("chrome.storage.local.get('keepReady')")).get("keepReady") is True

    chat.close()
    wait_for(lambda: "Open claude.ai" in panel.inner_text("#target"))


@pytest.mark.parametrize(("mode", "marked", "reason"), [
    ("always", 0, "Held because review is on for every paste and file."),
    ("low_confidence", 2, "Held because Redactit was unsure about part of it."),
])
def test_a_held_paste_is_approved_or_cancelled_in_the_review_queue(setup, mode, marked, reason):
    s = setup()
    stub(s, reviewMode=mode, reviewCount=marked)  # the host's policy, and its decisions marked for review
    b = s.browser
    chat, panel = open_panel(s)
    editor = chat.locator(".ProseMirror")
    item = panel.locator("#reviewList .review")
    for approve in (True, False):
        chat.evaluate("document.querySelector('.ProseMirror').textContent = ''")
        b.paste(chat, ".ProseMirror", PASTE)
        wait_for(lambda: item.count() == 1)
        assert panel.inner_text("#reviewCount") == "1"
        assert item.locator(".review-what").inner_text() == "Paste on claude.ai"
        assert item.locator(".review-reason").inner_text() == reason
        assert wait_for(lambda: item.locator(".preview-text").input_value()) == REDACTED
        assert editor.inner_text() == ""  # nothing reaches the page until the review ends
        panel.bring_to_front()
        item.locator(".review-approve" if approve else ".review-cancel").click()
        wait_for(lambda: item.count() == 0)
        if approve:
            assert wait_for(lambda: editor.inner_text()) == REDACTED
        else:
            assert wait_for(lambda: b.recent()[0]["code"] == "review_rejected")
            assert editor.inner_text() == ""
    assert panel.is_visible("#reviewEmpty")
    assert_no_raw(page_view(chat))


@pytest.mark.parametrize("kind", ["png", "pdf"])
def test_a_held_file_is_shown_as_the_very_file_approve_sends(setup, tmp_path, kind):
    """The reviewer sees the redacted image itself, or the redacted PDF itself beside its page
    text, never only a name; Approve is enabled once it is shown, and what went in is that."""
    s = setup()
    stub(s, reviewMode="always")
    b = s.browser
    chat, panel = open_panel(s)
    path = tmp_path / f"Priya Okafor scan.{kind}"
    path.write_bytes(ORIGINAL[kind])
    b.drop_files(chat, "#dropzone", [path])  # a desktop drop on the chat, held by the policy
    item = panel.locator("#reviewList .review")
    wait_for(lambda: item.count() == 1)
    approve = item.locator(".review-approve")
    wait_for(lambda: approve.is_enabled())
    if kind == "png":
        assert item.locator(".preview-image").is_visible()
        url = item.locator(".preview-image").get_attribute("src")
    else:
        assert item.locator(".preview-file").is_visible()
        url = item.locator(".preview-file").get_attribute("href")
        assert item.locator(".preview-text").input_value() == MARKDOWN
    assert url.startswith(f"blob:{browserkit.ORIGIN}")
    assert blob_bytes(panel, url) == STUB_OUT[kind]
    assert chat.evaluate("window.__attachments.length") == 0
    panel.bring_to_front()
    approve.click()
    wait_for(lambda: chat.evaluate("window.__attachments.length") == 1)
    assert base64.b64decode(chat.evaluate("window.__read(0)")) == STUB_OUT[kind]
    assert_no_raw(page_view(chat))


def test_remapping_is_not_available_when_refused_and_real_values_stay_in_the_panel(setup):
    s = setup()  # no host at first: the worker refuses the remap with its reason
    b = s.browser
    chat, panel = open_panel(s)
    reply = "Thanks. I will write to [PERSON_1] at [EMAIL_1] today."
    panel.click("#remapSection summary")
    panel.fill("#remapIn", reply)
    panel.click("#remapBtn")
    wait_for(lambda: "not available" in panel.inner_text("#remapStatus"))
    assert "not installed" in panel.inner_text("#remapStatus")
    assert panel.is_hidden("#remapResult")

    # With a host, the worker re-maps for the chat in view: the panel shows the real
    # values, and copies them only on an explicit click.
    stub(s)
    b.copy("nothing copied yet")
    panel.bring_to_front()
    panel.click("#remapBtn")
    wait_for(lambda: panel.is_visible("#remapResult"))
    assert panel.inner_text("#remapOut") == f"Thanks. I will write to {NAME} at {EMAIL} today."
    assert [f["type"] for f in b.stub_frames()][:1] == ["remap"]
    assert b.recent()[0] == {"kind": "remap", "site": "claude.ai", "held": True, "code": "delivered"}
    assert "contains real data" in panel.inner_text("#remapWarn")
    assert b.clipboard() == "nothing copied yet"  # shown, not copied
    panel.bring_to_front()
    panel.click("#remapCopyBtn")
    wait_for(lambda: "Copied" in panel.inner_text("#remapCopyLabel"))
    assert NAME in b.clipboard()

    # Real values never reach the page, the extension's storage or the console.
    assert_no_raw(page_view(chat))
    assert_no_raw(json.dumps(b.evaluate(
        "Promise.all([chrome.storage.local.get(null), chrome.storage.session.get(null)])")))
    assert_no_raw(" ".join(panel.console))
    panel.click("#remapClearBtn")
    assert panel.is_hidden("#remapResult") and panel.inner_text("#remapOut") == ""


def test_real_values_are_cleared_when_the_tab_moves_to_another_chat(setup):
    """Real values belong to the chat they were mapped for; the same tab showing another
    chat clears them, as another tab would."""
    s = setup()
    stub(s)
    b = s.browser
    chat = b.open("https://claude.ai/chat/first-chat-0001")
    panel = b.panel()
    panel.click("#remapSection summary")
    panel.fill("#remapIn", "I will write to [PERSON_1] today.")
    panel.click("#remapBtn")
    wait_for(lambda: panel.is_visible("#remapResult"))
    assert NAME in panel.inner_text("#remapOut")
    chat.goto("https://claude.ai/chat/second-chat-0002")
    wait_for(lambda: panel.is_hidden("#remapResult"))
    assert panel.inner_text("#remapOut") == ""
    assert "chat in view changed" in panel.inner_text("#remapStatus")


def test_the_panel_fits_side_panel_widths_and_works_from_the_keyboard(setup):
    s = setup()
    _, panel = open_panel(s)
    for width in (320, 500):
        panel.set_viewport_size({"width": width, "height": 900})
        for section in ("#textSection", "#remapSection", "#settingsSection", "#keySection"):
            panel.evaluate(f"document.querySelector('{section}').open = true")
        assert panel.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
        face = panel.locator(".face-wrap").bounding_box()
        assert face["width"] >= 240 and face["x"] >= 0 and face["x"] + face["width"] <= width

    # The drop zone's right folder is a button: Enter opens the file picker.
    panel.focus("#dropBtn")
    assert panel.evaluate("document.activeElement.matches(':focus-visible')")
    with panel.expect_file_chooser():
        panel.keyboard.press("Enter")
    # Sections open from the keyboard, and the empty left folder is not a tab stop.
    assert panel.get_attribute("#dragOut", "tabindex") == "-1"
    panel.evaluate("document.querySelector('#textSection').open = false")
    panel.focus("#textSection summary")
    panel.keyboard.press("Enter")
    assert panel.evaluate("document.querySelector('#textSection').open")
    assert panel.get_attribute("#announce", "aria-live") == "polite"

    # Reduced motion: the divider's dots stand still.
    panel.emulate_media(reduced_motion="reduce")
    panel.evaluate("document.getElementById('stage').classList.add('running')")
    assert panel.evaluate("getComputedStyle(document.querySelector('.flow')).animationName") == "none"


def test_cancel_stops_the_file_here_and_at_the_host(setup, tmp_path):
    s = setup()
    stub(s, "hang")
    _, panel = open_panel(s)
    drop(s, panel, tmp_path, "png")
    wait_for(lambda: step(panel) == "Reading the image and covering what it finds")
    assert panel.get_attribute("#dropBtn", "aria-disabled") == "true"
    panel.click("#cancelBtn")
    wait_for(lambda: step(panel) == "Cancelled. Nothing was kept.")
    assert s.browser.recent()[0]["code"] == "cancelled"
    job = next(f["id"] for f in s.browser.stub_frames() if f["type"] == "redact_file")
    wait_for(lambda: {"type": "cancel", "id": job} in s.browser.stub_frames())
    assert panel.is_hidden("#runActions") and panel.is_hidden("#result")
    assert panel.get_attribute("#dropBtn", "aria-disabled") == "false"


def test_a_drop_is_blocked_with_the_reason_when_there_is_no_chat_or_no_host(setup, tmp_path):
    s = setup()  # no host registered and no stub: Chrome cannot find the host
    _, panel = open_panel(s, chat=False)
    drop(s, panel, tmp_path, "png")
    wait_for(lambda: "Open a chat on claude.ai" in step(panel))
    chat = s.browser.open(CHAT)
    drop(s, panel, tmp_path, "png")
    wait_for(lambda: "not installed" in step(panel))
    assert step(panel).startswith("Blocked.")
    assert "err" in panel.get_attribute("#step", "class")
    assert "blocked" in panel.get_attribute("#stage", "class")
    assert panel.is_hidden("#result")
    assert s.browser.recent()[0]["code"] == "host_missing"
    assert wait_for(lambda: panel.inner_text("#hostPill") == "Not installed")
    assert_no_raw(page_view(chat))


# --- the real host ----------------------------------------------------------------------------

@pytest.fixture
def models_installed():
    from redactit import models

    try:
        models.path_for("gliner/model.onnx")
        models.path_for("yunet/face_detection_yunet_2023mar.onnx")
    except models.ModelError:
        browserkit.unavailable("models not installed; run `redactit setup-models`")


def test_the_real_host_redacts_a_pdf_dropped_on_the_panel(setup, needs_registry, models_installed, tmp_path):
    s = setup(host="real")
    chat, panel = open_panel(s)
    original = drop(s, panel, tmp_path, "pdf").read_bytes()
    wait_for(lambda: step(panel).startswith("Redacted in") or "Blocked" in step(panel), timeout=300)
    assert step(panel).startswith("Redacted in"), step(panel)
    assert [t for t in steps(panel) if t.startswith("Redacting page")] == [
        "Redacting page 1 of 2", "Redacting page 2 of 2"]
    with panel.expect_download() as download:
        panel.click("#downloadLink")
    with open(download.value.path(), "rb") as f:
        data = f.read()
    assert data.startswith(b"%PDF") and data != original
    panel.click("#copyResultBtn")
    wait_for(lambda: panel.inner_text("#copyResultLabel") == "Copied")
    text = s.browser.clipboard()
    assert "[" in text  # the page text, with pseudonyms in place of the values
    assert_no_raw(text)
    assert_no_raw(page_view(chat))
