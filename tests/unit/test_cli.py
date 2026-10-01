"""`redactit redact`'s handling of files and folders, with the engine and each file's
redaction stood in, so no models are needed."""

from types import SimpleNamespace

import pytest
from redactit import cli, models
from redactit.cli import output_name, output_suffixes


@pytest.fixture
def redacted(monkeypatch):
    """Runs of `cli.redact_file`, as (name, site); `actual` overrides an input's output
    names, as an image whose decoded format differs from its suffix does."""
    monkeypatch.setattr(models, "verify", lambda *_args: None)
    monkeypatch.setattr(cli, "open_engine", lambda *_args: SimpleNamespace(warm_images=lambda: None))
    calls = SimpleNamespace(runs=[], actual={})

    def redact_file(name, data, _engine, _scope, *, site=None, destination="cli"):
        calls.runs.append((name, site))
        names = calls.actual.get(name) or [output_name(name, s) for s in output_suffixes(name)]
        return [(out, f"redacted {len(data)} bytes\n") for out in names]

    monkeypatch.setattr(cli, "redact_file", redact_file)
    return calls


def write(folder, files: dict):
    folder.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return [folder / name for name in files]


def test_inputs_sharing_an_output_name_never_overwrite_each_other(tmp_path, redacted, capsys):
    inputs = write(tmp_path / "in", {"notes.docx": b"Word", "notes.docx.md": b"Markdown",
                                     "photo.webp": b"WebP", "photo.webp.png": b"a PNG"})
    out = tmp_path / "out"
    assert cli.main(["redact", *map(str, inputs), "--out", str(out)]) == 0
    assert [name for name, _ in redacted.runs] == ["notes.docx", "photo.webp"]
    assert {p.name: p.read_text(encoding="utf-8") for p in out.iterdir()} == {
        "notes.docx.md": "redacted 4 bytes\n", "photo.webp.png": "redacted 4 bytes\n"}
    err = capsys.readouterr().err
    assert "skipped 2/4: an earlier input has an output of the same name" in err
    assert "skipped 4/4: an earlier input has an output of the same name" in err


def test_an_output_name_known_only_after_redacting_is_checked_too(tmp_path, redacted, capsys):
    inputs = write(tmp_path / "in", {"photo.jpg.png": b"a PNG", "photo.jpg": b"a PNG named .jpg"})
    redacted.actual["photo.jpg"] = ["photo.jpg.png"]  # a PNG keeps PNG: photo.jpg becomes photo.jpg.png
    out = tmp_path / "out"
    assert cli.main(["redact", *map(str, inputs), "--out", str(out)]) == 0
    assert (out / "photo.jpg.png").read_text(encoding="utf-8") == "redacted 5 bytes\n"
    assert "skipped 2/2: an earlier input has an output of the same name" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["notes.txt", "notes.md", "scan.pdf", "photo.jpg"])
def test_an_output_folder_where_an_output_would_replace_its_input_is_refused(tmp_path, redacted, capsys, name):
    (src,) = write(tmp_path / "in", {name: b"original"})
    assert cli.main(["redact", str(src), "--out", str(tmp_path / "in")]) == 1
    assert redacted.runs == [] and src.read_bytes() == b"original"
    assert [p.name for p in (tmp_path / "in").iterdir()] == [name]
    assert capsys.readouterr().err == ("error: an output would overwrite an input; "
                                       "choose an --out folder that does not hold the inputs\n")


def test_an_input_of_another_name_is_not_overwritten_either(tmp_path, redacted):
    """notes.docx writes notes.docx.md, which here is another input."""
    inputs = write(tmp_path / "in", {"notes.docx": b"Word", "notes.docx.md": b"Markdown"})
    assert cli.main(["redact", str(inputs[0]), str(inputs[1]), "--out", str(tmp_path / "in")]) == 1
    assert inputs[1].read_bytes() == b"Markdown"


def test_the_inputs_folder_can_hold_outputs_that_replace_nothing(tmp_path, redacted):
    (src,) = write(tmp_path / "in", {"notes.docx": b"Word"})
    assert cli.main(["redact", str(src), "--out", str(tmp_path / "in")]) == 0
    assert src.read_bytes() == b"Word"
    assert (tmp_path / "in" / "notes.docx.md").read_text(encoding="utf-8") == "redacted 4 bytes\n"
