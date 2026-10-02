"""The folder watcher end to end with the real engine: synthetic Word, PDF, image and text
files dropped into an inbox come out redacted in the outbox under `redactit redact`'s
names, with no seeded value left, the originals untouched and no temp folder behind.
Skipped until models are installed."""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
from redactit import models

try:
    models.path_for("gliner/model.onnx")
except models.ModelError:
    pytest.skip("models not installed; run `redactit setup-models`", allow_module_level=True)

import corpus_docx  # noqa: E402
import corpus_media  # noqa: E402
import generate as gen  # noqa: E402
import run as leak  # noqa: E402
import yaml  # noqa: E402
from redactit.audit import AuditLog  # noqa: E402
from redactit.cli import redact_file  # noqa: E402
from redactit.copies import Copies  # noqa: E402
from redactit.hosts.watcher import TEMP_PREFIX, Watcher  # noqa: E402
from redactit.pipeline import Engine  # noqa: E402
from redactit.policy import DEFAULT_POLICY, load_policy  # noqa: E402
from redactit.vault import Vault  # noqa: E402

TESTS = Path(__file__).resolve().parent


def wait_for(condition, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.1)


def temp_folders(outbox: Path) -> list[Path]:
    return [p for p in outbox.iterdir() if p.name.startswith(TEMP_PREFIX)]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory) -> Path:
    """One file per interface format, with its seeded values in a leak-test manifest."""
    root = tmp_path_factory.mktemp("corpus")
    vf = gen.ValueFactory(7)
    builders = {
        "notes.txt": ("txt", lambda p: gen.build_txt(vf, p)),
        "letter.docx": ("docx", lambda p: corpus_docx.build_docx("comments", vf, p)),
        "scan.pdf": ("pdf", lambda p: corpus_media.build_pdf("digital", vf, p)),
        "photo.png": ("png", lambda p: corpus_media.build_image("png", vf, p)),
    }
    docs = [{"file": name, "format": fmt, "seeded": build(root / name)} for name, (fmt, build) in builders.items()]
    (root / "manifest.json").write_text(json.dumps({"documents": docs}), encoding="utf-8")
    (root / "company_terms.txt").write_text("\n".join(vf.used_terms) + "\n", encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def engine(corpus, tmp_path_factory):
    """The default policy at the admin floor plus the corpus's company terms, as the leak test runs it."""
    tmp = tmp_path_factory.mktemp("engine")
    policy = yaml.safe_load(DEFAULT_POLICY.read_text(encoding="utf-8"))
    policy["custom_terms"]["files"] = [str(corpus / "company_terms.txt")]
    (tmp / "policy.yaml").write_text(yaml.safe_dump(policy), encoding="utf-8")
    with Vault(tmp / "vault.db", os.urandom(32)) as vault:  # a throwaway key, never the real keychain
        yield Engine(load_policy(tmp / "policy.yaml"), vault, AuditLog(tmp / "audit.jsonl")), tmp / "audit.jsonl"


def test_dropped_files_come_out_redacted(corpus, engine, tmp_path):
    eng, audit = engine
    inbox, outbox = tmp_path / "inbox", tmp_path / "outbox"
    inbox.mkdir()
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    for doc in manifest["documents"]:
        shutil.copy2(corpus / doc["file"], inbox / doc["file"])
    originals = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in inbox.iterdir()}

    log, stop = [], threading.Event()
    watch = Watcher(inbox, outbox, copies=Copies(tmp_path / "data" / "copies.json"), log=log.append)
    convert = lambda name, data: redact_file(name, data, eng, uuid.uuid4().hex, destination="outbox")  # noqa: E731
    thread = threading.Thread(target=watch.run, args=(convert, stop), daemon=True)
    thread.start()
    try:
        wait_for(lambda: len(log) >= 5 or not thread.is_alive(), timeout=900)  # "watching", then one line per file
    finally:
        stop.set()
        thread.join(60)

    assert sorted(log[1:]) == ["redacted a .docx file (1 output)", "redacted a .pdf file (2 outputs)",
                               "redacted a .png file (1 output)", "redacted a .txt file (1 output)"]
    assert sorted(p.name for p in outbox.iterdir()) == [
        "letter.docx.md", "notes.txt", "photo.png", "scan.pdf", "scan.pdf.md"]  # no temp folder left
    result = leak.run(corpus, outbox, None)
    assert result["survivors"] == [] and result["violations"] == [] and result["missing_outputs"] == []
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in inbox.iterdir()} == originals

    haystacks = {"log": "\n".join(log), "audit": audit.read_text(encoding="utf-8")}
    compact = {k: leak.normalize(v) for k, v in haystacks.items()}
    spaced = {k: leak.words(v) for k, v in haystacks.items()}
    leaked = [s["value"] for d in manifest["documents"] for s in d["seeded"]
              if s["entity_type"] != "FACE" and leak.found_in(s["entity_type"], s["value"], compact, spaced)]
    assert leaked == []


def test_ctrl_c_stops_the_watch_command_and_removes_its_temp_folder(tmp_path):
    """`redactit watch` as a user runs it, stopped from the keyboard: Ctrl+C on POSIX,
    Ctrl+Break on Windows (a test cannot send Ctrl+C to one process group there)."""
    inbox, outbox, stderr = tmp_path / "inbox", tmp_path / "outbox", tmp_path / "stderr.txt"
    env = {**os.environ, "REDACTIT_DATA_DIR": str(tmp_path / "data"), "PYTHON_KEYRING_BACKEND": "hostkit.MemoryKeyring"}
    code = "import sys; sys.path.insert(0, sys.argv.pop(1)); from redactit.cli import main; sys.exit(main(sys.argv[1:]))"
    cmd = [sys.executable, "-c", code, str(TESTS), "watch", "--inbox", str(inbox), "--outbox", str(outbox)]
    windows = sys.platform == "win32"
    with stderr.open("wb") as err:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, env=env,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if windows else 0)
    read = lambda: stderr.read_text(encoding="utf-8", errors="replace")  # noqa: E731
    try:
        wait_for(lambda: "redactit watch: watching" in read() or proc.poll() is not None, timeout=300)
        assert len(temp_folders(outbox)) == 1, read()
        (inbox / "note.txt").write_text("Call Priya Okafor at priya.okafor@corp.local.\n", encoding="utf-8")
        wait_for(lambda: (outbox / "note.txt").exists(), timeout=120)
        proc.send_signal(signal.CTRL_BREAK_EVENT if windows else signal.SIGINT)
        assert proc.wait(timeout=60) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "redactit watch: stopped" in read()
    assert temp_folders(outbox) == []
    # The command records its output in the data folder, to be deleted once it expires,
    # and audits the start-up purge.
    index = json.loads((tmp_path / "data" / "copies.json").read_text(encoding="utf-8"))
    assert [e["path"] for e in index["copies"]] == [str(outbox / "note.txt")]
    events = [json.loads(line)["event"] for line in (tmp_path / "data" / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "copies_purge" in events and "redaction" in events
    out = (outbox / "note.txt").read_text(encoding="utf-8")
    assert "Okafor" not in out and "[PERSON_1]" in out
    assert not [v for v in ("Priya", "Okafor", "corp.local") if v in read()]
