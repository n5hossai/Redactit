/**
 * Redact & copy: text typed or pasted into the panel goes through `redactit/redact-text`
 * for the chat in view (its site's rules, its chat's pseudonyms), and the redacted
 * version is copied, ready to paste into the chat. The panel is the reviewer here, so
 * the worker never holds this text for review.
 *
 * Only the redacted text is ever copied. The input stays in the text box, in this page's
 * memory, until the user clears it or closes the panel.
 */
import { $, announce, ask, copyText, findTarget, setNote } from './common.js';
import { LOCAL_REASONS } from './job.js';

/** The worker's limit for this message; larger text goes through a file drop instead. */
const MAX_TEXT = 8 * 1024 * 1024;

export function initTextbox() {
  const input = $('textIn');
  const btn = $('redactCopyBtn');
  const status = $('textStatus');
  const out = $('textOut');
  let copyTimer = 0;

  function say(text, tone) {
    setNote(status, text, tone);
    announce(text);
  }

  async function copyOut() {
    const ok = await copyText(out.value);
    if (!ok) {
      out.focus();
      out.select();
    }
    return ok;
  }

  btn.addEventListener('click', async () => {
    if (btn.getAttribute('aria-busy') === 'true') return;
    const text = input.value;
    if (!text.trim()) return say('Type or paste some text first.', 'warn');
    if (new TextEncoder().encode(text).length > MAX_TEXT) {
      return say('This is more than 8 MiB of text. Save it as a text file and drop it on the face instead.', 'bad');
    }
    const target = await findTarget();
    if (!target) return say(LOCAL_REASONS.no_chat, 'bad');
    btn.setAttribute('aria-busy', 'true');
    setNote(status, `Redacting for ${target.site}…`);
    out.hidden = $('textOutLabel').hidden = $('textCopyRow').hidden = true;
    const reply = await ask({ type: 'redactit/redact-text', text, tabId: target.id });
    btn.removeAttribute('aria-busy');
    if (!reply || reply.ok !== true || typeof reply.text !== 'string') {
      const why = reply && typeof reply.message === 'string' ? reply.message : LOCAL_REASONS.extension_error;
      return say(`Blocked. ${why}`, 'bad');
    }
    out.value = reply.text;
    out.hidden = $('textOutLabel').hidden = $('textCopyRow').hidden = false;
    if (await copyOut()) return say('Redacted and copied. Paste it into the chat.', 'good');
    return say('Redacted, but copying was blocked: the text below is selected, press Ctrl+C.', 'warn');
  });

  $('textCopyBtn').addEventListener('click', async () => {
    const label = $('textCopyLabel');
    const ok = await copyOut();
    label.textContent = ok ? 'Copied' : 'Copy blocked';
    announce(ok ? 'Redacted text copied.' : 'Copying was blocked. The text is selected; press Control C.');
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => { label.textContent = 'Copy again'; }, 1800);
  });
}
