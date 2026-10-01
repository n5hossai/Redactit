"""The native host's message schema, chunk reassembly and install config, without a process.

Round trips through a real host process are in tests/test_host.py.
"""

import base64
import json
from pathlib import Path

import pytest
from redactit.hosts import native
from redactit.hosts.native import RAW_CHUNK, Job, Reject, parse

TEMPLATE = Path(__file__).resolve().parents[2] / "installers" / "host-manifest.json"
EXTENSION = "chrome-extension://" + "abcdefghijklmnop" * 2 + "/"
SECRET = "Priya Okafor"  # must never come back in an error


def body(msg) -> bytes:
    return json.dumps(msg).encode("utf-8")


def header(rid="r1", size=10, **extra):
    return {"type": "redact_text", "id": rid, "scope": "claude.ai/chat/1", "site": "claude.ai", "size": size,
            "total": native.chunk_count(size), **extra}


def chunk(seq, data: bytes, total, rid="r1"):
    return {"type": "chunk", "id": rid, "seq": seq, "total": total, "data": base64.b64encode(data).decode("ascii")}


def rejected(raw: bytes) -> Reject:
    with pytest.raises(Reject) as info:
        parse(raw)
    assert SECRET not in info.value.message and SECRET not in str(info.value.rid)
    return info.value


@pytest.mark.parametrize("msg", [
    {"type": "ping", "id": "p1"},
    {"type": "cancel", "id": "r1"},
    header(),
    {**header(), "type": "redact_file", "kind": "pdf"},
    chunk(0, b"abc", 1),
])
def test_every_message_type_parses(msg):
    assert parse(body(msg)) == msg


@pytest.mark.parametrize(("raw", "code"), [
    (b"", "bad_json"),
    (b"{not json", "bad_json"),
    (b"\xff\xfe{}", "bad_json"),  # not UTF-8
    (b"[1, 2]", "bad_json"),  # not an object
    (b'{"type": "ping", "id": "a", "id": "b"}', "bad_json"),  # duplicate key
    (b'{"type": "ping", "id": NaN}', "bad_json"),
    (b"[" * 100_000 + b"]" * 100_000, "bad_json"),  # nested too deep to parse
], ids=["empty", "not-json", "not-utf8", "not-object", "duplicate-key", "nan", "too-deep"])
def test_malformed_json_is_refused(raw, code):
    assert rejected(raw).code == code


@pytest.mark.parametrize(("msg", "detail"), [
    ({"type": "exfiltrate", "id": "u1", "text": SECRET}, "unknown message type"),
    ({"type": ["ping"], "id": "u1"}, "unknown message type"),
    ({"type": "ping", "id": "u1", "note": SECRET}, "unknown or missing fields"),
    ({"type": "ping"}, "unknown or missing fields"),
    ({**header(), "kind": "pdf"}, "unknown or missing fields"),  # kind belongs to redact_file only
    ({**header(), "size": True}, "field 'size' is not valid"),
    ({**header(), "size": 10.0}, "field 'size' is not valid"),
    ({**header(), "size": -1}, "field 'size' is not valid"),
    ({**header(), "total": 0}, "field 'total' is not valid"),
    ({**header(), "site": "https://claude.ai/chat"}, "field 'site' is not valid"),
    ({**header(), "scope": SECRET}, "field 'scope' is not valid"),
    ({**header(), "type": "redact_file", "kind": "exe"}, "field 'kind' is not valid"),
    ({**chunk(0, b"", 1), "data": "A" * (native.CHUNK + 4)}, "field 'data' is not valid"),
])
def test_schema_violations_are_refused_without_echoing_values(msg, detail):
    rej = rejected(body(msg))
    assert (rej.code, rej.message) == ("bad_message", detail)


def test_an_id_that_could_carry_content_is_not_echoed():
    rej = rejected(body({"type": "exfiltrate", "id": SECRET}))
    assert rej.rid is None


def test_chunks_reassemble_exactly():
    data = bytes(range(256)) * (RAW_CHUNK // 256 * 2 + 3)  # two full chunks and a partial one
    job = Job.start(header(size=len(data)))
    pieces = [data[i:i + RAW_CHUNK] for i in range(0, len(data), RAW_CHUNK)]
    done = [job.add(parse(body(chunk(seq, p, len(pieces))))) for seq, p in enumerate(pieces)]
    assert done == [False, False, True] and job.payload() == data


def test_an_empty_payload_is_one_empty_chunk():
    job = Job.start(header(size=0))
    assert job.total == 1 and job.add(chunk(0, b"", 1)) and job.payload() == b""


@pytest.mark.parametrize("bad", [
    chunk(1, b"x" * RAW_CHUNK, 2),  # wrong sequence number
    chunk(0, b"x" * RAW_CHUNK, 3),  # wrong total
    chunk(0, b"x" * 10, 2),  # a short chunk before the last
])
def test_out_of_order_or_wrong_size_chunks_are_refused(bad):
    job = Job.start(header(size=RAW_CHUNK + 5))
    with pytest.raises(Reject) as info:
        job.add(bad)
    assert (info.value.code, info.value.rid) == ("bad_sequence", "r1")


def test_a_last_chunk_longer_than_declared_is_refused():
    job = Job.start(header(size=5))
    with pytest.raises(Reject, match="bad_sequence"):
        job.add(chunk(0, b"123456", 1))


def test_a_total_that_does_not_follow_from_the_size_is_refused():
    with pytest.raises(Reject, match="bad_sequence"):
        Job.start({**header(size=10), "total": 2})


def test_chunk_data_must_be_base64():
    job = Job.start(header(size=3))
    with pytest.raises(Reject) as info:
        job.add({**chunk(0, b"abc", 1), "data": "not base64!"})
    assert info.value.code == "bad_message"


def test_the_installed_manifest_is_the_only_source_of_the_allowed_origin(tmp_path):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"allowed_origins": [EXTENSION]}), encoding="utf-8")
    assert native.allowed_origins(manifest) == {EXTENSION}


@pytest.mark.parametrize("origins", [
    [],
    [EXTENSION, "chrome-extension://" + "b" * 32 + "/"],  # a second caller
    ["chrome-extension://*/"],
    ["https://claude.ai/"],
    "chrome-extension://" + "a" * 32 + "/",  # not a list
])
def test_a_manifest_must_allow_exactly_one_extension(tmp_path, origins):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({"allowed_origins": origins}), encoding="utf-8")
    with pytest.raises(Reject, match="bad_config"):
        native.allowed_origins(manifest)


def test_the_unfilled_template_is_refused():
    """An installer that forgot to write the real ID gets a host that serves nobody."""
    assert json.loads(TEMPLATE.read_text(encoding="utf-8"))["type"] == "stdio"
    with pytest.raises(Reject, match="bad_config"):
        native.allowed_origins(TEMPLATE)


def test_a_missing_manifest_is_refused(tmp_path):
    with pytest.raises(Reject, match="bad_config"):
        native.allowed_origins(tmp_path / "missing.json")


def test_frames_to_the_extension_stay_under_chromes_limit():
    largest = native._frame({"type": "chunk", "id": "x" * 64, "seq": 10**6, "total": 10**6,
                             "data": base64.b64encode(b"\xff" * RAW_CHUNK).decode("ascii")})
    assert native.CHUNK <= len(largest) <= native.TO_EXTENSION_MAX and len(largest) <= native.MAX_FRAME
