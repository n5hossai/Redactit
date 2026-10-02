// A scripted native host for the side panel's browser tests, put in place of
// chrome.runtime.connectNative inside the service worker (browserkit.Browser.stub_host).
//
// Why: on Windows Chromium finds native hosts only in the registry, so tests that need a
// host skip there unless a machine opts in. With this stub the worker's own code still
// runs in full (framing, holds while warming, review, result checks, fail closed); only
// the process behind the port is replaced. It speaks src/redactit/hosts/native.py's
// frames, and the worker checks every one of them, so a drift fails as a `protocol` block.
// The real host is still covered by test_round_trip.py and by test_panel.py's real-host test.
//
// Modes:
//   redact  says `warming` for options.warmMs, then `ready-text` and `ready-all`; answers
//           each request with `queued`, `redacting`, PDF pages 1..options.pages (one every
//           options.pageMs), then a fixed result: text with options.replace applied, and
//           options.files[kind] (base64) for a PDF's file part or an image.
//   hang    ready at once; takes requests, reports `queued` and `redacting`, never answers.
//
// Every frame from the worker is logged in self.__stubFrames as {type, id}, never its data.
(mode, options) => {
  const RAW_CHUNK = 384 * 1024;
  const opts = { warmMs: 0, pages: 2, pageMs: 200, replace: {}, files: {}, markdown: '# Page 1\n', ...options };
  const frames = (self.__stubFrames = []);

  const fromB64 = (data) => Uint8Array.from(atob(data), (c) => c.charCodeAt(0));
  const toB64 = (bytes) => {
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  };
  const utf8 = (text) => new TextEncoder().encode(text);

  function open() {
    const listeners = { message: [], disconnect: [] };
    const requests = new Map();
    let connected = true;
    let queue = Promise.resolve();

    /** Frames go out in order, each after its delay, as the real host's stdout would. */
    const send = (msg, delay = 0, rid = null) => {
      queue = queue.then(() => new Promise((r) => setTimeout(r, delay))).then(() => {
        if (!connected || (rid && requests.get(rid)?.cancelled)) return;
        for (const fn of listeners.message) fn(msg);
      });
    };
    const status = (state, id = null) => ({ type: 'status', id, state, version: '0.1.0', protocol: 1 });

    function answer(rid) {
      const req = requests.get(rid);
      send({ type: 'progress', id: rid, stage: 'queued' }, 0, rid);
      send({ type: 'progress', id: rid, stage: 'redacting' }, opts.pageMs, rid);
      if (mode === 'hang') return;
      if (req.kind === 'pdf') {
        for (let page = 1; page <= opts.pages; page += 1) {
          send({ type: 'progress', id: rid, stage: 'redacting', page, pages: opts.pages }, opts.pageMs, rid);
        }
      }
      let parts;
      if (req.kind === 'pdf') {
        parts = [['file', 'application/pdf', fromB64(opts.files.pdf)], ['text', 'text/markdown', utf8(opts.markdown)]];
      } else if (req.kind === 'image') {
        parts = [['file', 'image/png', fromB64(opts.files.image)]];
      } else {
        const input = new Uint8Array(req.chunks.flatMap((c) => [...fromB64(c)]));
        let text = new TextDecoder().decode(input);
        for (const [raw, token] of Object.entries(opts.replace)) text = text.split(raw).join(token);
        const media = req.kind === 'text' || req.kind === 'txt' ? 'text/plain' : 'text/markdown';
        parts = [['text', media, utf8(text)]];
      }
      const bytes = new Uint8Array(parts.reduce((n, p) => n + p[2].length, 0));
      let at = 0;
      for (const p of parts) {
        bytes.set(p[2], at);
        at += p[2].length;
      }
      const total = Math.max(1, Math.ceil(bytes.length / RAW_CHUNK));
      send({ type: 'result', id: rid, size: bytes.length, total,
        parts: parts.map(([name, media_type, b]) => ({ name, media_type, size: b.length })) }, opts.pageMs, rid);
      for (let seq = 0; seq < total; seq += 1) {
        send({ type: 'chunk', id: rid, seq, total, data: toB64(bytes.subarray(seq * RAW_CHUNK, (seq + 1) * RAW_CHUNK)) },
          0, rid);
      }
    }

    const port = {
      name: 'stub',
      onMessage: { addListener: (fn) => listeners.message.push(fn) },
      onDisconnect: { addListener: (fn) => listeners.disconnect.push(fn) },
      postMessage(msg) {
        frames.push({ type: msg.type, id: msg.id ?? null });
        if (msg.type === 'redact_text' || msg.type === 'redact_file') {
          requests.set(msg.id, { kind: msg.kind || 'text', total: msg.total, chunks: [], cancelled: false });
        } else if (msg.type === 'chunk') {
          const req = requests.get(msg.id);
          req.chunks.push(msg.data);
          if (msg.seq + 1 === req.total) answer(msg.id);
        } else if (msg.type === 'cancel') {
          const req = requests.get(msg.id);
          if (req) req.cancelled = true;
        } else if (msg.type === 'ping') {
          send(status('ready-all', msg.id));
        }
      },
      disconnect() {
        connected = false;
      },
    };
    if (mode === 'redact' && opts.warmMs) {
      send(status('warming'));
      send(status('ready-text'), opts.warmMs);
      send(status('ready-all'));
    } else {
      send(status('ready-text'));
      send(status('ready-all'));
    }
    return port;
  }

  chrome.runtime.connectNative = () => open();
  return true;
}
