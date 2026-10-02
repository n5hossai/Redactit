/**
 * Redactit's interception on the AI sites, shared by every adapter (PLAN §7).
 *
 * Runs at document_start, before any of the site's scripts, so its capture-phase listeners
 * on `window` are the first to see a paste, a drop, or a file picked in a file input
 * (THREAT_MODEL T5). Each one is cancelled before the site sees it; its content goes to
 * the service worker, and only the redacted result comes back into the page: as a
 * synthetic event of the same kind, which the site handles as it would the user's, or,
 * if the site ignores that, inserted directly.
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

  // A test build may rewrite this line: tests/e2e/build.py.
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
  /** A drag that started inside this page carries only what the page already shows. */
  let dragFromPage = false;
  let fileNumber = 0;

  window.addEventListener('paste', onPaste, true);
  window.addEventListener('drop', onDrop, true);
  window.addEventListener('input', onFileInput, true);
  window.addEventListener('change', onFileInput, true);
  window.addEventListener('beforeinput', onBeforeInput, true);
  window.addEventListener('dragstart', (e) => { if (e.isTrusted) dragFromPage = true; }, true);
  window.addEventListener('dragend', () => { dragFromPage = false; }, true);

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
    if (dragFromPage) {
      dragFromPage = false;
      return;
    }
    const data = e.dataTransfer;
    const files = [...data.files];
    const text = files.length ? '' : data.getData('text/plain');
    if (!files.length && !text && !data.types.length) return;
    stop(e);
    const target = e.composedPath()[0];
    handle({ how: 'drop', target, point: [e.clientX, e.clientY], caret: null, text, files });
  }

  /**
   * A file picked in a file input. Chrome fires `input` then `change`; whichever comes
   * first takes the files and empties the input, so no later listener, and no script
   * reading `input.files`, ever sees the originals.
   */
  function onFileInput(e) {
    const input = e.composedPath()[0];
    if (!(input instanceof HTMLInputElement) || input.type !== 'file') return;
    if (!e.isTrusted || ours.has(e)) return;
    e.stopImmediatePropagation();
    const files = input.files ? [...input.files] : [];
    if (!files.length) return;
    input.value = '';
    handle({ how: 'input', target: input, caret: null, text: '', files });
  }

  /** Defence in depth: a paste or drop that reached the editor without passing our
   * listeners above (it should not happen) is stopped before it is inserted. */
  function onBeforeInput(e) {
    if (!e.isTrusted || !/^insertFrom(Paste|Drop|PasteAsQuotation)$/.test(e.inputType)) return;
    if (e.inputType === 'insertFromDrop' && dragFromPage) return;
    stop(e);
    notice.show('block', `Redactit blocked this. ${LOCAL_REASONS.unreadable} Nothing was sent to the site.`);
  }

  function stop(e) {
    e.preventDefault();
    e.stopImmediatePropagation();
  }

  // --- one paste, drop or pick -------------------------------------------------------------

  /**
   * Redacts every item, then hands the page the ones that came back, in one event. An
   * item that is blocked is left out; the rest still go in.
   */
  async function handle(req) {
    const items = (req.text ? 1 : 0) + req.files.length;
    const what = req.files.length > 1 ? `${req.files.length} files` : req.files.length ? 'this file' : 'this paste';
    const task = { ports: new Set(), what };
    // Shown only if nothing more specific (starting, page N, review) has been said yet.
    const slow = setTimeout(() => task.shown || notice.show('info', `Redactit is checking ${what}…`, task),
      SLOW_NOTICE_MS);
    const blocked = [];
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
      if ((text || files.length) && !insert(req, text, files)) throw new Blocked('insert_failed');
    } catch (e) {
      blocked.splice(0, blocked.length, asBlocked(e));
    } finally {
      clearTimeout(slow);
    }
    if (!blocked.length) return notice.hide(task);
    const reason = blocked[0].reason;
    const passed = items - blocked.length;
    if (passed > 0 && blocked[0].code !== 'insert_failed') {
      return notice.show('block', `Redactit blocked ${blocked.length} of ${items} items. ${reason} The rest were added.`);
    }
    return notice.show('block', `Redactit blocked ${what}. ${reason} Nothing was sent to the site.`);
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
   */
  function insert(req, text, files) {
    if (req.how === 'input') return setFiles(req.target, files);
    let target = req.target instanceof Element && req.target.isConnected ? req.target : null;
    if (req.how === 'paste') target = target || composer();
    if (!target) return false;
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
    if (event.defaultPrevented) return true;
    let ok = true;
    if (text) ok = insertText(editableOf(target) || composer(), text);
    if (files.length) ok = setFiles(fileInput(), files) && ok;
    return ok;
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
    for (const type of ['input', 'change']) {
      const event = new Event(type, { bubbles: true, composed: type === 'input' });
      ours.add(event);
      input.dispatchEvent(event);
    }
    return true;
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
