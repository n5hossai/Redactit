"""Test support for the browser tests: a Chromium with the unpacked extension, a stand-in
chat site, and a native host registered for that browser only.

- The extension is a copy of extension/ with its manifest unchanged (the same key, so the
  same ID and the same three host permissions). The copy may rewrite the few lines that
  background.js and intercept.js mark for it: the host's name, so every browser gets its
  own registration, and timeouts a test needs shorter.
- The AI sites are never contacted. Playwright answers every request the browser makes:
  claude.ai and chatgpt.com get the stand-in pages in site/, a helper origin holds what
  the test copies to the clipboard, and anything else is aborted. The content scripts
  still match the real URLs, so the shipped manifest is what gets tested.
- The host is registered where this Chromium looks: `<profile>/NativeMessagingHosts/` on
  Linux and macOS, a profile made per test, so nothing outside it changes. On Windows
  Chromium reads only the registry, so registration there writes uniquely named
  HKCU keys and always deletes them; it runs only with REDACTIT_E2E_REGISTRY=1.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import sysconfig
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
EXTENSION = REPO / "extension"
SITE = Path(__file__).parent / "site"
FAKE_HOST = Path(__file__).parent / "fakehost.py"
HELPER = "https://clipboard-helper.test/"

_KEY = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))["key"]
EXTENSION_ID = "".join("abcdefghijklmnop"[int(c, 16)] for c in hashlib.sha256(base64.b64decode(_KEY)).hexdigest()[:32])
ORIGIN = f"chrome-extension://{EXTENSION_ID}/"
# Where Playwright's Chromium looks on Windows (checked: its Chrome for Testing key is not read).
REGISTRY_PATHS = (r"Software\Chromium\NativeMessagingHosts",)


def unavailable(reason: str, *, module: bool = False):
    """Skips, unless REDACTIT_E2E_REQUIRED=1 (CI's e2e job), where a skip would let the
    job pass without having tested anything."""
    import pytest

    if os.environ.get("REDACTIT_E2E_REQUIRED") == "1":
        pytest.fail(f"required browser test cannot run: {reason}", pytrace=False)
    pytest.skip(reason, allow_module_level=module)


REGISTRY_REASON = ("Windows Chromium finds native hosts only in the registry; "
                   "set REDACTIT_E2E_REGISTRY=1 to let this test add and remove an HKCU key")


def registry_allowed() -> bool:
    return sys.platform != "win32" or os.environ.get("REDACTIT_E2E_REGISTRY") == "1"


def unique_host_name() -> str:
    return f"com.redactit.test_{uuid.uuid4().hex[:12]}"


# --- the extension -----------------------------------------------------------------------

def _rewrite(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{path.name}: expected exactly one {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def build_extension(dest: Path, host_name: str, *, warm_hold_ms: int | None = None,
                    adapter_check_ms: int | None = None) -> Path:
    shutil.copytree(EXTENSION, dest)
    _rewrite(dest / "background.js", "const HOST_NAME = 'com.redactit.host';", f"const HOST_NAME = '{host_name}';")
    if warm_hold_ms is not None:
        _rewrite(dest / "background.js", "const WARM_HOLD_MS = 30_000;", f"const WARM_HOLD_MS = {warm_hold_ms};")
    if adapter_check_ms is not None:
        _rewrite(dest / "content" / "intercept.js", "const ADAPTER_CHECK_MS = 15_000;",
                 f"const ADAPTER_CHECK_MS = {adapter_check_ms};")
    return dest


# --- the native host ---------------------------------------------------------------------

class Registration:
    """A host registered for one browser profile; `close` removes every trace of it."""

    def __init__(self, tmp: Path, profile: Path, host_name: str, mode: str):
        import hostkit

        self.name, self.mode = host_name, mode
        self.pid_file, self.log_file = tmp / "host.pid", tmp / "host-frames.log"
        self.data_dir, self.stderr_file = tmp / "host-data", tmp / "host-stderr.txt"
        manifest = tmp / f"{host_name}.json"
        python = sys._base_executable
        if mode == "real":
            # What the installer's launcher runs (PLAN §7), with the test's in-memory keyring.
            args = ["-I", "-S", "-c", hostkit.LAUNCH, sysconfig.get_path("purelib"), str(TESTS), "--manifest", str(manifest)]
        else:
            args = ["-I", "-S", str(FAKE_HOST), mode, str(self.pid_file), str(self.log_file)]
        env = {"REDACTIT_DATA_DIR": str(self.data_dir), "PYTHON_KEYRING_BACKEND": "hostkit.MemoryKeyring"}
        if os.environ.get("REDACTIT_MODEL_DIR"):
            env["REDACTIT_MODEL_DIR"] = str(Path(os.environ["REDACTIT_MODEL_DIR"]).resolve())
        launcher = _write_launcher(tmp / "launcher", python, args, env, self.stderr_file)
        manifest.write_text(json.dumps({"name": host_name, "description": "test host", "path": str(launcher),
                                        "type": "stdio", "allowed_origins": [ORIGIN]}), encoding="utf-8")
        self._keys: list[str] = []  # deleted in reverse: our host key, then any parent we created
        if sys.platform == "win32":
            import winreg

            for base in REGISTRY_PATHS:
                try:
                    winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, base))
                except FileNotFoundError:
                    self._keys.append(base)
                key = f"{base}\\{host_name}"
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key) as k:
                    winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(manifest))
                self._keys.append(key)
        else:
            folder = profile / "NativeMessagingHosts"
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copy(manifest, folder / manifest.name)

    def pid(self, timeout: float = 30) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with contextlib.suppress(OSError, ValueError):
                return int(self.pid_file.read_text(encoding="ascii"))
            time.sleep(0.1)
        raise TimeoutError("the host never started")

    def frames(self) -> list[dict]:
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text(encoding="utf-8").splitlines()]

    def close(self) -> None:
        if sys.platform == "win32":
            import winreg

            for key in reversed(self._keys):
                # DeleteKey refuses a key with subkeys, so a parent another test still uses stays.
                with contextlib.suppress(FileNotFoundError, PermissionError, OSError):
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
            self._keys.clear()


def _write_launcher(stem: Path, python: str, args: list[str], env: dict, stderr: Path) -> Path:
    if sys.platform == "win32":
        # Chrome runs a host through `cmd /d /c`, so a batch file works; nothing here needs
        # escaping beyond double quotes (the code has no %, quotes or redirections).
        assert not any(re.search(r'[%"^&|<>]', a) for a in args)
        lines = ["@echo off", *[f'set "{k}={v}"' for k, v in env.items()],
                 " ".join(f'"{a}"' for a in [python, *args]) + f' %* 2>>"{stderr}"']
        path = stem.with_suffix(".bat")
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))
        return path
    quote = lambda s: "'" + s.replace("'", "'\\''") + "'"  # noqa: E731
    lines = ["#!/bin/sh", *[f"export {k}={quote(v)}" for k, v in env.items()],
             "exec " + " ".join(quote(a) for a in [python, *args]) + f' "$@" 2>>{quote(str(stderr))}']
    path = stem.with_suffix(".sh")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


# --- the browser -------------------------------------------------------------------------

def _route(route) -> None:
    """Every request the browser makes is answered here; nothing reaches the network."""
    url = route.request.url
    path = url.split("?", 1)[0]
    if path.endswith("/__standin/recorder.js"):
        return route.fulfill(status=200, content_type="text/javascript", body=(SITE / "recorder.js").read_text("utf-8"))
    if url.startswith("https://claude.ai/"):
        return route.fulfill(status=200, content_type="text/html", body=(SITE / "claude.html").read_text("utf-8"))
    if url.startswith("https://chatgpt.com/"):
        return route.fulfill(status=200, content_type="text/html", body=(SITE / "bare.html").read_text("utf-8"))
    if url.startswith(HELPER):
        return route.fulfill(status=200, content_type="text/html",
                             body="<!doctype html><textarea id=src rows=4 cols=60></textarea>")
    return route.abort()


class Browser:
    """One Chromium with the extension loaded, in its own profile."""

    def __init__(self, playwright, profile: Path, extension: Path):
        kwargs = {}
        if os.environ.get("REDACTIT_E2E_CHROMIUM"):  # a specific Chromium build, if Playwright's cannot start here
            kwargs["executable_path"] = os.environ["REDACTIT_E2E_CHROMIUM"]
        # channel="chromium" is the full browser in the new headless mode, which runs
        # extensions; Playwright's default headless shell does not.
        self.context = playwright.chromium.launch_persistent_context(
            str(profile), channel="chromium", headless=True,
            args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"], **kwargs)
        self.context.route("**/*", _route)
        workers = [w for w in self.context.service_workers if w.url.startswith(ORIGIN)]
        self.worker = workers[0] if workers else self.context.wait_for_event(
            "serviceworker", predicate=lambda w: w.url.startswith(ORIGIN), timeout=30_000)
        self._helper = None

    def close(self) -> None:
        self.context.close()

    def open(self, url: str):
        page = self.context.new_page()
        page.goto(url)
        return page

    def copy(self, text: str) -> None:
        """Puts text on the clipboard the way a user would: selected and copied in another site."""
        if self._helper is None:
            self._helper = self.open(HELPER)
        self._helper.fill("#src", text)
        self._helper.focus("#src")
        self._helper.keyboard.press("Control+A")
        self._helper.keyboard.press("Control+C")

    def paste(self, page, selector: str, text: str) -> None:
        self.copy(text)
        page.bring_to_front()
        page.click(selector)
        page.keyboard.press("Control+V")

    def drop_files(self, page, selector: str, paths: list[Path]) -> None:
        """A real drop of files from the desktop: trusted events, through Chrome's own drag path."""
        box = page.locator(selector).bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        cdp = self.context.new_cdp_session(page)
        data = {"items": [], "files": [str(p) for p in paths], "dragOperationsMask": 1}
        for kind in ("dragEnter", "dragOver", "drop"):
            cdp.send("Input.dispatchDragEvent", {"type": kind, "x": x, "y": y, "data": data})
        cdp.detach()

    def panel(self):
        """The side panel's page, open in a tab: an extension page, as the panel is."""
        return self.open(f"{ORIGIN}sidepanel/panel.html")

    @staticmethod
    def api(page, message: dict):
        """One message to the service worker's API, sent from `page`."""
        return page.evaluate("m => chrome.runtime.sendMessage(m)", message)

    def recent(self) -> list[dict]:
        """What the service worker records per request: kind, site, held, outcome."""
        return self.worker.evaluate("recent")

    def wait_recent(self, count: int, timeout: float = 120) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(entries := self.recent()) >= count:
                return entries
            time.sleep(0.2)
        raise TimeoutError(f"fewer than {count} requests finished")

    def evaluate(self, expression: str):
        return self.worker.evaluate(expression)


class Setup:
    """A browser plus, optionally, a registered host; everything torn down in reverse."""

    def __init__(self, playwright, tmp: Path, *, host: str | None, **build):
        tmp.mkdir(parents=True, exist_ok=True)
        name = unique_host_name()
        profile = tmp / "profile"
        self.browser = None
        self.registration = Registration(tmp, profile, name, host) if host else None
        try:
            self.browser = Browser(playwright, profile, build_extension(tmp / "extension", name, **build))
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self.registration:
                self.registration.close()


# What the page's own scripts get from the calls extension/content/guard.js replaces, and
# from trying to undo it. "+" marks Redactit's own refusal, not the browser's.
GUARD_PROBE = """async () => {
    const outcome = (call) => call().then(() => 'allowed', (e) => e.name + (/Redactit/.test(e.message) ? '+' : ''));
    const result = {
        readText: await outcome(() => navigator.clipboard.readText()),
        read: await outcome(() => navigator.clipboard.read()),
        picker: await outcome(() => window.showOpenFilePicker()),
        directory: await outcome(() => window.showDirectoryPicker()),
        deleted: delete Clipboard.prototype.readText,
    };
    try { Object.defineProperty(Clipboard.prototype, 'readText', {value: () => 'mine'}); result.redefined = true; }
    catch (e) { result.redefined = e.name; }
    window.showOpenFilePicker = () => 'mine';
    result.assigned = window.showOpenFilePicker.name;
    result.after = await outcome(() => navigator.clipboard.readText());
    return result;
}"""
GUARD_REFUSED = {"readText": "NotAllowedError+", "read": "NotAllowedError+", "picker": "NotAllowedError+",
                 "directory": "NotAllowedError+", "deleted": False, "redefined": "TypeError",
                 "assigned": "showOpenFilePicker", "after": "NotAllowedError+"}


def notice_text(context, page) -> str:
    """The text of Redactit's in-page notice, read through DevTools, which can see into a
    closed shadow root; the page itself cannot."""
    cdp = context.new_cdp_session(page)
    try:
        root = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})["root"]
    finally:
        cdp.detach()
    found: list[str] = []

    def walk(node, inside_closed: bool) -> None:
        for shadow in node.get("shadowRoots", []):
            walk(shadow, inside_closed or shadow.get("shadowRootType") == "closed")
        for child in node.get("children", []):
            walk(child, inside_closed)
        if inside_closed and node.get("nodeType") == 3:
            found.append(node.get("nodeValue", ""))

    walk(root, False)
    return " ".join(t for t in found if t.strip())


def wait_for(predicate, timeout: float = 60, step: float = 0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := predicate():
            return value
        time.sleep(step)
    raise TimeoutError("condition not met")


def page_view(page) -> str:
    """Everything the page's scripts saw or hold: event records, files, DOM history, markup."""
    return json.dumps(page.evaluate(
        "({seen: window.__seen, dom: window.__dom, html: document.documentElement.outerHTML,"
        " files: window.__attachments.map(f => [f.name, f.size, f.type])})"))
