"""Every file pick reaches redaction, wherever the page keeps its file input and however it
opens the chooser (THREAT_MODEL T5): an input in no document, in an open or a closed
shadow root, opened by click(), showPicker(), a dispatched click or a label, or clicked by
the user. The browser's chooser never opens for the page's own input, so even a page that
moves the input out of the document while the chooser is open gets only redacted files.

The page here watches as closely as a page can: its listeners on the input and its shadow
root, and a timer reading `input.files` every few milliseconds, all through its own
reference to the input. The scripted host (stubhost.js) stands behind the worker, so these
run on every OS; one test has no host at all, where the pick must be blocked.
"""

import pytest

pytest.importorskip("playwright")

from browserkit import notice_text, wait_for  # noqa: E402

NAME, PHONE = "Alice Okafor", "555-0100"
SECRET = f"Patient {NAME}, phone {PHONE}.\n"
REPLACE = {NAME: "[PERSON_1]", PHONE: "[PHONE_1]"}
REDACTED = "Patient [PERSON_1], phone [PHONE_1].\n"
CHAT = "https://claude.ai/new"

# Builds the case's input, records everything the page can learn about its files, and
# leaves window.__open() to start the pick the way the case says.
SETUP = """(kind) => {
  const got = (window.__got = []);
  const seen = new Set();
  const keep = (entry) => {
    const key = JSON.stringify(entry);
    if (!seen.has(key)) { seen.add(key); got.push(entry); }
  };
  const input = document.createElement('input');
  input.type = 'file';
  const watch = (where) => () => {
    for (const f of input.files || []) f.text().then((text) => keep({where, name: f.name, text}));
  };
  for (const type of ['input', 'change']) {
    input.addEventListener(type, watch(type + '@input'), true);
    input.addEventListener(type, watch(type + '@input'));
  }
  setInterval(watch('timer'), 5);
  const host = document.createElement('div');
  host.id = 'host';
  host.style.cssText = 'width: 300px; height: 60px';
  const shadow = (mode) => {
    const root = host.attachShadow({mode});
    for (const type of ['input', 'change']) root.addEventListener(type, watch(type + '@root'), true);
    document.body.prepend(host);
    return root;
  };
  input.style.cssText = 'width: 300px; height: 60px; display: block';
  const cases = {
    detached_click: () => () => input.click(),
    detached_show_picker: () => () => input.showPicker(),
    detached_dispatched_click: () => () => input.dispatchEvent(new MouseEvent('click', {bubbles: true})),
    detached_label: () => {
      const label = document.createElement('label');
      const span = document.createElement('span');
      label.append(span, input);
      return () => span.click();
    },
    open_shadow_removed_while_open: () => {
      shadow('open').append(input);
      return () => { input.click(); setTimeout(() => host.remove(), 0); };
    },
    closed_shadow_click: () => {
      shadow('closed').append(input);
      return () => input.click();
    },
    closed_shadow_slotted_label_removed_while_open: () => {
      const label = document.createElement('label');
      label.append(document.createElement('slot'), input);
      shadow('closed').append(label);
      const span = document.createElement('span');
      span.textContent = 'attach';
      host.append(span);
      return () => { span.click(); setTimeout(() => host.remove(), 0); };
    },
    user_clicks_closed_shadow_input: () => {
      shadow('closed').append(input);
      input.addEventListener('click', () => setTimeout(() => host.remove(), 0));
      return null;
    },
    user_clicks_input_removed_while_open: () => {
      host.append(input);
      document.body.prepend(host);
      input.addEventListener('click', () => setTimeout(() => host.remove(), 0));
      return null;
    },
  };
  window.__input = input;
  window.__open = cases[kind]();
}"""

SCRIPTED = ["detached_click", "detached_show_picker", "detached_dispatched_click", "detached_label",
            "open_shadow_removed_while_open", "closed_shadow_click", "closed_shadow_slotted_label_removed_while_open"]
BY_USER = ["user_clicks_closed_shadow_input", "user_clicks_input_removed_while_open"]


def secret_file(tmp_path):
    path = tmp_path / f"{NAME} notes.txt"
    path.write_bytes(SECRET.encode())
    return path


def pick(s, page, kind, path):
    page.bring_to_front()
    page.evaluate(SETUP, kind)
    with page.expect_file_chooser() as chooser:
        if kind in BY_USER:
            page.click("#host")
        else:
            page.evaluate("window.__open()")
    # The chooser is Redactit's own, never the page's input.
    assert chooser.value.element.evaluate("el => el !== window.__input && !el.isConnected")
    chooser.value.set_files(str(path))


@pytest.mark.parametrize("kind", SCRIPTED + BY_USER)
def test_a_pick_reaches_the_page_only_redacted(setup, tmp_path, kind):
    s = setup()
    s.browser.stub_host("redact", replace=REPLACE)
    page = s.browser.open(CHAT)
    pick(s, page, kind, secret_file(tmp_path))
    got = wait_for(lambda: (g := page.evaluate("window.__got")) and any(e["text"] == REDACTED for e in g) and g)
    page.wait_for_timeout(300)  # the timer keeps reading the input
    got = page.evaluate("window.__got")
    assert not [e for e in got if NAME in e["text"] or PHONE in e["text"] or NAME in e["name"]]
    assert {e["name"] for e in got} == {"redacted-1.txt"}
    assert [(r["kind"], r["code"]) for r in s.browser.recent()] == [("txt", "delivered")]


def test_the_page_cannot_undo_the_guard_on_file_inputs(setup, tmp_path):
    """The replaced click(), showPicker() and dispatchEvent() cannot be deleted, redefined or
    assigned; a pick through them still opens Redactit's chooser."""
    s = setup()
    s.browser.stub_host("redact", replace=REPLACE)
    page = s.browser.open(CHAT)
    tried = page.evaluate("""() => {
      const out = {};
      for (const [proto, name] of [[HTMLElement.prototype, 'click'], [HTMLInputElement.prototype, 'showPicker'],
                                   [EventTarget.prototype, 'dispatchEvent'], [Element.prototype, 'attachShadow']]) {
        const before = proto[name];
        out[name + '.deleted'] = delete proto[name];
        try { Object.defineProperty(proto, name, {value: () => 'mine'}); out[name + '.redefined'] = true; }
        catch (e) { out[name + '.redefined'] = e.name; }
        proto[name] = () => 'mine';
        out[name + '.kept'] = proto[name] === before;
      }
      return out;
    }""")
    assert tried == {f"{n}.{k}": v for n in ("click", "showPicker", "dispatchEvent", "attachShadow")
                     for k, v in (("deleted", False), ("redefined", "TypeError"), ("kept", True))}
    pick(s, page, "detached_click", secret_file(tmp_path))
    wait_for(lambda: any(e["text"] == REDACTED for e in page.evaluate("window.__got")))


def test_without_a_host_a_detached_pick_is_blocked_and_the_page_gets_nothing(setup, tmp_path):
    s = setup(host=None)
    page = s.browser.open(CHAT)
    pick(s, page, "detached_click", secret_file(tmp_path))
    assert s.browser.wait_recent(1)[0]["code"] == "host_missing"
    wait_for(lambda: "Nothing was sent" in notice_text(s.browser.context, page))
    page.wait_for_timeout(300)
    assert page.evaluate("window.__got") == []
    assert page.evaluate("window.__input.files.length") == 0
