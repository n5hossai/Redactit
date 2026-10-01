"""OCR speed settings that must not cost recall: the gate on the turned passes, the memory
arena and the thread count. RapidOCR's models ship in its wheel, so these need no setup."""

import os

from PIL import Image, ImageDraw, ImageFont
from redactit import ocr
from redactit.cores import physical_cores

FONT = ImageFont.load_default(size=32)
UPRIGHT = ["Name: Priya Okafor", "Email: priya.okafor@northwind.com", "Card: 4111 1111 1111 1111"]


def _tile(text: str, angle: int = 0) -> Image.Image:
    tile = Image.new("RGB", (FONT.getbbox(text)[2] + 20, 56), "white")
    ImageDraw.Draw(tile).text((10, 8), text, font=FONT, fill="black")
    return tile.rotate(angle, expand=True, fillcolor="white")


def _upright_page() -> Image.Image:
    img = Image.new("RGB", (900, 260), "white")
    for i, line in enumerate(UPRIGHT):
        img.paste(_tile(line), (20, 20 + 70 * i))
    return img


def _sideways_page() -> Image.Image:
    img = Image.new("RGB", (900, 900), "white")
    img.paste(_tile("Name: Priya Okafor", 90), (100, 80))
    img.paste(_tile("IBAN GB82 WEST 1234 5698 7654 32", 270), (600, 40))
    return img


def _turns_read(monkeypatch, img: Image.Image, **dial) -> tuple[list[ocr.Line], set[int]]:
    turns, real = set(), ocr._read
    monkeypatch.setattr(ocr, "_read", lambda view, t, scale, **kw: turns.add(t) or real(view, t, scale, **kw))
    return ocr.read_lines(img, **dial), turns


def test_turned_passes_are_skipped_only_when_the_upright_pass_left_nothing_unread():
    assert ocr.skip_turned_passes(confident=3, unread=0, dial=3)
    assert ocr.skip_turned_passes(confident=1, unread=0, dial=4)
    assert not ocr.skip_turned_passes(confident=3, unread=1, dial=3)  # a box read badly or sideways
    assert not ocr.skip_turned_passes(confident=0, unread=0, dial=3)  # no upright box at all
    assert not ocr.skip_turned_passes(confident=0, unread=2, dial=1)  # only sideways boxes
    assert not ocr.skip_turned_passes(confident=3, unread=0, dial=ocr.STRICT_DIAL)


def test_a_sideways_only_page_runs_every_pass_and_is_read(monkeypatch):
    lines, turns = _turns_read(monkeypatch, _sideways_page(), dial=3)
    assert set(ocr.TURNS) <= turns
    assert any("Okafor" in ln.text for ln in lines) and any("5698" in ln.text for ln in lines)


def test_a_fully_confident_upright_page_skips_them_and_reads_the_same(monkeypatch):
    img = _upright_page()
    gated, turns = _turns_read(monkeypatch, img, dial=3)
    assert turns == {0}
    assert [ln.text.split(":")[0] for ln in gated] == ["Name", "Email", "Card"]
    assert gated == ocr.read_lines(img, dial=ocr.STRICT_DIAL)  # the turned passes would have added nothing


def test_the_strictest_dial_runs_every_pass_and_is_the_default(monkeypatch):
    _, strict = _turns_read(monkeypatch, _upright_page(), dial=ocr.STRICT_DIAL)
    _, default = _turns_read(monkeypatch, _upright_page())
    assert set(ocr.TURNS) <= strict and set(ocr.TURNS) <= default


def test_the_sessions_reuse_memory_and_hand_it_back_after_each_image(monkeypatch):
    engine = ocr._engine()
    for session in (engine.text_det.infer.session, engine.text_rec.session.session):
        options = session.get_session_options()
        assert options.enable_cpu_mem_arena and options.intra_op_num_threads == physical_cores()
    trims = []
    monkeypatch.setattr(ocr, "_trim", lambda: trims.append(1))
    ocr.read_lines(Image.new("RGB", (64, 64), "white"))
    assert trims == [1]


def test_physical_cores_is_a_core_count_on_every_supported_system():
    cores = physical_cores()
    assert 1 <= cores <= os.cpu_count()


def test_large_views_are_read_without_the_memory_pool(monkeypatch):
    """A 12 MP photo read through the pool peaked at 5.7 GB; past POOL_MAX_PIXELS it is not."""
    engine = ocr._engine()
    (det_holder, det_pooled, det_plain), _ = engine.sessions
    active = []
    monkeypatch.setattr(ocr, "_read", lambda view, turns, scale, **kw: active.append(det_holder.session) or [])
    ocr._read_view(Image.new("RGB", (4000, 3000)), dial=3)  # 12 MP
    large = len(active)
    ocr._read_view(Image.new("RGB", (1920, 1080)), dial=3)
    assert set(map(id, active[:large])) == {id(det_plain)}
    assert set(map(id, active[large:])) == {id(det_pooled)}
