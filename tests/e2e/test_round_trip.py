"""Round trips through the real host: what the user pastes, drops or picks reaches the
page redacted, and the raw values never reach the page's DOM or its listeners
(THREAT_MODEL T5), including when the site adapter has turned itself off.

One browser and one host serve every test, in file order: the first paste is the one
that starts the host, so it is the one held while the host warms up.
"""

import base64
import io
import json
import time

import pytest

pytest.importorskip("playwright")  # before hashing the models below: most runs stop here

import browserkit  # noqa: E402
from redactit import models  # noqa: E402

try:
    models.path_for("gliner/model.onnx")
    models.path_for("yunet/face_detection_yunet_2023mar.onnx")
except models.ModelError:
    browserkit.unavailable("models not installed; run `redactit setup-models`", module=True)

import numpy as np  # noqa: E402
from browserkit import notice_text, page_view, wait_for  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from redactit.hosts import native  # noqa: E402

# Synthetic values only.
NAME, EMAIL, CARD = "Priya Okafor", "priya.okafor@northwind.com", "4111 1111 1111 1111"
SECRETS = [NAME, "Okafor", EMAIL, CARD, CARD.replace(" ", "")]
PASTE = f"Please email {NAME} at {EMAIL}; she paid with card {CARD}."
LINES = [f"Patient: {NAME}", f"Email: {EMAIL}", f"Card: {CARD}"]


@pytest.fixture(scope="module")
def env(playwright, tmp_path_factory):
    if not browserkit.registry_allowed():
        browserkit.unavailable(browserkit.REGISTRY_REASON)
    s = browserkit.Setup(playwright, tmp_path_factory.mktemp("round-trip"), host="real", adapter_check_ms=1500)
    yield s
    s.close()


@pytest.fixture(scope="module")
def chat(env):
    return env.browser.open("https://claude.ai/new")


def assert_no_raw(text: str) -> None:
    assert not [s for s in SECRETS if s in text]


def seen(page, type_: str) -> list[dict]:
    return [e for e in page.evaluate("window.__seen") if e["type"] == type_]


def attachment(page, i: int) -> tuple[dict, bytes]:
    meta = page.evaluate(f"(f => ({{name: f.name, size: f.size, type: f.type}}))(window.__attachments[{i}])")
    return meta, base64.b64decode(page.evaluate(f"window.__read({i})"))


def png_with_values(width: int, height: int, noise: float = 0.0, seed: int = 1) -> bytes:
    rng = np.random.default_rng(seed)
    pixels = 235 + rng.normal(0, noise, (height, width, 3)) if noise else np.full((height, width, 3), 245.0)
    img = Image.fromarray(pixels.clip(0, 255).astype("uint8"))
    draw, font = ImageDraw.Draw(img), ImageFont.load_default(size=36)
    for i, line in enumerate(LINES):
        draw.rectangle((40, 40 + 80 * i, 1000, 100 + 80 * i), fill="white")
        draw.text((50, 50 + 80 * i), line, fill="black", font=font)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def test_a_paste_during_warm_up_is_held_then_delivered_redacted(env, chat):
    b = env.browser
    b.paste(chat, ".ProseMirror", PASTE)  # the host starts now
    assert "starting" in wait_for(lambda: (t := notice_text(b.context, chat)) and "starting" in t and t, timeout=30)
    time.sleep(1)  # past the generic "checking" notice's delay: the specific one must stay
    if not chat.locator(".ProseMirror").inner_text():
        assert "starting" in notice_text(b.context, chat)
    text = wait_for(lambda: chat.locator(".ProseMirror").inner_text(), timeout=180)
    assert_no_raw(text)
    assert text.startswith("Please email ") and "[" in text  # redacted, and the rest intact
    assert b.recent()[0] == {"kind": "text", "site": "claude.ai", "held": True, "code": "delivered"}
    pastes = seen(chat, "paste")  # recorded by the page at window capture and document bubble
    assert pastes and all(not p["trusted"] and p["text"] == text and p["html"] == "" for p in pastes)
    assert_no_raw(page_view(chat))


def test_a_paste_into_a_plain_textarea_is_inserted_redacted(env, chat):
    """The stand-in's textarea has no paste handler, so the synthetic paste goes
    unhandled and the extension inserts the text itself."""
    before = len(env.browser.recent())
    env.browser.paste(chat, "#plain", PASTE)
    value = wait_for(lambda: chat.input_value("#plain"), timeout=120)
    assert_no_raw(value)
    assert value.startswith("Please email ")
    assert env.browser.wait_recent(before + 1)[0]["held"] is False  # the host was warm
    inputs = [e for e in seen(chat, "input") if e.get("value") == value]
    assert inputs and all(not e["trusted"] for e in inputs)
    assert_no_raw(page_view(chat))


def test_a_dropped_png_reaches_the_page_as_the_redacted_file(env, chat, tmp_path):
    original = png_with_values(1100, 320)
    path = tmp_path / "Priya Okafor scan.png"  # a name that is itself a value
    path.write_bytes(original)
    env.browser.drop_files(chat, "#dropzone", [path])
    wait_for(lambda: chat.evaluate("window.__attachments.length") == 1, timeout=180)
    meta, data = attachment(chat, 0)
    assert meta["name"].startswith("redacted-") and meta["name"].endswith(".png") and meta["type"] == "image/png"
    assert data != original
    before, after = (np.asarray(Image.open(io.BytesIO(d)).convert("RGB"), dtype=int) for d in (original, data))
    assert before.shape == after.shape
    changed = np.abs(before - after).sum(axis=2) > 0
    assert changed[40:300, 40:1000].mean() > 0.05  # the value lines were covered
    assert not changed[:, 1060:].any()  # far from the text, nothing was touched
    drops = seen(chat, "drop")
    assert drops and all(not d["trusted"] and [f["name"] for f in d["files"]] == [meta["name"]] for d in drops)
    assert_no_raw(page_view(chat))


def test_a_large_file_is_chunked_both_ways(env, chat, tmp_path):
    """About 5 MB each way: more than ten of the host's chunks in, and as many back."""
    original = png_with_values(1700, 1300, noise=18, seed=11)  # noise does not compress
    assert native.chunk_count(len(original)) >= 10
    path = tmp_path / "large.png"
    path.write_bytes(original)
    chat.set_input_files("#upload", str(path))
    wait_for(lambda: chat.evaluate("window.__attachments.length") == 2, timeout=300)
    meta, data = attachment(chat, 1)
    assert meta["type"] == "image/png" and native.chunk_count(len(data)) >= 10
    image = Image.open(io.BytesIO(data))
    image.load()  # every PNG chunk's CRC and the zlib checksum: a lost or reordered chunk fails here
    assert image.size == (1700, 1300)
    changes = [e for e in seen(chat, "change") if e.get("files")]
    assert changes and all(not e["trusted"] and [f["name"] for f in e["files"]] == [meta["name"]] for e in changes)


def test_an_adapter_whose_selectors_are_missing_falls_back_and_still_redacts(env, chat):
    b = env.browser
    other = b.open("https://chatgpt.com/")  # the stand-in has none of the chatgpt.com selectors

    def adapter_of(url):
        return b.evaluate(f"chrome.tabs.query({{url: '{url}'}}).then(([t]) => pageAdapters.get(t.id) || null)")

    assert wait_for(lambda: (a := adapter_of("https://chatgpt.com/*")) and a["active"] is not None and a,
                    timeout=30) == {"name": "chatgpt", "active": False}
    assert adapter_of("https://claude.ai/*") == {"name": "claude", "active": True}
    b.paste(other, "#plain", PASTE)
    value = wait_for(lambda: other.input_value("#plain"), timeout=120)
    assert_no_raw(value)
    assert value.startswith("Please email ")
    assert_no_raw(page_view(other))


def test_the_panel_redacts_text_and_sees_the_hosts_review_mode(env, chat):
    b = env.browser
    panel = b.panel()
    tab = b.evaluate("chrome.tabs.query({url: 'https://claude.ai/*'}).then(([t]) => t.id)")
    out = b.api(panel, {"type": "redactit/redact-text", "text": PASTE, "tabId": tab})
    assert out["ok"] is True and out["text"] == chat.locator(".ProseMirror").inner_text()  # one scope per tab
    assert_no_raw(out["text"])
    status = b.api(panel, {"type": "redactit/status"})
    assert status["reviewMode"] in ("always", "low_confidence") and "reviewMode" not in status["settings"]


def test_with_the_guard_in_place_a_normal_paste_still_arrives_redacted(env, chat):
    """The main-world guard (tests/e2e/test_guard.py) refuses the page's own clipboard
    reads; a paste, the path the guard steers sites to, still arrives redacted."""
    b = env.browser
    b.context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://claude.ai")
    chat.bring_to_front()
    assert chat.evaluate(browserkit.GUARD_PROBE) == browserkit.GUARD_REFUSED
    chat.evaluate("document.getElementById('plain').value = ''")
    b.paste(chat, "#plain", PASTE)
    value = wait_for(lambda: chat.input_value("#plain"), timeout=120)
    assert value.startswith("Please email ")
    assert_no_raw(value)
    assert_no_raw(page_view(chat))


def test_nothing_raw_in_extension_storage_or_the_hosts_logs(env):
    stored = env.browser.evaluate(
        "Promise.all([chrome.storage.local.get(null), chrome.storage.session.get(null)])")
    assert_no_raw(json.dumps(stored))
    assert_no_raw(json.dumps(env.browser.recent()))
    reg = env.registration
    stderr = reg.stderr_file.read_text(encoding="utf-8", errors="replace") if reg.stderr_file.exists() else ""
    audit = (reg.data_dir / "audit.jsonl").read_text(encoding="utf-8")
    assert "redaction" in audit
    assert_no_raw(stderr + audit)
