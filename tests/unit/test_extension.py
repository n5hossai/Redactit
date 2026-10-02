"""The extension's manifest and code, checked without a browser (PLAN §7, THREAT_MODEL T18).

The browser tests in tests/e2e/ need Chromium; these run everywhere and catch the
changes a review could miss: a widened permission, remote code, a protocol constant that
drifted from the host's.
"""

import base64
import hashlib
import json
import re
from pathlib import Path

from redactit.hosts import native

REPO = Path(__file__).resolve().parents[2]
EXT = REPO / "extension"
MANIFEST = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
SITES = ["https://claude.ai/*", "https://chatgpt.com/*", "https://gemini.google.com/*"]
# The ID Chrome derives from the manifest's `key` (docs/PLAN.md §7): the host manifest's
# allowed_origins names it, so a new key must change both.
EXTENSION_ID = "ejcaindhhnocdeolkgmcfnbemobjhllk"


def _js_files():
    return sorted(EXT.rglob("*.js"))


def _code(path: Path) -> str:
    """JS without its comments, so a comment that mentions eval is not a finding."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$|(?<=[;,{})\s])//[^'\"`\n]*$", "", text)


def extension_id(key_b64: str) -> str:
    digest = hashlib.sha256(base64.b64decode(key_b64)).hexdigest()[:32]
    return "".join("abcdefghijklmnop"[int(c, 16)] for c in digest)


def test_permissions_are_exactly_the_planned_ones():
    assert MANIFEST["manifest_version"] == 3
    assert MANIFEST["permissions"] == ["nativeMessaging", "storage", "sidePanel"]
    assert MANIFEST["host_permissions"] == SITES
    for absent in ("optional_permissions", "optional_host_permissions", "externally_connectable",
                   "web_accessible_resources", "content_security_policy", "update_url"):
        assert absent not in MANIFEST, absent  # the default MV3 CSP applies; nothing else can reach us
    assert "<all_urls>" not in json.dumps(MANIFEST)


def test_content_scripts_run_first_on_the_three_sites_only():
    scripts = MANIFEST["content_scripts"]
    for entry in scripts:
        assert entry["run_at"] == "document_start" and entry["all_frames"] is True
        assert set(entry["matches"]) <= set(SITES)
    guard, *isolated = scripts
    # The main-world guard covers every site; nothing else runs in the page's own world.
    assert guard == {"matches": SITES, "js": ["content/guard.js"], "run_at": "document_start",
                     "all_frames": True, "match_origin_as_fallback": True, "world": "MAIN"}
    assert sorted(m for s in isolated for m in s["matches"]) == sorted(SITES)
    for entry in isolated:
        assert "world" not in entry  # the isolated world: the page cannot reach these scripts
        assert entry["js"][-1] == "content/intercept.js"
        assert len(entry["js"]) == 2 and entry["js"][0].startswith("content/adapters/")
        assert (EXT / entry["js"][0]).is_file()


def test_the_key_gives_the_documented_id_and_no_private_key_is_shipped():
    assert extension_id(MANIFEST["key"]) == EXTENSION_ID
    for path in EXT.rglob("*"):
        if path.is_file():
            assert b"PRIVATE KEY" not in path.read_bytes(), path


def test_no_remote_code_no_eval_and_no_logging():
    for path in _js_files():
        code = _code(path)
        rel = path.relative_to(EXT)
        assert not re.search(r"\beval\s*\(|\bnew\s+Function\b|\bimportScripts\b|\bimport\s*\(", code), rel
        assert not re.search(r"setTimeout\s*\(\s*['\"`]|setInterval\s*\(\s*['\"`]", code), rel
        assert not re.search(r"https?://", code), rel  # no fetches, no remote scripts
        assert not re.search(r"\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon", code), rel  # no telemetry
        assert not re.search(r"\bconsole\.", code), rel  # nothing logged, so nothing raw in logs
        assert ".innerHTML" not in code and "insertAdjacentHTML" not in code, rel
    for path in EXT.rglob("*.html"):
        assert not re.search(r"<script[^>]+src=[\"']https?:", path.read_text(encoding="utf-8")), path


def test_the_worker_speaks_the_hosts_protocol():
    """background.js mirrors src/redactit/hosts/native.py; a drift here breaks every request."""
    bg = (EXT / "background.js").read_text(encoding="utf-8")
    template = json.loads((REPO / "installers" / "host-manifest.json").read_text(encoding="utf-8"))
    assert f"const HOST_NAME = '{template['name']}';" in bg
    assert f"const PROTOCOL = {native.PROTOCOL};" in bg
    assert native.CHUNK == 512 * 1024 and "const CHUNK = 512 * 1024;" in bg
    assert "const RAW_CHUNK = (CHUNK / 4) * 3;" in bg and native.RAW_CHUNK == native.CHUNK // 4 * 3
    assert native.MAX_PAYLOAD == 64 * 2**20 and "const MAX_PAYLOAD = 64 * 1024 * 1024;" in bg
    assert f"const MAX_JOBS = {native.MAX_IN_FLIGHT};" in bg
    assert "const KINDS = [" + ", ".join(f"'{k}'" for k in native.KINDS) + "];" in bg
    assert "const NEEDS_IMAGES = new Set(['pdf', 'image']);" in bg and native._NEEDS_IMAGES == {"pdf", "image"}
    intercept = (EXT / "content" / "intercept.js").read_text(encoding="utf-8")
    assert native.RAW_CHUNK == 384 * 1024 and "const RAW_CHUNK = 384 * 1024;" in intercept
    modes = re.search(r"const REVIEW_MODES = \[(.*?)\];", bg).group(1)
    assert set(native.REVIEW_MODES.values()) <= set(re.findall(r"'(\w+)'", modes))


def test_real_values_have_no_path_to_a_content_script():
    """T6: re-mapping and review contents are answered to extension pages only, and no
    content script asks for them."""
    bg = (EXT / "background.js").read_text(encoding="utf-8")
    panel_only = re.search(r"const panelHandlers = \{(.*?)\n  \};", bg, re.S).group(1)
    for message in ("redactit/remap", "redactit/review-get", "redactit/review-decide"):
        assert f"'{message}'" in panel_only
        assert bg.count(f"'{message}'") == 1  # handled nowhere else
    assert "if (panelHandlers[type] && !page)" in bg
    assert "msg.kind === 'text' || KINDS.includes(msg.kind)" in bg  # a job port cannot start a remap
    for path in (EXT / "content").rglob("*.js"):
        code = _code(path)
        assert "redactit/remap" not in code and "redactit/review-" not in code, path.name


def test_attach_hands_only_host_checked_files_to_a_page():
    """redactit/attach: panel-only, keeps only the panel's file jobs' results (never a remap
    or text), in memory, and the caller sends no bytes; the page side takes it only from
    the worker, in the top frame."""
    bg = (EXT / "background.js").read_text(encoding="utf-8")
    panel_only = re.search(r"const panelHandlers = \{(.*?)\n  \};", bg, re.S).group(1)
    assert "'redactit/attach': () => attachToChat(msg)" in panel_only and bg.count("'redactit/attach'") == 1
    assert "if (!job.fromPage && KINDS.includes(job.kind)) keepForAttach(job);" in bg
    assert "'remap'" not in json.dumps(re.findall(r"const KINDS = \[.*?\];", bg))
    attach = re.search(r"async function attachToChat\(msg\) \{(.*?)\n\}", bg, re.S).group(1)
    assert "msg.data" not in attach and "msg.bytes" not in attach  # only the kept, checked bytes
    assert "fromB64Chunks(kept.chunks" in attach and "{ frameId: 0 }" in attach
    assert "chrome.storage" not in re.search(r"let attachable = null;(.*?)async function attachToChat", bg, re.S).group(1)
    intercept = _code(EXT / "content" / "intercept.js")
    assert "sender.id !== chrome.runtime.id || sender.tab" in intercept
    assert "if (window === window.top) {" in intercept


def test_the_main_world_guard_locks_what_it_replaces():
    guard = _code(EXT / "content" / "guard.js")
    assert "writable: false, enumerable: true, configurable: false" in guard
    for target, name in [("Clipboard.prototype", "read"), ("Clipboard.prototype", "readText"),
                         ("window", "showOpenFilePicker"), ("window", "showDirectoryPicker")]:
        assert f"lock({target}, '{name}'," in guard
    assert "'NotAllowedError'" in guard and "chrome." not in guard  # the page's world has no extension APIs


def test_every_code_the_host_can_send_has_a_message_for_the_user():
    bg = (EXT / "background.js").read_text(encoding="utf-8")
    reasons = re.search(r"const REASONS = \{(.*?)\n\};", bg, re.S).group(1)
    codes = set(re.findall(r"^\s+(\w+):", reasons, re.M))
    faults = set(re.findall(r"'(\w+)'", re.search(r"const FRAMING_FAULTS = new Set\(\[(.*?)\]\);", bg, re.S).group(1)))
    startup = {"origin_refused", "bad_config"}  # mapped to host_refused and host_config
    assert set(native.MESSAGES) - faults - startup <= codes, set(native.MESSAGES) - faults - startup - codes
    assert faults <= set(native.MESSAGES)  # every framing fault is one the host really sends


def test_adapters_are_marked_unverified_until_the_owner_checks_them():
    for path in (EXT / "content" / "adapters").glob("*.js"):
        text = path.read_text(encoding="utf-8")
        assert "UNVERIFIED" in text and "verified: false" in text, path.name
