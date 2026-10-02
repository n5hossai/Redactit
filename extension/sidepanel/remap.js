/**
 * Reading a reply with real names: the user pastes the AI's reply, and the worker's
 * `redactit/remap {text, tabId}` turns that chat's pseudonyms ([PERSON_1]) back into the
 * real values, which are shown here.
 *
 * T6: real values appear only inside this panel, an extension page the sites cannot
 * read. They are put in the DOM as text, never stored, never sent to a tab, and never
 * copied on their own: copying takes its own click, next to a warning that the text
 * holds real data. They are cleared when the chat in view changes, since they belong to
 * the chat they were mapped for.
 */
import { $, announce, ask, copyText, findTarget, setNote } from './common.js';
import { LOCAL_REASONS } from './job.js';

export function initRemap() {
  const input = $('remapIn');
  const btn = $('remapBtn');
  const status = $('remapStatus');
  const result = $('remapResult');
  const out = $('remapOut');
  let mappedFor = null;
  let copyTimer = 0;

  function say(text, tone) {
    setNote(status, text, tone);
    announce(text);
  }

  function clearOut() {
    out.textContent = '';
    result.hidden = true;
    mappedFor = null;
  }

  btn.addEventListener('click', async () => {
    if (btn.getAttribute('aria-busy') === 'true') return;
    const text = input.value;
    if (!text.trim()) return say("Paste the AI's reply first.", 'warn');
    const target = await findTarget();
    if (!target) return say(LOCAL_REASONS.no_chat, 'bad');
    btn.setAttribute('aria-busy', 'true');
    clearOut();
    const reply = await ask({ type: 'redactit/remap', text, tabId: target.id });
    btn.removeAttribute('aria-busy');
    if (!reply || reply.ok === false || typeof reply.text !== 'string') {
      const why = reply && typeof reply.message === 'string' ? ` ${reply.message}` : '';
      return say(`Re-mapping is not available in this version of Redactit.${why}`, 'warn');
    }
    out.textContent = reply.text;
    mappedFor = target.id;
    result.hidden = false;
    status.hidden = true;
    announce('Shown with real values below. They stay in this panel.');
    return undefined;
  });

  $('remapCopyBtn').addEventListener('click', async () => {
    if (mappedFor === null) return;
    const label = $('remapCopyLabel');
    const ok = await copyText(out.textContent);
    label.textContent = ok ? 'Copied: contains real data' : 'Copy blocked';
    announce(ok ? 'Copied. It contains real data.' : 'Copying was blocked.');
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => { label.textContent = 'Copy with real data'; }, 2500);
  });

  $('remapClearBtn').addEventListener('click', () => {
    input.value = '';
    status.hidden = true;
    clearOut();
    input.focus();
  });

  return {
    /** Real values belong to the chat they were mapped for. */
    targetChanged(target) {
      if (mappedFor === null || (target && target.id === mappedFor)) return;
      clearOut();
      setNote(status, 'Cleared: the chat in view changed.', '');
    },
  };
}
