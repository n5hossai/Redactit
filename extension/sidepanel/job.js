/**
 * One file through the service worker's job port (`redactit/job`, API at the top of
 * background.js), from the side panel. The request goes out in the host's chunk framing;
 * the result is used only after `done`, with every byte accounted for against its header,
 * the same rule the content scripts follow. Anything else ends as a block with the
 * worker's fixed text, so nothing half-checked is ever shown as a result.
 */

const RAW_CHUNK = 384 * 1024; // the host's chunk size (src/redactit/hosts/native.py)
export const MAX_PAYLOAD = 64 * 1024 * 1024;

const TEXT_EXT = new Set(['txt', 'text', 'csv', 'tsv', 'log', 'json', 'xml', 'yaml', 'yml', 'ini', 'cfg', 'toml',
  'html', 'htm', 'css', 'js', 'ts', 'py', 'java', 'c', 'h', 'cpp', 'cs', 'go', 'rs', 'rb', 'php', 'sh', 'sql']);
const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif', 'tif', 'tiff']);

/** Panel-side texts for blocks that never reach the worker. */
export const LOCAL_REASONS = {
  unsupported: 'Redactit cannot check this kind of file. Use a PDF, Word, text or Markdown file, or an image.',
  too_large: 'This is larger than 64 MiB, the most Redactit checks at once.',
  unreadable: 'Redactit could not read this file.',
  no_chat: 'Open a chat on claude.ai, chatgpt.com or gemini.google.com in this window first: pseudonyms are kept per chat.',
  extension_error: 'Redactit hit an internal error.',
  reload: 'Redactit was updated or restarted. Close and reopen this panel.',
};

/** The worker's kind for a file, as intercept.js decides it, so the panel and a drop on the
 * page treat the same file the same way. null: nothing checks it, so it never passes. */
export function kindOf(file) {
  const ext = extOf(file.name);
  const type = (file.type || '').toLowerCase();
  if (ext === 'pdf' || type === 'application/pdf') return 'pdf';
  if (ext === 'docx' || type === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document') return 'docx';
  if (ext === 'md' || ext === 'markdown' || type === 'text/markdown') return 'md';
  if (IMAGE_EXT.has(ext) || /^image\/(png|jpeg|webp|bmp|gif|tiff)$/.test(type)) return 'image';
  if (TEXT_EXT.has(ext) || type.startsWith('text/')) return 'txt';
  return null;
}

export function extOf(name) {
  const dot = (name || '').lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
}

export class Blocked extends Error {
  /** @param {string} code  @param {string} [message] fixed text for the user */
  constructor(code, message) {
    super(code);
    this.code = code;
    this.reason = message || LOCAL_REASONS[code] || LOCAL_REASONS.extension_error;
  }
}

/**
 * @typedef {object} JobEvents
 * @property {(job: string) => void} [accepted]
 * @property {(sent: number, total: number) => void} [upload]
 * @property {(state: string) => void} [held]
 * @property {(stage: string, page?: number, pages?: number) => void} [progress]
 * @property {() => void} [review]
 * @property {(received: number, total: number) => void} [receive]
 */

/**
 * @typedef {object} JobResult
 * @property {Uint8Array} [file]
 * @property {string} [fileType]
 * @property {Uint8Array} [text]
 * @property {string} [textType]
 */

/**
 * Starts one job. `cancel()` drops it here and at the host (the worker answers with a
 * `cancelled` block); closing the panel does the same, because the port closes.
 * @param {Blob} blob
 * @param {string} kind
 * @param {number} tabId   the chat the request is for: its site and pseudonym scope
 * @param {JobEvents} on
 * @returns {{cancel: () => void, done: Promise<JobResult>}}
 */
export function runJob(blob, kind, tabId, on) {
  let port = null;
  let cancel = () => {};
  const done = new Promise((resolve, reject) => {
    try {
      port = chrome.runtime.connect({ name: 'redactit/job' });
    } catch {
      reject(new Blocked('reload'));
      return;
    }
    const size = blob.size;
    const total = Math.max(1, Math.ceil(size / RAW_CHUNK));
    let header = null;
    const chunks = [];
    let received = 0;
    let settled = false;
    const end = (error, value) => {
      if (settled) return;
      settled = true;
      try {
        port.disconnect();
      } catch {
        // already closed
      }
      if (error) reject(error);
      else resolve(value);
    };
    cancel = () => {
      if (settled) return;
      const cancelled = new Blocked('cancelled', 'Cancelled.');
      try {
        port.postMessage({ op: 'cancel' });
      } catch {
        end(cancelled);
        return;
      }
      // The worker answers with its own `cancelled` block. A cancel that lands before the
      // job exists gets no answer, so the port is closed here, which drops it too.
      setTimeout(() => end(cancelled), 1500);
    };
    port.onDisconnect.addListener(() => end(new Blocked('extension_error')));
    port.onMessage.addListener((m) => {
      if (settled || !m || typeof m !== 'object') return;
      if (m.op === 'accepted') {
        on.accepted?.(m.job);
        sendChunks().catch(() => end(new Blocked('unreadable')));
      } else if (m.op === 'held') on.held?.(m.state);
      else if (m.op === 'progress') on.progress?.(m.stage, m.page, m.pages);
      else if (m.op === 'review') on.review?.();
      else if (m.op === 'result') {
        header = m;
        on.receive?.(0, m.total);
      } else if (m.op === 'chunk') {
        chunks.push(m.data);
        received += 1;
        if (header) on.receive?.(received, header.total);
      } else if (m.op === 'blocked') end(new Blocked(m.code, m.message));
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
        if (settled) return;
        port.postMessage({ op: 'chunk', seq, total, data });
        on.upload?.(seq + 1, total);
      }
    }

    try {
      port.postMessage({ op: 'start', kind, size, total, tabId });
    } catch {
      end(new Blocked('reload'));
    }
  });
  return { cancel: () => cancel(), done };
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
  /** @type {JobResult} */
  const out = {};
  let offset = 0;
  for (const part of header.parts) {
    const slice = bytes.subarray(offset, offset + part.size);
    offset += part.size;
    if (part.name === 'file') Object.assign(out, { file: slice, fileType: part.media_type });
    else if (part.name === 'text') Object.assign(out, { text: slice, textType: part.media_type });
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
  const bin = atob(data);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
  return out;
}
