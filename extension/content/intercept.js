/**
 * Redactit's interception on the AI sites, shared by every adapter (PLAN §7).
 *
 * Runs at document_start, before any of the site's scripts, so its capture-phase listeners
 * on `window` are the first to see a paste, a drop, or a click that would open a file
 * chooser (THREAT_MODEL T5). Each one is cancelled before the site sees it; its content
 * goes to the service worker, and only the redacted result comes back into the page: as a
 * synthetic event of the same kind, which the site handles as it would the user's, or,
 * if the site ignores that, inserted directly. Files are picked in Redactit's own chooser,
 * never the page's (see "file choosers" below, and guard.js for picks a script starts).
 *
 * Fails closed (T7): when anything goes wrong the original is simply not passed on, and a
 * notice says why. Nothing here ever receives the real values behind pseudonyms; those
 * stay in the side panel, an extension page (T6). Nothing is logged.
 *
 * The site adapter (content/adapters/<site>.js, loaded first) only says where the composer
 * and the file input are. Without one, or when its selectors match nothing, the generic
 * handling below still cancels, redacts and blocks exactly the same way.
 */
(() => {
  'use strict';

  if (globalThis.RedactitIntercept) return; // once per frame
  globalThis.RedactitIntercept = true;

  // A test build may rewrite this line: tests/e2e/browserkit.py.
  const ADAPTER_CHECK_MS = 15_000;

  const RAW_CHUNK = 384 * 1024; // the host's chunk size (src/redactit/hosts/native.py)
  const MAX_PAYLOAD = 64 * 1024 * 1024;
  const SLOW_NOTICE_MS = 400; // quicker than this, a notice would only flicker
  const TEXT_INPUTS = new Set(['text', 'search', 'url', 'email', 'tel', '']);
  const TEXT_EXT = new Set(['txt', 'text', 'csv', 'tsv', 'log', 'json', 'xml', 'yaml', 'yml', 'ini', 'cfg', 'toml',
    'html', 'htm', 'css', 'js', 'ts', 'py', 'java', 'c', 'h', 'cpp', 'cs', 'go', 'rs', 'rb', 'php', 'sh', 'sql']);
  const IMAGE_TYPES = { png: 'png', jpg: 'jpg', jpeg: 'jpg', webp: 'png', bmp: 'png', gif: 'png', tif: 'png', tiff: 'png' };

  const LOCAL_REASONS = {
    unsupported: 'Redactit cannot check this kind of file.',
    too_large: 'This is larger than 64 MiB, the most Redactit checks at once.',
    unreadable: 'Redactit could not read what was pasted or dropped.',
    insert_failed: 'Redactit could not hand the redacted version to this page.',
    reload_page: 'Redactit was updated or restarted. Reload this page.',
    extension_error: 'Redactit hit an internal error.',
  };

  const adapter = (globalThis.RedactitAdapters || []).find((a) => a.hosts.includes(location.hostname)) || null;
  /** null while checking, then whether the adapter's composer was found. */
  let adapterActive = null;
  /** Events we dispatch ourselves: the only untrusted ones our listeners let through. */
  const ours = new WeakSet();
  /**
   * A drag that started inside this page carries only what the page already shows, so its
   * drop is left to the page: {data} is what it carried, read once the page's dragstart
   * listeners had set it (null if they stopped the event). Only a trusted dragstart starts
   * one. It can outlive its drag (a page that removes the drag's source keeps `dragend`
   * from ever reaching `window`), and another drag can then arrive from outside, so a drop
   * is left alone only if it carries exactly that data and no files: no drag that began
   * in the page can carry the computer's files. It ends at the next drop, dragend or
   * pointerdown, and after PAGE_DRAG_MS.
   * @type {{data: string|null}|null}
   */
  let pageDrag = null;
  let pageDragTimer = 0;
  const PAGE_DRAG_MS = 20_000;
  /** True only in the task of a page drag's drop, for the editor's insertFromDrop. */
  let pageDropInserting = false;
  let fileNumber = 0;

  /** FileLists Redactit put into a page's input: redacted already, never taken again. */
  const given = new WeakSet();

  window.addEventListener('paste', onPaste, true);
  window.addEventListener('drop', onDrop, true);
  window.addEventListener('input', onFileInput, true);
  window.addEventListener('change', onFileInput, true);
  window.addEventListener('beforeinput', onBeforeInput, true);
  window.addEventListener('click', onClick, true);
  window.addEventListener('redactit-pick', onPickRequest, true);
  window.addEventListener('dragstart', (e) => {
    if (!e.isTrusted) return;
    endPageDrag();
    pageDrag = { data: null };
    pageDragTimer = setTimeout(endPageDrag, PAGE_DRAG_MS);
  }, true);
  window.addEventListener('dragstart', (e) => { // after the page's listeners set the data
    if (e.isTrusted && pageDrag && e.dataTransfer) pageDrag.data = dragData(e.dataTransfer);
  });
  window.addEventListener('dragend', endPageDrag, true);
  window.addEventListener('pointerdown', (e) => { if (e.isTrusted) endPageDrag(); }, true);

  class Blocked extends Error {
    constructor(code, message) {
      super(code);
      this.code = code;
      this.reason = message || LOCAL_REASONS[code] || LOCAL_REASONS.extension_error;
    }
  }

  // --- interception ------------------------------------------------------------------------

  /** Untrusted pastes come from the page's own scripts, which already hold their data. */
  function onPaste(e) {
    if (!e.isTrusted || ours.has(e) || !e.clipboardData) return;
    const data = e.clipboardData;
    const files = [...data.files];
    const text = data.getData('text/plain');
    if (!files.length && !text && !data.types.length) return; // an empty paste carries nothing
    stop(e);
    const target = editableOf(e.composedPath()[0]);
    handle({ how: 'paste', target, caret: caretOf(target), text, files });
  }

  function onDrop(e) {
    if (!e.isTrusted || ours.has(e) || !e.dataTransfer) return;
    const data = e.dataTransfer;
    const drag = pageDrag;
    endPageDrag();
    if (drag && drag.data !== null && !carriesFiles(data) && dragData(data) === drag.data) {
      pageDropInserting = true; // the editor inserts it next, in this same task
      setTimeout(() => { pageDropInserting = false; }, 0);
      return;
    }
    const files = [...data.files];
    const text = files.length ? '' : data.getData('text/plain');
    if (!files.length && !text && !data.types.length) return;
    stop(e);
    const target = e.composedPath()[0];
    handle({ how: 'drop', target, point: [e.clientX, e.clientY], caret: null, text, files });
  }

  /**
   * A file picked in the browser's own chooser for one of the page's inputs. Redactit
   * opens its own chooser instead wherever it can (below, and guard.js), so this is the
   * second line: the user's click was on something it could not place. Chrome fires
   * `input` then `change`; whichever comes first takes the files and empties the input,
   * so no later listener, and no script reading `input.files`, ever sees the originals.
   * `input` is composed, so it reaches `window` from a shadow root too; from inside a
   * closed one the path shows only its host, and the input is found inside.
   */
  function onFileInput(e) {
    if (!e.isTrusted || ours.has(e)) return;
    const first = e.composedPath()[0];
    let inputs;
    if (isFileInput(first)) inputs = [first];
    else if (e.type === 'input' && !(e instanceof InputEvent)) inputs = hiddenPicks(first);
    if (!inputs || !inputs.length) return;
    e.stopImmediatePropagation();
    for (const input of inputs) {
      const files = input.files ? [...input.files] : [];
      if (!files.length) continue;
      input.value = '';
      handle({ how: 'input', target: input, caret: null, text: '', files });
    }
  }

  /** File inputs inside the closed shadow tree of `host` holding files Redactit did not
   * put there: a pick the page's listeners inside that tree would otherwise get raw. */
  function hiddenPicks(host) {
    const found = [];
    const visit = (root, depth) => {
      for (const el of root.querySelectorAll('*')) {
        if (isFileInput(el) && el.files && el.files.length && !given.has(el.files)) found.push(el);
        const inner = depth < 16 && shadowRootOf(el);
        if (inner) visit(inner, depth + 1);
      }
    };
    const root = closedRootOf(host);
    if (root) visit(root, 0);
    return found;
  }

  // --- file choosers -----------------------------------------------------------------------

  /**
   * Redactit's own file chooser. A pick the page starts (guard.js turns its click(),
   * showPicker() and dispatched clicks into a `redactit-pick` request) or the user starts
   * on one of the page's file inputs opens on this input instead of the page's. It is in no
   * document, and nothing the page can reach refers to it, so its events reach only the
   * listeners here. The picked files are redacted, and only the result goes into the page's
   * input. The page's input never holds an original, wherever it is, in a shadow root or no
   * document at all, and whatever the page does to it while the chooser is open.
   */
  const chooser = document.createElement('input');
  chooser.type = 'file';
  /** Where the open chooser's files go: {deliver(files) -> boolean, cancel()}. */
  let destination = null;
  chooser.addEventListener('change', onChosen);
  chooser.addEventListener('cancel', () => {
    const to = destination;
    destination = null;
    to?.cancel();
  });

  /** Opens the chooser for a page's input with its options; false if Chrome refused. */
  function openChooser({ multiple, directory, accept }, to) {
    chooser.multiple = multiple;
    chooser.webkitdirectory = directory;
    chooser.accept = accept;
    chooser.value = '';
    try {
      chooser.showPicker(); // needs the user's gesture, as the page's own chooser would
    } catch {
      return false;
    }
    destination = to;
    return true;
  }

  function onChosen() {
    const to = destination;
    destination = null;
    const files = chooser.files ? [...chooser.files] : [];
    chooser.value = '';
    if (!to || !files.length) return;
    handle({ how: 'pick', deliver: to.deliver, caret: null, text: '', files });
  }

  /**
   * guard.js asks for the chooser on behalf of a page script's click() or showPicker() on
   * a file input. The request carries the input's options only; the redacted files go
   * back to guard.js, which holds the input, in a `redactit-picked` event, and guard.js
   * answers by cancelling it. A page can send this request itself: it then gets a chooser
   * whose files reach it only redacted, which is what its own input would give it.
   */
  function onPickRequest(e) {
    e.stopImmediatePropagation();
    const detail = typeof e.detail === 'string' ? e.detail : '--';
    const options = { multiple: detail[0] === 'm', directory: detail[1] === 'd', accept: detail.slice(2) };
    if (openChooser(options, { deliver: handToGuard, cancel: () => tellGuard('redactit-pick-cancelled') })) {
      e.preventDefault();
    }
  }

  function handToGuard(files) {
    const data = new DataTransfer();
    for (const file of files) data.items.add(file);
    given.add(data.files);
    const event = new DragEvent('redactit-picked', { cancelable: true, dataTransfer: data });
    window.dispatchEvent(event);
    return event.defaultPrevented;
  }

  function tellGuard(type) {
    window.dispatchEvent(new Event(type));
  }

  /**
   * A click that would open the browser's chooser for one of the page's file inputs (the
   * input itself, or a label for it) opens Redactit's instead. The click still reaches
   * the page; only its default action, the chooser, is cancelled, here on `window` before
   * any page listener runs. Inside a closed shadow root the path shows only its host, so
   * the element under the pointer (or, from the keyboard, the focused one) is looked up
   * inside it.
   */
  function onClick(e) {
    if (ours.has(e)) return;
    const input = fileInputFor(clickedElement(e), e.bubbles, e.composed);
    if (!input) return;
    e.preventDefault();
    if (input.disabled) return;
    openChooser({ multiple: input.multiple, directory: input.webkitdirectory, accept: input.accept }, {
      deliver: (files) => setFiles(input, files),
      cancel: () => dispatchOurs(input, new Event('cancel', { bubbles: true })),
    });
  }

  function clickedElement(e) {
    let node = e.composedPath()[0];
    const fromKeyboard = e.detail === 0 && e.clientX === 0 && e.clientY === 0;
    for (let depth = 0; depth < 16; depth += 1) {
      const root = closedRootOf(node);
      const inner = root && (fromKeyboard ? root.activeElement : root.elementFromPoint(e.clientX, e.clientY));
      if (!inner || inner === node) break;
      node = inner;
    }
    return node;
  }

  /**
   * The file input a click on `target` would open, found as the browser finds a click's
   * activation target: the first node on its path with an activation behaviour, where a
   * label stands for its control.
   */
  function fileInputFor(target, goesUp, leavesRoots) {
    for (let node = target; node; node = goesUp ? parentOnPath(node, leavesRoots) : null) {
      if (isFileInput(node)) return node;
      if (node.localName === 'label' && 'control' in node) return isFileInput(node.control) ? node.control : null;
      if (activatesOtherwise(node)) return null;
    }
    return null;
  }

  /** Elements whose own activation comes before any label around them. */
  function activatesOtherwise(node) {
    if (node.localName === 'input') return node.type !== 'hidden';
    if (node.localName === 'button') return true;
    return (node.localName === 'a' || node.localName === 'area') && node.hasAttribute?.('href');
  }

  /** The next node on an event's path: the slot, else the parent, else a shadow root's
   * host when the event leaves the root. */
  function parentOnPath(node, leavesRoots) {
    const slot = node.assignedSlot || closedSlotOf(node);
    if (slot) return slot;
    const parent = node.parentNode;
    if (!parent || parent.nodeType !== Node.DOCUMENT_FRAGMENT_NODE || !parent.host) return parent;
    return leavesRoots ? parent.host : null;
  }

  function closedSlotOf(node) {
    const root = closedRootOf(node.parentNode);
    if (!root) return null;
    for (const slot of root.querySelectorAll('slot')) if (slot.assignedNodes().includes(node)) return slot;
    return null;
  }

  function isFileInput(node) {
    return node?.localName === 'input' && node.type === 'file';
  }

  /** Any shadow root of `el`, open or closed: content scripts may see both. */
  function shadowRootOf(el) {
    try {
      return el?.nodeType === Node.ELEMENT_NODE ? chrome.dom.openOrClosedShadowRoot(el) || null : null;
    } catch {
      return null;
    }
  }

  /** A closed shadow root of `el`: the one kind an event's path does not show from outside. */
  function closedRootOf(el) {
    const root = shadowRootOf(el);
    return root && root !== el.shadowRoot ? root : null;
  }

  /** Defence in depth: a paste or drop that reached the editor without passing our
   * listeners above (it should not happen) is stopped before it is inserted. */
  function onBeforeInput(e) {
    if (!e.isTrusted || !/^insertFrom(Paste|Drop|PasteAsQuotation)$/.test(e.inputType)) return;
    if (e.inputType === 'insertFromDrop' && pageDropInserting && !(e.dataTransfer && carriesFiles(e.dataTransfer))) return;
    stop(e);
    notice.show('block', `Redactit blocked this. ${LOCAL_REASONS.unreadable} Nothing was sent to the site.`);
  }

  function stop(e) {
    e.preventDefault();
    e.stopImmediatePropagation();
  }

  function endPageDrag() {
    pageDrag = null;
    clearTimeout(pageDragTimer);
  }

  /** Files from the computer: the one thing a drag that began in the page cannot carry. */
  function carriesFiles(data) {
    return data.files.length > 0 || [...data.types].includes('Files');
  }

  /** Every type a drag carries with its data, as one string to compare. */
  function dragData(data) {
    return JSON.stringify([...data.types].filter((t) => t !== 'Files').map((t) => [t, data.getData(t)]));
  }

  // --- one paste, drop or pick -------------------------------------------------------------

  /**
   * Redacts every item, then hands the page the ones that came back, in one event. An
   * item that is blocked is left out; the rest still go in. The notice counts what really
   * went in: text the page took is never reported as "nothing was sent".
   */
  async function handle(req) {
    const items = (req.text ? 1 : 0) + req.files.length;
    const what = req.files.length > 1 ? `${req.files.length} files` : req.files.length ? 'this file' : 'this paste';
    const task = { ports: new Set(), what };
    // Shown only if nothing more specific (starting, page N, review) has been said yet.
    const slow = setTimeout(() => task.shown || notice.show('info', `Redactit is checking ${what}…`, task),
      SLOW_NOTICE_MS);
    const blocked = [];
    let passed = 0;
    let text = '';
    const files = [];
    try {
      if (!items) throw new Blocked('unreadable');
      recheckAdapter();
      if (req.text) {
        try {
          text = await redactText(req.text, task);
        } catch (e) {
          blocked.push(asBlocked(e));
        }
      }
      for (const file of req.files) {
        try {
          files.push(await redactFile(file, task));
        } catch (e) {
          blocked.push(asBlocked(e));
        }
      }
      if (text || files.length) {
        const went = insert(req, text, files);
        const failed = (text && !went.text ? 1 : 0) + (files.length && !went.files ? files.length : 0);
        passed = (text ? 1 : 0) + files.length - failed;
        for (let i = 0; i < failed; i += 1) blocked.push(new Blocked('insert_failed'));
      }
    } catch (e) {
      blocked.push(asBlocked(e));
    } finally {
      clearTimeout(slow);
    }
    if (!blocked.length) return notice.hide(task);
    // Insert failures last: the reason a user can act on (a file type, a host) comes first.
    blocked.sort((a, b) => (a.code === 'insert_failed') - (b.code === 'insert_failed'));
    const reason = blocked[0].reason;
    if (passed > 0) {
      const count = items - passed;
      return notice.show('block', `Redactit blocked ${count} of ${items} items. ${reason} The rest were added.`);
    }
    const tail = blocked[0].code === 'insert_failed' ? 'Nothing was added.' : 'Nothing was sent to the site.';
    return notice.show('block', `Redactit blocked ${what}. ${reason} ${tail}`);
  }

  function asBlocked(e) {
    return e instanceof Blocked ? e : new Blocked('extension_error');
  }

  async function redactText(text, task) {
    const out = await redact('text', new Blob([text]), task);
    return decodeText(out.text);
  }

  /** A redacted file under a neutral name: a file's own name can identify someone, and
   * the engine does not check names. */
  async function redactFile(file, task) {
    const kind = kindOf(file);
    if (!kind) throw new Blocked('unsupported');
    if (file.size > MAX_PAYLOAD) throw new Blocked('too_large');
    const out = await redact(kind, file, task);
    const n = (fileNumber += 1);
    if (kind === 'pdf') return new File([out.file], `redacted-${n}.pdf`, { type: 'application/pdf' });
    if (kind === 'image') {
      const ext = out.fileType === 'image/jpeg' ? 'jpg' : 'png';
      return new File([out.file], `redacted-${n}.${ext}`, { type: out.fileType });
    }
    const ext = kind === 'txt' ? extOf(file.name) || 'txt' : 'md'; // Word comes back as Markdown
    decodeText(out.text); // must be UTF-8, like any text we insert
    return new File([out.text], `redacted-${n}.${TEXT_EXT.has(ext) || ext === 'md' ? ext : 'txt'}`,
      { type: kind === 'txt' ? 'text/plain' : 'text/markdown' });
  }

  function kindOf(file) {
    const ext = extOf(file.name);
    const type = (file.type || '').toLowerCase();
    if (ext === 'pdf' || type === 'application/pdf') return 'pdf';
    if (ext === 'docx' || type === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document') return 'docx';
    if (ext === 'md' || ext === 'markdown' || type === 'text/markdown') return 'md';
    if (IMAGE_TYPES[ext] || /^image\/(png|jpeg|webp|bmp|gif|tiff)$/.test(type)) return 'image';
    if (TEXT_EXT.has(ext) || type.startsWith('text/')) return 'txt';
    return null; // SVG, archives, spreadsheets...: nothing checks them, so they never pass
  }

  function extOf(name) {
    const dot = (name || '').lastIndexOf('.');
    return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
  }

  function decodeText(bytes) {
    try {
      return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    } catch {
      throw new Blocked('extension_error');
    }
  }

  // --- talking to the service worker -------------------------------------------------------

  /**
   * One job over a 'redactit/job' port (API in background.js). Resolves only on `done`,
   * with every byte accounted for; anything else rejects with Blocked.
   * @returns {Promise<{file?: Uint8Array, fileType?: string, text?: Uint8Array}>}
   */
  function redact(kind, blob, task) {
    return new Promise((resolve, reject) => {
      let port;
      try {
        port = chrome.runtime.connect({ name: 'redactit/job' });
      } catch {
        reject(new Blocked('reload_page')); // the extension was reloaded under this page
        return;
      }
      task.ports.add(port);
      const size = blob.size;
      const total = Math.max(1, Math.ceil(size / RAW_CHUNK));
      let header = null;
      const chunks = [];
      let settled = false;
      const end = (error, value) => {
        if (settled) return;
        settled = true;
        task.ports.delete(port);
        try {
          port.disconnect();
        } catch {
          // already closed
        }
        if (error) reject(error);
        else resolve(value);
      };
      port.onDisconnect.addListener(() => end(new Blocked('extension_error')));
      port.onMessage.addListener((m) => {
        if (settled || !m || typeof m !== 'object') return;
        if (m.op === 'accepted') sendChunks().catch(() => end(new Blocked('unreadable')));
        else if (m.op === 'held') notice.show('info', `Redactit is starting. ${cap(task.what)} will go in once it has been checked.`, task);
        else if (m.op === 'progress' && m.page) notice.show('info', `Redactit is checking page ${m.page} of ${m.pages}…`, task);
        else if (m.op === 'review') notice.show('review', 'Redactit needs your review: click the Redactit button in the toolbar.', task);
        else if (m.op === 'result') header = m;
        else if (m.op === 'chunk') chunks.push(m.data);
        else if (m.op === 'blocked') end(new Blocked(m.code, m.message));
        else if (m.op === 'done') {
          try {
            end(null, split(header, chunks));
          } catch {
            end(new Blocked('extension_error'));
          }
        }
      });

      async function sendChunks() {
        for (let seq = 0; seq < total && !settled; seq += 1) {
          const data = await toB64(blob.slice(seq * RAW_CHUNK, (seq + 1) * RAW_CHUNK));
          if (!settled) port.postMessage({ op: 'chunk', seq, total, data });
        }
      }

      try {
        port.postMessage({ op: 'start', kind, size, total });
      } catch {
        end(new Blocked('reload_page'));
      }
    });
  }

  /** The result's parts, checked against its header once more before anything is used. */
  function split(header, chunks) {
    if (!header || chunks.length !== header.total) throw new Error('incomplete');
    const bytes = new Uint8Array(header.size);
    let at = 0;
    for (const data of chunks) {
      const piece = fromB64(data);
      if (at + piece.length > header.size) throw new Error('overrun');
      bytes.set(piece, at);
      at += piece.length;
    }
    if (at !== header.size) throw new Error('short');
    const out = {};
    let offset = 0;
    for (const part of header.parts) {
      const slice = bytes.subarray(offset, offset + part.size);
      offset += part.size;
      if (part.name === 'file') Object.assign(out, { file: slice, fileType: part.media_type });
      else if (part.name === 'text') out.text = slice;
    }
    return out;
  }

  function toB64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const url = String(reader.result);
        resolve(url.slice(url.indexOf(',') + 1));
      };
      reader.onerror = () => reject(new Error('read'));
      reader.readAsDataURL(blob);
    });
  }

  function fromB64(data) {
    if (typeof Uint8Array.fromBase64 === 'function') return Uint8Array.fromBase64(data);
    const bin = atob(data);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
    return out;
  }

  // --- handing the result to the page ------------------------------------------------------

  /**
   * Gives the page the redacted text and files the way the user's action would have: a
   * synthetic paste or drop carrying only them. A site that handles pastes itself (most
   * rich editors) takes it from there. If it ignores the event, we do what the browser
   * would have done: insert the text, or put the files in the site's file input.
   * @returns {{text: boolean, files: boolean}} whether the text, and the files, went in
   */
  function insert(req, text, files) {
    const both = (ok) => ({ text: ok, files: ok });
    if (req.how === 'pick') return both(attempt(() => req.deliver(files)));
    if (req.how === 'input') return both(attempt(() => setFiles(req.target, files)));
    let target = req.target instanceof Element && req.target.isConnected ? req.target : null;
    if (req.how === 'paste') target = target || composer();
    if (!target) return both(false);
    const data = new DataTransfer();
    if (text) data.setData('text/plain', text);
    for (const file of files) data.items.add(file);
    restoreCaret(target, req.caret);
    const init = { bubbles: true, cancelable: true, composed: true };
    const event = req.how === 'paste'
      ? new ClipboardEvent('paste', { ...init, clipboardData: data })
      : new DragEvent('drop', { ...init, dataTransfer: data, clientX: req.point[0], clientY: req.point[1] });
    ours.add(event);
    target.dispatchEvent(event);
    if (event.defaultPrevented) return both(true);
    return {
      text: !text || attempt(() => insertText(editableOf(target) || composer(), text)),
      files: !files.length || attempt(() => setFiles(fileInput(), files)),
    };
  }

  /** `step()`'s answer, with a throw counted as false: one failed step leaves the other. */
  function attempt(step) {
    try {
      return step() === true;
    } catch {
      return false;
    }
  }

  function insertText(el, text) {
    if (!el) return false;
    el.focus();
    if (isTextField(el)) {
      const start = el.selectionStart ?? el.value.length;
      el.setRangeText(text, start, el.selectionEnd ?? start, 'end');
      const event = new InputEvent('input', { bubbles: true, composed: true, inputType: 'insertFromPaste', data: text });
      ours.add(event);
      el.dispatchEvent(event);
      return true;
    }
    // execCommand is deprecated, but it is still the one way to insert into a
    // contenteditable that every editor (ProseMirror, Quill) sees as typing.
    return el.isContentEditable && document.execCommand('insertText', false, text);
  }

  function setFiles(input, files) {
    if (!input || !files.length) return false;
    const data = new DataTransfer();
    for (const file of files) data.items.add(file);
    input.files = data.files;
    given.add(input.files);
    for (const type of ['input', 'change']) {
      dispatchOurs(input, new Event(type, { bubbles: true, composed: type === 'input' }));
    }
    return true;
  }

  function dispatchOurs(target, event) {
    ours.add(event);
    target.dispatchEvent(event);
  }

  function isTextField(el) {
    return el instanceof HTMLTextAreaElement || (el instanceof HTMLInputElement && TEXT_INPUTS.has(el.type));
  }

  function editableOf(node) {
    const el = node instanceof Element ? node : node?.parentElement;
    if (!el) return null;
    if (isTextField(el)) return el;
    if (!el.isContentEditable) return null;
    let host = el;
    while (host.parentElement?.isContentEditable) host = host.parentElement;
    return host;
  }

  function caretOf(el) {
    if (!el) return null;
    if (isTextField(el)) return { start: el.selectionStart, end: el.selectionEnd };
    const sel = document.getSelection();
    return sel && sel.rangeCount && el.contains(sel.anchorNode) ? { range: sel.getRangeAt(0).cloneRange() } : null;
  }

  function restoreCaret(el, caret) {
    if (!caret) return;
    try {
      el.focus();
      if (caret.range) {
        const sel = document.getSelection();
        sel.removeAllRanges();
        sel.addRange(caret.range);
      } else if (isTextField(editableOf(el)) && caret.start != null) {
        el.setSelectionRange(caret.start, caret.end);
      }
    } catch {
      // the page re-rendered; the editor's own selection is used
    }
  }

  // --- the site adapter --------------------------------------------------------------------

  function query(selectors) {
    for (const selector of selectors || []) {
      try {
        const el = document.querySelector(selector);
        if (el) return el;
      } catch {
        // a malformed selector counts as missing
      }
    }
    return null;
  }

  function composer() {
    return adapter && adapterActive ? query(adapter.composer) : null;
  }

  function fileInput() {
    return adapter && adapterActive ? query(adapter.fileInput) : null;
  }

  function report() {
    if (window !== window.top) return;
    try {
      chrome.runtime.sendMessage({ type: 'redactit/page', adapter: adapter?.name ?? null, active: adapterActive })
        .catch(() => {});
    } catch {
      // the extension was reloaded under this page
    }
  }

  /**
   * The adapter's self-check: its composer must appear within ADAPTER_CHECK_MS of the
   * page loading. If it does not, the adapter is off and only the generic handling runs;
   * pastes and files are still intercepted, redacted or blocked.
   */
  function selfCheck() {
    if (!adapter) {
      adapterActive = false;
      report();
      return;
    }
    let observer = null;
    let timer = null;
    let pending = false;
    const done = (ok) => {
      observer?.disconnect();
      clearTimeout(timer);
      adapterActive = ok;
      report();
    };
    if (query(adapter.composer)) return done(true);
    observer = new MutationObserver(() => {
      if (pending) return;
      pending = true;
      setTimeout(() => { // at most a few checks a second on a busy page
        pending = false;
        if (adapterActive === null && query(adapter.composer)) done(true);
      }, 250);
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    timer = setTimeout(() => done(Boolean(query(adapter.composer))), ADAPTER_CHECK_MS);
    return undefined;
  }

  /** A single-page site can show its composer later (after sign-in, say). */
  function recheckAdapter() {
    if (adapter && adapterActive === false && query(adapter.composer)) {
      adapterActive = true;
      report();
    }
  }

  // --- attaching the side panel's copy ----------------------------------------------------

  /** What the worker may hand over, and the extension each keeps (never one from a name). */
  const INSERT_TYPES = { 'application/pdf': 'pdf', 'image/png': 'png', 'image/jpeg': 'jpg',
    'text/markdown': 'md', 'text/plain': 'txt' };

  /**
   * The side panel's "Attach to chat": the service worker sends the copy it got from the
   * host and checked (redactit/attach in background.js), and it goes in as a redacted drop
   * would, on the composer, else into the site's file input, under a neutral name. Only
   * the worker can send this (a page cannot reach chrome.runtime), and only in the top
   * frame. Without a working adapter there is no known composer, so it is refused.
   */
  function onInsert(msg, sender, reply) {
    if (!msg || msg.type !== 'redactit/insert' || sender.id !== chrome.runtime.id || sender.tab) return false;
    const ext = INSERT_TYPES[msg.media_type];
    let ok = false;
    try {
      recheckAdapter();
      const target = composer();
      if (ext && typeof msg.data === 'string' && adapter && adapterActive && target) {
        const file = new File([fromB64(msg.data)], `redacted-${(fileNumber += 1)}.${ext}`, { type: msg.media_type });
        ok = insert({ how: 'drop', target, point: centre(target), caret: null }, '', [file]).files;
      }
    } catch {
      ok = false;
    }
    reply(ok ? { ok: true } : { ok: false, code: 'insert_failed' });
    return false;
  }

  function centre(el) {
    const box = el.getBoundingClientRect();
    return [box.left + box.width / 2, box.top + box.height / 2];
  }

  if (window === window.top) {
    try {
      chrome.runtime.onMessage.addListener(onInsert);
    } catch {
      // the extension was reloaded under this page
    }
  }

  report(); // at load: starts the host early when "Keep Redactit ready" is on
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', selfCheck, { once: true });
  else selfCheck();

  // --- the in-page notice ------------------------------------------------------------------

  /**
   * One notice per frame, in a closed shadow root: the site's scripts can see that it
   * exists, but cannot read or change what it says. It never shows content, only status.
   */
  const notice = (() => {
    let host = null;
    let text = null;
    let cancel = null;
    let owner = null;
    let timer = null;
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(`
      :host { all: initial; position: fixed; z-index: 2147483647; right: 16px; bottom: 16px; max-width: 380px; }
      .box { font: 14px/1.4 system-ui, sans-serif; color: #1b1b1f; background: #fff; border: 1px solid #c9c9d1;
             border-left: 4px solid #3b6fd8; border-radius: 8px; padding: 10px 12px; display: flex; gap: 10px;
             align-items: flex-start; box-shadow: 0 4px 16px rgba(0,0,0,.18); }
      .box.block { border-left-color: #c62828; }
      .box.review { border-left-color: #b26a00; }
      .msg { flex: 1; }
      button { font: inherit; background: none; border: 1px solid #c9c9d1; border-radius: 6px; padding: 2px 8px;
               cursor: pointer; color: inherit; }
      @media (prefers-color-scheme: dark) {
        .box { color: #ececf1; background: #26262b; border-color: #4a4a52; }
        button { border-color: #4a4a52; }
      }
    `);

    function build() {
      host = document.createElement('div');
      const root = host.attachShadow({ mode: 'closed' });
      root.adoptedStyleSheets = [sheet]; // not an inline <style>: a site's CSP cannot block it
      const box = document.createElement('div');
      box.className = 'box';
      box.setAttribute('role', 'status');
      text = document.createElement('div');
      text.className = 'msg';
      cancel = document.createElement('button');
      cancel.textContent = 'Cancel';
      cancel.addEventListener('click', () => {
        for (const port of owner?.ports || []) {
          try {
            port.postMessage({ op: 'cancel' });
          } catch {
            // already finished
          }
        }
      });
      const close = document.createElement('button');
      close.textContent = 'Dismiss';
      close.addEventListener('click', () => hide());
      box.append(text, cancel, close);
      root.append(box);
    }

    /** kind: 'info' | 'review' | 'block'. A task keeps the notice up until it ends and
     * gets a Cancel button; a block notice stays 15 s. */
    function show(kind, message, task) {
      if (!host) build();
      if (!host.isConnected) (document.body || document.documentElement).append(host);
      clearTimeout(timer);
      owner = task || null;
      if (task) task.shown = true;
      text.textContent = message;
      text.parentElement.className = `box ${kind}`;
      cancel.hidden = !task;
      if (!task) timer = setTimeout(() => hide(), 15_000);
    }

    function hide(task) {
      if (task && owner !== task) return; // a newer notice belongs to something else
      clearTimeout(timer);
      owner = null;
      host?.remove();
    }

    return { show, hide };
  })();

  function cap(s) {
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
})();
