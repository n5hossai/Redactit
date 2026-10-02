/**
 * Getting the redacted copy out of the left folder, in order of preference:
 *
 * 1. Attach to chat: the worker hands the copy it checked to the chat tab's content
 *    script, which puts it in the composer the way it puts in a redacted drop. The panel
 *    names only the job, never bytes, so nothing but host output can reach a page that
 *    way. Until the worker has that message (`redactit/attach`), the panel says so.
 * 2. Drag: Chromium does not carry a File made in a page to another page. One added to
 *    the drag in `dragstart` arrives at a drop target only as its name, in text/plain
 *    (tests/e2e/test_panel.py records this), and a chat would paste that name. So the
 *    drag carries only `DownloadURL`: dropped on the desktop or a folder, it saves the copy.
 * 3. Download: a link to a blob URL, which extension pages may download.
 */
import { $, announce, ask, copyText, setNote } from './common.js';

/** @typedef {import('./dropzone.js').Redacted} Redacted */

export function initResult() {
  /** @type {Redacted|null} */
  let current = null;
  let url = null;
  let copyTimer = 0;
  const box = $('result');
  const stage = $('stage');
  const dragOut = $('dragOut');
  const chip = $('chip');
  const link = $('downloadLink');
  const note = $('resultNote');
  const attachBtn = $('attachBtn');

  function show(r) {
    clear();
    current = r;
    url = URL.createObjectURL(r.blob);
    $('chipName').textContent = r.name;
    link.href = url;
    link.download = r.name;
    link.setAttribute('aria-label', `Download ${r.name}`);
    $('copyResultBtn').hidden = r.text === null;
    $('copyResultLabel').textContent = r.blob.type.startsWith('text/') ? 'Copy text' : 'Copy page text';
    if (r.review?.needed) {
      const what = r.review.count === 1 ? 'one item' : r.review.count ? `${r.review.count} items` : 'some items';
      setNote($('reviewNote'), `Redactit was unsure about ${what}. Check the copy before you send it.`, 'warn');
    }
    box.hidden = false;
  }

  /** Forgets the copy: its blob URL stops working, so nothing can be dragged or saved. */
  function clear() {
    current = null;
    if (url) URL.revokeObjectURL(url);
    url = null;
    link.removeAttribute('href');
    box.hidden = true;
    note.hidden = true;
    $('reviewNote').hidden = true;
  }

  async function attach() {
    if (!current || attachBtn.getAttribute('aria-busy') === 'true') return;
    attachBtn.setAttribute('aria-busy', 'true');
    const reply = await ask({ type: 'redactit/attach', job: current.job, tabId: current.tabId });
    attachBtn.removeAttribute('aria-busy');
    if (reply && reply.ok === true) {
      setNote(note, 'Attached to the chat. Check it there before you send it.', 'good');
      announce('Attached to the chat.');
    } else if (reply && reply.ok === false && typeof reply.message === 'string') {
      setNote(note, reply.message, 'bad');
      announce(reply.message);
    } else {
      const text = "Attaching isn't available in this version of Redactit yet. Download the copy and add it to the chat, or drag the left folder to your desktop.";
      setNote(note, text, 'warn');
      announce(text);
    }
  }

  function startDrag(e) {
    if (!current || !url) {
      e.preventDefault();
      return;
    }
    e.dataTransfer.effectAllowed = 'copy';
    e.dataTransfer.setData('DownloadURL', `${current.blob.type}:${current.name}:${url}`);
    if (e.currentTarget === dragOut) {
      try {
        e.dataTransfer.setDragImage(chip, 28, 18);
      } catch {
        // the default drag image will do
      }
      stage.classList.add('lifting');
    }
  }

  for (const source of [dragOut, chip]) {
    source.addEventListener('dragstart', startDrag);
    source.addEventListener('dragend', () => stage.classList.remove('lifting'));
  }
  dragOut.addEventListener('click', () => attach());
  dragOut.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault();
    attach();
  });
  attachBtn.addEventListener('click', () => attach());

  $('copyResultBtn').addEventListener('click', async () => {
    if (!current || current.text === null) return;
    const label = $('copyResultLabel');
    const before = label.textContent;
    const ok = await copyText(current.text);
    label.textContent = ok ? 'Copied' : 'Copy blocked';
    announce(ok ? 'Redacted text copied.' : 'Copying was blocked. Download the copy instead.');
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => { label.textContent = before; }, 1800);
  });

  return { show, clear };
}
