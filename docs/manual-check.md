# Manual check on the live sites (Phase 5)

The browser tests run against a stand-in chat page, so this checks the real claude.ai and
chatgpt.com once by hand: a paste, a dropped file, a picked file, Attach to chat,
re-mapping, and a fail-closed demo. Use only the synthetic data below or the generated
corpus, never a real document. It takes about 15 minutes per site.

The adapters' selectors (`extension/content/adapters/`) were written without access to the
live sites. If a site's composer does not match, the adapter turns itself off and pastes and
files are still redacted or blocked by the generic interception. Step 9 shows which is
active.

## 1. Prepare (once)

1. Install the engine and its models, if not done already:
   `py -3.12 -m uv sync` and `py -3.12 -m uv run redactit setup-models` in the repository.
2. Make a synthetic test folder:
   `py -3.12 -m uv run --group leak python tests/corpus/generate.py --seed 7 --out %USERPROFILE%\Documents\redactit-check --per-variant 1`
3. Register the native host for Chrome:
   `powershell -NoProfile -ExecutionPolicy Bypass -File installers\dev\register-host.ps1`
   (`-Browser edge` or `-Browser both` for Edge.) Add `-WhatIf` first to see what it changes:
   one folder, `%LOCALAPPDATA%\Redactit\dev-host`, and one key under
   `HKCU\Software\Google\Chrome\NativeMessagingHosts`.
4. In Chrome, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**
   and choose the repository's `extension` folder. The ID must read
   `ejcaindhhnocdeolkgmcfnbemobjhllk`. If it does not, the host refuses the extension.

## 2. On each site (claude.ai, then chatgpt.com), signed in

Synthetic text to copy:

> Contact Priya Okafor at priya.okafor@example.com or 416-555-0199. Card 4111 1111 1111 1111.

| # | Do | Expect |
|---|---|---|
| 1 | Open the site, then click the Redactit toolbar button | The side panel opens; the status goes from Warming up to Ready |
| 2 | Paste the synthetic text into the message box | The box shows `[PERSON_1]`, `[EMAIL_1]`, the phone and card masked; never the raw text, not even for a moment |
| 3 | Drag `png\plain_000.png` from the test folder onto the message box | It attaches as `redacted-1.png`; open it: text, the face and the QR code are covered |
| 4 | Use the site's own attach button to pick `pdf\digital_000.pdf` | It attaches as `redacted-N.pdf`, with no selectable text and the values boxed |
| 5 | Drop `docx\tracked_changes_000.docx` on the side panel's face, then click **Attach to chat** | The right folder drains as the left fills; the redacted Markdown attaches as `redacted-N.md`; if your policy reviews, it asks you to approve first |
| 6 | Send the paste from step 2 and wait for the reply; paste the reply into the panel's **Read a reply with real names** | The panel shows Priya Okafor where the reply says `[PERSON_1]`; the chat page itself never shows real names |
| 7 | Open another chat in the same tab and look at the re-mapping view | It has cleared |
| 8 | **Fail-closed demo:** run `installers\dev\unregister-host.ps1`, reload the site, and paste again | The paste is blocked with a notice; nothing reaches the message box. Run `register-host.ps1` again afterwards |
| 9 | In `chrome://extensions`, click the extension's **service worker** link; in its console type `pageAdapters` | Shows, per tab, whether the site's adapter is active or turned itself off |

Note for each site: each step's result, the adapter state from step 9, and anything on the
site that broke (a button that stopped working, an error in the page).

## 3. Clean up

1. `powershell -NoProfile -ExecutionPolicy Bypass -File installers\dev\unregister-host.ps1`
2. Remove the extension in `chrome://extensions` if you no longer want it.
3. Delete `%USERPROFILE%\Documents\redactit-check`.
