/**
 * Redactit service worker: the only path between the AI sites and the native host.
 *
 * Content scripts hand it what the user pasted, dropped or picked; it sends that to the
 * host (`com.redactit.host`, src/redactit/hosts/native.py) in the host's chunked frames and
 * hands back only what the host redacted. It FAILS CLOSED (THREAT_MODEL T7): a missing,
 * stopped, silent or confused host, or an engine error, ends the request with `blocked`,
 * and nothing unredacted is ever handed back. It logs nothing and stores no content:
 * chrome.storage holds one setting and the chat-scope aliases (URL paths), never input.
 *
 * ## Message API (content scripts and the side panel)
 *
 * ### Jobs: one port per paste or file, `chrome.runtime.connect({name: 'redactit/job'})`
 *
 * Client to worker:
 *   {op: 'start', kind, size, total, tabId?}
 *       kind: 'text' (a paste) or 'txt' | 'md' | 'docx' | 'pdf' | 'image' (a file).
 *       size: payload bytes (text as UTF-8), at most 64 MiB. total: chunkCount(size).
 *       tabId: extension pages only (the side panel): the tab whose site and chat the
 *       request is for. Content scripts never send it; their own tab is used.
 *       Send chunks only after `accepted`.
 *   {op: 'chunk', seq, total, data}
 *       data: base64 of payload bytes [seq * RAW_CHUNK, (seq + 1) * RAW_CHUNK), every chunk
 *       full but the last; seq from 0. The same framing as the host's.
 *   {op: 'cancel'}  or closing the port: the request is dropped, at the host too.
 *
 * Worker to client, in this order:
 *   {op: 'accepted', job}             job: the id that redactit/cancel and reviews use
 *   {op: 'held', state}               the host is starting; the request waits (30 s at most)
 *   {op: 'progress', stage, page?, pages?}   from the host: 'queued', 'redacting', PDF pages
 *   {op: 'review'}                    held for review in the side panel (pages only; see Review)
 *   {op: 'result', size, total, parts}       parts: [{name: 'file'|'text', media_type, size}],
 *                                     concatenated in that order into `size` bytes
 *   {op: 'chunk', seq, total, data}   the result, framed like the request
 *   {op: 'done'}                      the result is complete and checked; only now use it
 *   {op: 'blocked', code, message}    final; message is fixed text for the user and never
 *                                     quotes the input. The port then closes.
 *   Results by kind: text/txt -> text (text/plain); md/docx -> text (text/markdown);
 *   pdf -> file (application/pdf) + text (text/markdown); image -> file (image/png|jpeg).
 *
 * ### One-shot messages: `chrome.runtime.sendMessage({type, ...})`
 *
 *   redactit/status {tabId?, probe?}  -> Status (below). Anyone in the extension. probe:
 *       true pings the host first; one that does not answer within 5 s is disconnected.
 *   redactit/start-host            -> Status, after starting the host if it is down.
 *   redactit/restart-host          -> Status. Extension pages only. Fails open jobs.
 *   redactit/cancel {job}          -> {ok}. Extension pages only.
 *   redactit/redact-text {text, tabId}  -> {ok: true, text} | {ok: false, code, message}.
 *       Extension pages only; for "Redact & copy". At most 8 MiB of UTF-8; use a job port
 *       beyond that. Never held for review: the panel is the reviewer.
 *   redactit/review-list          -> [Review]. Extension pages only.
 *   redactit/review-get {job}      -> Review & {text?}: the redacted text part, if any.
 *   redactit/review-decide {job, approve}  -> {ok}. approve=true hands the result to the
 *       page; false blocks it. Extension pages only, so a page cannot approve itself.
 *   redactit/page {adapter, active}     content scripts only: reports the site adapter's
 *       self-check (active: null while checking) and starts the host when keepReady is on.
 *
 *   Status: {state: 'down'|'starting'|'warming'|'ready-text'|'ready-all'|'unavailable',
 *            version, error: {code, message} | null, jobs, reviews, settings: {keepReady},
 *            reviewMode: 'always'|'low_confidence'|'off'|null (the host's policy; null
 *            until the host has loaded it), adapter?: {name, active} for tabId,
 *            recent: [{kind, site, held, code}]}
 *   Review: {job, site, kind, parts, createdAt, expiresAt, reason: 'always'|'low_confidence'}
 *       A page's result is held for review when the host's policy says so: always, or
 *       with low_confidence when the engine marked any decision for review. Never with
 *       'off'. A result requested by an extension page is never held.
 *
 * ### Events for the side panel: `chrome.runtime.connect({name: 'redactit/events'})`
 *
 *   {event: 'status', status} on every state change, and {event: 'reviews', reviews} when
 *   the review queue changes; both are sent once on connect.
 *
 * ### Settings: chrome.storage.local (the side panel writes them; this worker follows)
 *
 *   keepReady: boolean (default false): "Keep Redactit ready" starts the host when an AI
 *              site loads, not on the first paste (speed plan decision 1).
 *   Review mode is not a setting: it is the policy the host loaded (policy.review.mode).
 */
'use strict';

// Settings a test build may rewrite: tests/e2e/browserkit.py replaces these exact lines in its copy.
const HOST_NAME = 'com.redactit.host';
const WARM_HOLD_MS = 30_000;

/** Silence from the host, per request, before it is blocked. Files get longer: one large
 * image is a single host step with no progress in between. */
const SILENCE_MS = { text: 120_000, file: 600_000 };
const REVIEW_TIMEOUT_MS = 10 * 60_000;
/** Reconnect delays after an unexpected disconnect, only with keepReady on. */
const RECONNECT_MS = [1_000, 2_000, 5_000, 15_000, 60_000];
/** The host idles out after 30 minutes; a disconnect after this much quiet is that exit,
 * and is not undone by reconnecting. */
const IDLE_EXIT_MS = 25 * 60_000;

// The host's protocol (src/redactit/hosts/native.py). These must match it exactly.
const PROTOCOL = 2; // 2: results carry a review count, and status the policy's review mode
const CHUNK = 512 * 1024; // base64 characters per chunk
const RAW_CHUNK = (CHUNK / 4) * 3; // the payload bytes they carry (384 KiB)
const MAX_PAYLOAD = 64 * 1024 * 1024;
const MAX_JOBS = 16; // the host's MAX_IN_FLIGHT: one more would be refused as busy
const KINDS = ['txt', 'md', 'docx', 'pdf', 'image'];
const NEEDS_IMAGES = new Set(['pdf', 'image']); // these wait for OCR, as at the host
const HOST_STATES = ['warming', 'ready-text', 'ready-all', 'unavailable'];
/** The policy's review mode as the host reports it ('off' is reserved: no policy sets it yet). */
const REVIEW_MODES = ['always', 'low_confidence', 'off'];
const MAX_PANEL_TEXT = 8 * 1024 * 1024;
const FRAMING_FAULTS = new Set(['bad_frame', 'message_too_large', 'bad_json', 'bad_message', 'bad_sequence',
  'unknown_request', 'duplicate_request']);

/** The parts each kind may come back as. Anything else is a protocol error. */
const RESULT_SHAPES = {
  text: [[['text', 'text/plain']]],
  txt: [[['text', 'text/plain']]],
  md: [[['text', 'text/markdown']]],
  docx: [[['text', 'text/markdown']]],
  pdf: [[['file', 'application/pdf'], ['text', 'text/markdown']]],
  image: [[['file', 'image/png']], [['file', 'image/jpeg']]],
};

/** Allowed sites and where each keeps its chat id (for the pseudonym scope, PLAN §6). */
const SITES = {
  'claude.ai': /^\/chat\/([A-Za-z0-9-]{8,64})(?:\/|$)/,
  'chatgpt.com': /^\/(?:g\/[A-Za-z0-9-]{1,128}\/)?c\/([A-Za-z0-9-]{8,64})(?:\/|$)/,
  'gemini.google.com': /^\/(?:u\/\d{1,2}\/)?app\/([A-Za-z0-9]{8,64})(?:\/|$)/,
};

/** What the user is told, per code. Fixed text: never the input, never a host string
 * except the two codes whose host messages are written for users (see `reasonFor`). */
const REASONS = {
  host_missing: "Redactit's app is not installed on this computer, or not set up for this browser.",
  host_forbidden: "Redactit's app refused this extension. Reinstall Redactit.",
  host_refused: "Redactit's app refused this extension. Reinstall Redactit.",
  host_config: "Redactit's app is not installed correctly. Reinstall Redactit.",
  host_exited: "Redactit's app stopped unexpectedly.",
  host_down: "Redactit's app is not running.",
  host_timeout: "Redactit's app stopped responding.",
  host_incompatible: "Redactit's app and this extension are different versions. Update both to the same release.",
  protocol: "Redactit's app sent something the extension could not check.",
  warming_timeout: `Redactit's app took more than ${Math.round(WARM_HOLD_MS / 1000)} seconds to start. Try again.`,
  engine_unavailable: "Redactit's redaction engine could not start.",
  detection_timeout: 'Redactit could not finish checking this.',
  bad_input: 'Redactit could not read this file.',
  too_large: 'This is larger than 64 MiB, the most Redactit checks at once.',
  busy: 'Too many pastes or files are waiting. Try again in a moment.',
  unsupported: 'Redactit cannot check this kind of file.',
  unreadable: 'Redactit could not read what was pasted or dropped.',
  not_allowed_site: 'Redactit only works on claude.ai, chatgpt.com and gemini.google.com.',
  review_rejected: 'The redacted version was rejected in review.',
  review_timeout: 'The review was not finished in time.',
  cancelled: 'Cancelled.',
  insert_failed: 'Redactit could not hand the redacted version to this page.',
  extension_error: 'Redactit hit an internal error.',
  internal: 'Redactit hit an internal error.',
};

// --- settings ----------------------------------------------------------------------------

const DEFAULT_SETTINGS = Object.freeze({ keepReady: false });
/** @type {{keepReady: boolean}} */
let settings = { ...DEFAULT_SETTINGS };

/** Review mode is deliberately not a setting: it follows the policy the host loaded, so a
 * user cannot switch review off here when the admin's policy asks for it. */
function readSettings(raw) {
  return { keepReady: raw.keepReady === true };
}

/** Loaded before any job starts, so a woken worker knows whether to start the host early. */
const settingsLoaded = chrome.storage.local.get(DEFAULT_SETTINGS)
  .then((raw) => { settings = readSettings(raw); })
  .catch(() => {});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== 'local' || !changes.keepReady) return;
  chrome.storage.local.get(DEFAULT_SETTINGS).then((raw) => {
    settings = readSettings(raw);
    broadcastStatus();
  }).catch(() => {});
});

// The toolbar button opens the side panel: chrome.sidePanel.open() needs a user gesture in
// extension code, so a page asking for review tells the user to click it (PLAN §7).
chrome.sidePanel?.setPanelBehavior?.({ openPanelOnActionClick: true })?.catch?.(() => {});

// --- the native host ---------------------------------------------------------------------

/** One connection at a time. `state` adds 'down' and 'starting' to the host's own states. */
const host = {
  /** @type {chrome.runtime.Port|null} */ port: null,
  state: 'down',
  version: null,
  /** @type {'always'|'low_confidence'|'off'|null} */ reviewMode: null, // from the host's policy
  /** @type {{code: string, message: string}|null} */ error: null,
  engineMessage: null, // the host's explanation when the engine could not start
  imagesFailed: false, // OCR could not start: PDFs and images are answered with an error
  pendingCode: null, // a refusal the host announced just before exiting
  lastFrameAt: 0,
  lastWorkAt: 0,
  attempt: 0,
  /** @type {ReturnType<typeof setTimeout>|null} */ timer: null,
};

/** Starts the host if it is not connected. Decision 1: on the first paste, drop or file
 * pick, or at page load with keepReady on; never at browser start. */
function startHost() {
  if (host.port) return;
  clearTimeout(host.timer);
  host.timer = null;
  let port;
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch {
    setDown('host_missing');
    return;
  }
  Object.assign(host, {
    port, state: 'starting', version: null, reviewMode: null, error: null, engineMessage: null,
    imagesFailed: false, pendingCode: null, lastFrameAt: Date.now(), lastWorkAt: Date.now(),
  });
  port.onMessage.addListener((msg) => {
    if (port !== host.port) return;
    try {
      onHostMessage(msg);
    } catch {
      dropHost('protocol'); // a frame that broke our own handling: nothing after it is trusted
    }
  });
  port.onDisconnect.addListener(() => onHostDisconnect(port));
  broadcastStatus();
}

function onHostDisconnect(port) {
  const why = chrome.runtime.lastError?.message || '';
  if (port !== host.port) return;
  const code = host.pendingCode || disconnectCode(why);
  const quiet = Date.now() - host.lastWorkAt;
  setDown(code);
  // Reconnecting keeps a warm host only for users who asked for one, never after the
  // host's own idle exit, and never when trying again cannot help.
  const hopeless = ['host_missing', 'host_forbidden', 'host_refused', 'host_config', 'host_incompatible'];
  if (settings.keepReady && quiet < IDLE_EXIT_MS && !hopeless.includes(code)) scheduleReconnect();
}

/** Chrome's lastError texts are fixed strings; mapped, never shown or stored. */
function disconnectCode(why) {
  if (/not found/i.test(why)) return 'host_missing';
  if (/forbidden/i.test(why)) return 'host_forbidden';
  if (/exited/i.test(why)) return 'host_exited';
  if (/communicating/i.test(why)) return 'protocol';
  return 'host_down';
}

function scheduleReconnect() {
  if (host.timer || host.attempt >= RECONNECT_MS.length) return;
  host.timer = setTimeout(() => {
    host.timer = null;
    if (!host.port) startHost();
  }, RECONNECT_MS[host.attempt++]);
}

/** Every open request fails with `code`; the connection is gone. */
function setDown(code) {
  host.port = null;
  host.state = 'down';
  host.error = { code, message: reasonFor(code) };
  failAll(code);
  broadcastStatus();
}

/** Ends a connection we no longer trust. Closing the port closes the host's stdin, and
 * the host exits on its own. */
function dropHost(code) {
  const port = host.port;
  setDown(code);
  try {
    port?.disconnect();
  } catch {
    // already gone
  }
}

function sendHost(msg) {
  if (!host.port) throw new Error('no host');
  host.port.postMessage(msg);
}

function readyFor(kind) {
  if (host.state === 'ready-all') return true;
  return host.state === 'ready-text' && (!NEEDS_IMAGES.has(kind) || host.imagesFailed);
}

// --- frames from the host ----------------------------------------------------------------

const isId = (v) => typeof v === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(v);
const isCount = (low) => (v) => Number.isInteger(v) && v >= low;
const isText = (max) => (v) => typeof v === 'string' && v.length <= max;
const isB64 = (v) => typeof v === 'string' && v.length <= CHUNK && v.length % 4 === 0
  && /^[A-Za-z0-9+/]*={0,2}$/.test(v);

/** The host's messages, field for field. Unknown types or fields are refused, as the host
 * refuses ours: a frame we cannot fully account for is not acted on. */
const FROM_HOST = {
  status: { id: (v) => v === null || isId(v), state: (v) => HOST_STATES.includes(v),
    version: isText(64), protocol: isCount(0), review_mode: (v) => v === null || REVIEW_MODES.includes(v) },
  progress: { id: isId, stage: (v) => typeof v === 'string' && /^[a-z_-]{1,32}$/.test(v),
    page: isCount(1), pages: isCount(1) },
  result: { id: isId, size: isCount(0), total: isCount(1), parts: isParts, review: isReview },
  chunk: { id: isId, seq: isCount(0), total: isCount(1), data: isB64 },
  error: { id: (v) => v === null || isId(v), code: (v) => typeof v === 'string' && /^[a-z_]{1,40}$/.test(v),
    message: isText(1000) },
};
const OPTIONAL = { progress: ['page', 'pages'] };

function isParts(v) {
  return Array.isArray(v) && v.length >= 1 && v.length <= 4 && v.every((p) =>
    p && typeof p === 'object' && !Array.isArray(p)
    && sameKeys(p, ['name', 'media_type', 'size'])
    && isText(32)(p.name) && isText(64)(p.media_type) && isCount(0)(p.size));
}

/** {needed, count}: counts only, and `needed` must agree with the count. */
function isReview(v) {
  return Boolean(v) && typeof v === 'object' && !Array.isArray(v) && sameKeys(v, ['needed', 'count'])
    && typeof v.needed === 'boolean' && isCount(0)(v.count) && v.needed === v.count > 0;
}

function sameKeys(obj, keys) {
  const own = Object.keys(obj);
  return own.length === keys.length && keys.every((k) => Object.prototype.hasOwnProperty.call(obj, k));
}

/** @returns {boolean} whether `msg` is exactly one of the host's messages. */
function validHostMessage(msg) {
  if (!msg || typeof msg !== 'object' || Array.isArray(msg)) return false;
  const fields = FROM_HOST[msg.type];
  if (!fields) return false;
  const optional = OPTIONAL[msg.type] || [];
  const present = optional.filter((k) => k in msg);
  if (present.length !== 0 && present.length !== optional.length) return false; // page and pages together
  const required = Object.keys(fields).filter((k) => !optional.includes(k));
  if (!sameKeys(msg, ['type', ...required, ...present])) return false;
  return [...required, ...present].every((k) => fields[k](msg[k]));
}

function onHostMessage(msg) {
  host.lastFrameAt = Date.now();
  // A host on another protocol version is told apart before its frames are checked
  // against ours: its status would fail that check, and the user would only see 'protocol'.
  if (msg && msg.type === 'status' && Number.isInteger(msg.protocol) && msg.protocol !== PROTOCOL) {
    return dropHost('host_incompatible');
  }
  if (!validHostMessage(msg)) return dropHost('protocol');
  if (msg.type === 'status') return onStatus(msg);
  if (msg.type === 'error' && msg.id === null) return onHostWideError(msg);
  const job = jobs.get(msg.id);
  if (!job) {
    // Frames for a request we dropped can still be on their way; anything else is not ours.
    if (closedIds.has(msg.id) || pings.has(msg.id)) return undefined;
    return dropHost('protocol');
  }
  host.lastWorkAt = Date.now();
  if (msg.type === 'error') {
    job.hostDone = true;
    // The host's complaints about our framing are our fault, told as one 'protocol' code;
    // a code from a newer host that we do not know is shown as an internal error.
    const code = FRAMING_FAULTS.has(msg.code) ? 'protocol' : REASONS[msg.code] ? msg.code : 'internal';
    return finish(job, code, msg.message);
  }
  if (msg.type === 'progress') return onProgress(job, msg);
  if (msg.type === 'result') return onResult(job, msg);
  return onResultChunk(job, msg);
}

function onStatus(msg) {
  if (msg.protocol !== PROTOCOL) return dropHost('host_incompatible');
  if (msg.id !== null) {
    const resolve = pings.get(msg.id);
    pings.delete(msg.id);
    resolve?.(msg.state);
    if (!resolve && !closedIds.has(msg.id)) return dropHost('protocol'); // a late answer is fine
  }
  const before = [host.state, host.reviewMode];
  host.state = msg.state;
  host.version = msg.version;
  host.reviewMode = msg.review_mode;
  if (msg.state === 'unavailable') {
    host.error = { code: 'engine_unavailable', message: reasonFor('engine_unavailable', host.engineMessage) };
    for (const job of [...jobs.values()]) cancelJob(job, 'engine_unavailable', host.engineMessage);
  } else if (msg.state !== 'warming') {
    host.attempt = 0; // a host that got this far may be reconnected to again
    host.error = null;
    for (const job of jobs.values()) if (job.held && readyFor(job.kind)) release(job);
  }
  if (before[0] !== host.state || before[1] !== host.reviewMode) broadcastStatus();
  return undefined;
}

/** An error frame for no request: the host's own startup, or our framing. */
function onHostWideError(msg) {
  if (msg.code === 'engine_unavailable') {
    host.engineMessage = msg.message; // the status that follows says whether text or only OCR failed
    if (host.state === 'ready-text') {
      host.imagesFailed = true; // waiting PDFs and images get this error from the host itself
      for (const job of jobs.values()) if (job.held && readyFor(job.kind)) release(job);
    }
    return;
  }
  if (msg.code === 'origin_refused') host.pendingCode = 'host_refused'; // the host exits next
  else if (msg.code === 'bad_config') host.pendingCode = 'host_config';
  else dropHost(msg.code === 'internal' ? 'internal' : 'protocol');
}

// --- pings -------------------------------------------------------------------------------

/** @type {Map<string, (state: string) => void>} */
const pings = new Map();

/** The host's own answer to "are you there", within 5 s, or null. */
function pingHost() {
  if (!host.port) return Promise.resolve(null);
  const id = newId('p');
  return new Promise((resolve) => {
    pings.set(id, resolve);
    setTimeout(() => {
      if (!pings.delete(id)) return;
      remember(closedIds, id, 256);
      resolve(null);
    }, 5_000);
    try {
      sendHost({ type: 'ping', id });
    } catch {
      pings.delete(id);
      resolve(null);
    }
  });
}

// --- jobs --------------------------------------------------------------------------------

/**
 * @typedef {object} Job
 * @property {string} id           the request id at the host
 * @property {string} kind         'text' or one of KINDS
 * @property {number} size
 * @property {number} total
 * @property {string} site
 * @property {string} scope
 * @property {number|null} tabId
 * @property {{postMessage: Function, disconnect: Function}} client
 * @property {boolean} fromPage    a content script asked, so review mode applies
 * @property {number} uploaded     chunks passed to the host
 * @property {boolean} held        waiting for the host to warm up
 * @property {boolean} everHeld
 * @property {boolean} hostDone    the host has answered in full (result or error)
 * @property {boolean} review
 * @property {'always'|'low_confidence'|null} reviewReason
 * @property {number} activeAt     last sign of life from either side
 * @property {{size: number, total: number, parts: object[], review: {needed: boolean, count: number}}|null} result
 * @property {string[]} chunks     the result's base64 chunks, checked as they arrive
 * @property {number} received     result bytes so far
 * @property {number} createdAt
 * @property {ReturnType<typeof setTimeout>|null} timer   warm-up hold or review timeout
 */

/** @type {Map<string, Job>} */
const jobs = new Map();
/** Ids we stopped caring about; the host may still send for them. */
const closedIds = new Set();
/** What happened to recent requests, for the side panel: kinds and codes, never content. */
const recent = [];
let idCounter = 0;

function newId(prefix) {
  const rand = crypto.getRandomValues(new Uint32Array(2));
  return `${prefix}${(++idCounter).toString(36)}-${rand[0].toString(36)}${rand[1].toString(36)}`;
}

const chunkCount = (size) => Math.max(1, Math.ceil(size / RAW_CHUNK));

/** Bytes a well-formed base64 string decodes to. */
function b64Bytes(data) {
  const pad = data.endsWith('==') ? 2 : data.endsWith('=') ? 1 : 0;
  return (data.length / 4) * 3 - pad;
}

function validStart(msg) {
  return msg && msg.op === 'start' && (msg.kind === 'text' || KINDS.includes(msg.kind))
    && isCount(0)(msg.size) && msg.total === chunkCount(msg.size);
}

/**
 * A new request: checked, sent to the host straight away (the host queues it while it
 * warms), and held here with a deadline until the host can serve its kind.
 */
function createJob(client, msg, place, fromPage) {
  if (msg.size > MAX_PAYLOAD) return refuse(client, 'too_large');
  if (jobs.size >= MAX_JOBS) return refuse(client, 'busy');
  // An engine that could not start stays that way until its host exits, so each new
  // request starts a fresh host: one that works once the user has fixed the install.
  if (host.state === 'unavailable') dropHost('engine_unavailable');
  const now = Date.now();
  host.lastWorkAt = now;
  /** @type {Job} */
  const job = {
    id: newId('j'), kind: msg.kind, size: msg.size, total: msg.total, site: place.site,
    scope: place.scope, tabId: place.tabId, client, fromPage, uploaded: 0, held: false,
    everHeld: false, hostDone: false, review: false, reviewReason: null, activeAt: now, result: null, chunks: [],
    received: 0, createdAt: now, timer: null,
  };
  jobs.set(job.id, job);
  ensureTicker();
  post(job, { op: 'accepted', job: job.id });
  startHost();
  if (!host.port) return finish(job, host.error?.code || 'host_down');
  const header = { type: job.kind === 'text' ? 'redact_text' : 'redact_file', id: job.id, scope: job.scope,
    site: job.site, size: job.size, total: job.total };
  if (job.kind !== 'text') header.kind = job.kind;
  sendHost(header);
  if (!readyFor(job.kind)) hold(job);
  return job;
}

function refuse(client, code) {
  try {
    client.postMessage({ op: 'blocked', code, message: reasonFor(code) });
    client.disconnect();
  } catch {
    // the client is gone
  }
  return null;
}

/** Waits for the host to warm up; after WARM_HOLD_MS the request is blocked (decision 1). */
function hold(job) {
  job.held = true;
  job.everHeld = true;
  post(job, { op: 'held', state: host.state });
  job.timer = setTimeout(() => {
    if (jobs.get(job.id) === job && job.held) cancelJob(job, 'warming_timeout');
  }, WARM_HOLD_MS);
}

function release(job) {
  clearTimeout(job.timer);
  job.timer = null;
  job.held = false;
  job.activeAt = Date.now(); // silence is counted from here, not from the paste
}

/** One request chunk from the client, checked as the host would, then passed on. */
function onClientChunk(job, msg) {
  job.activeAt = Date.now();
  const expected = Math.min(RAW_CHUNK, job.size - job.uploaded * RAW_CHUNK);
  if (msg.seq !== job.uploaded || msg.total !== job.total || job.uploaded >= job.total
      || !isB64(msg.data) || b64Bytes(msg.data) !== expected) {
    return cancelJob(job, 'extension_error');
  }
  try {
    sendHost({ type: 'chunk', id: job.id, seq: msg.seq, total: job.total, data: msg.data });
  } catch {
    return finish(job, host.error?.code || 'host_down');
  }
  job.uploaded += 1;
  return undefined;
}

function onProgress(job, msg) {
  job.activeAt = Date.now();
  const out = { op: 'progress', stage: msg.stage };
  if ('page' in msg) Object.assign(out, { page: msg.page, pages: msg.pages });
  post(job, out);
}

function onResult(job, msg) {
  const shapes = RESULT_SHAPES[job.kind];
  const shape = msg.parts.map((p) => [p.name, p.media_type]);
  const fits = shapes.some((s) => JSON.stringify(s) === JSON.stringify(shape));
  const sum = msg.parts.reduce((n, p) => n + p.size, 0);
  if (job.result || job.uploaded !== job.total || !fits || sum !== msg.size
      || msg.total !== chunkCount(msg.size) || msg.size > MAX_PAYLOAD) {
    return cancelJob(job, 'protocol');
  }
  job.activeAt = Date.now();
  job.result = { size: msg.size, total: msg.total, parts: msg.parts, review: msg.review };
  return undefined;
}

/** A result chunk, checked like the host checks ours; the result is used only once every
 * chunk is in and accounted for. */
function onResultChunk(job, msg) {
  const r = job.result;
  if (!r) return cancelJob(job, 'protocol');
  const expected = Math.min(RAW_CHUNK, r.size - job.received);
  if (msg.seq !== job.chunks.length || msg.total !== r.total || b64Bytes(msg.data) !== expected) {
    return cancelJob(job, 'protocol');
  }
  job.activeAt = Date.now();
  job.chunks.push(msg.data);
  job.received += expected;
  if (job.chunks.length < r.total) return undefined;
  job.hostDone = true;
  const reason = job.fromPage ? reviewReason(job.result.review) : null;
  if (reason) return startReview(job, reason);
  return deliver(job);
}

/**
 * Why a page's result must wait for review, or null. Follows the policy the host loaded:
 * 'always' holds everything; 'low_confidence' holds a result in which the engine marked
 * any decision for review; 'off' holds nothing. A host that has not said (null) is
 * treated as 'always': holding is the side that cannot leak.
 */
function reviewReason(review) {
  const mode = host.reviewMode;
  if (mode === 'off') return null;
  if (mode === 'low_confidence') return review.needed ? 'low_confidence' : null;
  return 'always';
}

function deliver(job) {
  const r = job.result;
  post(job, { op: 'result', size: r.size, total: r.total, parts: r.parts });
  job.chunks.forEach((data, seq) => post(job, { op: 'chunk', seq, total: r.total, data }));
  post(job, { op: 'done' });
  finish(job, null);
}

/** Drops a request here and at the host. */
function cancelJob(job, code, hostMessage) {
  if (jobs.get(job.id) !== job) return;
  if (!job.hostDone && host.port) {
    try {
      sendHost({ type: 'cancel', id: job.id });
    } catch {
      // the disconnect fails it anyway
    }
  }
  finish(job, code, hostMessage);
}

/**
 * Ends a request: `code` null means delivered; anything else is a block, told to the
 * client with fixed text. Safe to call twice.
 */
function finish(job, code, hostMessage) {
  if (jobs.get(job.id) !== job) return;
  clearTimeout(job.timer);
  jobs.delete(job.id);
  remember(closedIds, job.id, 256);
  job.chunks = [];
  if (code) post(job, { op: 'blocked', code, message: reasonFor(code, hostMessage) });
  try {
    job.client.disconnect();
  } catch {
    // already closed
  }
  recent.unshift({ kind: job.kind, site: job.site, held: job.everHeld, code: code || 'delivered' });
  recent.length = Math.min(recent.length, 20);
  if (job.review) broadcastReviews();
  broadcastStatus();
}

function failAll(code) {
  for (const job of [...jobs.values()]) {
    job.hostDone = true; // the host that had it is gone
    finish(job, code);
  }
  for (const resolve of pings.values()) resolve(null);
  pings.clear();
}

function post(job, msg) {
  try {
    job.client.postMessage(msg);
  } catch {
    // the page went away; its port's onDisconnect cancels the job
  }
}

function remember(set, value, max) {
  set.add(value);
  if (set.size > max) set.delete(set.values().next().value);
}

/** The user-facing text for a code. Only engine and input errors carry the host's own
 * text, which the host builds from fixed strings so that it never quotes input. */
function reasonFor(code, hostMessage) {
  const base = REASONS[code] || REASONS.internal;
  if ((code === 'engine_unavailable' || code === 'bad_input') && hostMessage) return `${base} (${hostMessage})`;
  return base;
}

// --- deadlines ---------------------------------------------------------------------------

let ticker = null;

/** Blocks requests the host went quiet on. Runs only while there are jobs. */
function ensureTicker() {
  if (ticker) return;
  ticker = setInterval(() => {
    if (jobs.size === 0) {
      clearInterval(ticker);
      ticker = null;
      return;
    }
    const now = Date.now();
    for (const job of [...jobs.values()]) {
      if (job.held || job.review || job.hostDone) continue;
      const limit = job.kind === 'text' ? SILENCE_MS.text : SILENCE_MS.file;
      if (now - Math.max(job.activeAt, host.lastFrameAt) <= limit) continue;
      if (now - host.lastFrameAt > SILENCE_MS.file) return dropHost('host_timeout'); // hung, not busy
      cancelJob(job, 'host_timeout');
    }
  }, 500);
}

// --- review ------------------------------------------------------------------------------

function startReview(job, reason) {
  job.review = true;
  job.reviewReason = reason;
  job.timer = setTimeout(() => finish(job, 'review_timeout'), REVIEW_TIMEOUT_MS);
  post(job, { op: 'review' });
  broadcastReviews();
}

function reviewOf(job) {
  return { job: job.id, site: job.site, kind: job.kind, parts: job.result.parts, createdAt: job.createdAt,
    expiresAt: job.createdAt + REVIEW_TIMEOUT_MS, reason: job.reviewReason };
}

function reviewList() {
  return [...jobs.values()].filter((j) => j.review).map(reviewOf);
}

/** The redacted text part of a held result, for the panel to show. */
function reviewText(job) {
  let at = 0;
  for (const part of job.result.parts) {
    if (part.name === 'text') {
      const bytes = fromB64Chunks(job.chunks, job.result.size);
      return new TextDecoder('utf-8').decode(bytes.subarray(at, at + part.size));
    }
    at += part.size;
  }
  return undefined;
}

function decideReview(id, approve) {
  const job = jobs.get(id);
  if (!job || !job.review) return false;
  clearTimeout(job.timer);
  job.timer = null;
  job.review = false;
  broadcastReviews();
  if (approve) deliver(job);
  else finish(job, 'review_rejected');
  return true;
}

// --- where a request comes from ----------------------------------------------------------

/** Temporary scopes of new chats, per tab, and the chat each was later given (PLAN §6). */
const scopes = { temp: {}, alias: {} };
const scopesLoaded = chrome.storage.session.get({ scopes })
  .then((s) => Object.assign(scopes, s.scopes || {}))
  .catch(() => {});

function saveScopes() {
  chrome.storage.session.set({ scopes }).catch(() => {});
}

/**
 * The pseudonym scope for a request: the chat's id from its URL, so pseudonyms stay
 * stable within a chat. A new chat has no id yet, so it gets a temporary scope for its
 * tab; when that tab reaches a chat URL, the chat keeps the temporary scope, and
 * [PERSON_1] still means the same person.
 */
function scopeFor(site, pageUrl, tabId) {
  const match = pageUrl && pageUrl.hostname === site ? SITES[site].exec(pageUrl.pathname) : null;
  const tabKey = String(tabId ?? 'none');
  if (match) {
    const key = `${site}/chat/${match[1]}`;
    if (scopes.alias[key]) return scopes.alias[key];
    const temp = scopes.temp[tabKey];
    if (temp && temp.startsWith(`${site}/`)) {
      scopes.alias[key] = temp;
      delete scopes.temp[tabKey];
      saveScopes();
      return temp;
    }
    return key;
  }
  if (!scopes.temp[tabKey]?.startsWith(`${site}/`)) {
    scopes.temp[tabKey] = `${site}/new/${newId('t')}`;
    saveScopes();
  }
  return scopes.temp[tabKey];
}

chrome.tabs.onRemoved.addListener((tabId) => {
  if (scopes.temp[String(tabId)]) {
    delete scopes.temp[String(tabId)];
    saveScopes();
  }
  pageAdapters.delete(tabId);
});

const ownOrigin = chrome.runtime.getURL('');
/** One of our own pages (the side panel, or the same page open in a tab). A content
 * script's sender URL is the site's, never ours, so a site cannot pass for one. */
const fromExtensionPage = (sender) => typeof sender.url === 'string' && sender.url.startsWith(ownOrigin);
const fromContentScript = (sender) => Boolean(sender.tab) && typeof sender.origin === 'string'
  && !fromExtensionPage(sender);

function siteOf(origin) {
  try {
    const url = new URL(origin);
    return url.protocol === 'https:' && SITES[url.hostname] ? url.hostname : null;
  } catch {
    return null;
  }
}

/** Site, scope and tab for a request. A content script's site is its frame's origin as
 * Chrome reports it; the side panel names a tab, whose URL Chrome shows only for our sites. */
async function placeOf(sender, tabId) {
  await scopesLoaded;
  if (fromContentScript(sender)) {
    const site = siteOf(sender.origin);
    if (!site) throw new Error('site');
    return { site, tabId: sender.tab.id, scope: scopeFor(site, safeUrl(sender.tab.url), sender.tab.id) };
  }
  if (fromExtensionPage(sender) && Number.isInteger(tabId)) {
    const tab = await chrome.tabs.get(tabId);
    const url = safeUrl(tab.url);
    const site = url && siteOf(url.origin);
    if (!site) throw new Error('site');
    return { site, tabId, scope: scopeFor(site, url, tabId) };
  }
  throw new Error('sender');
}

function safeUrl(text) {
  try {
    return text ? new URL(text) : null;
  } catch {
    return null;
  }
}

// --- ports and messages ------------------------------------------------------------------

chrome.runtime.onConnect.addListener((port) => {
  if (port.name === 'redactit/job') acceptJobPort(port);
  else if (port.name === 'redactit/events' && fromExtensionPage(port.sender)) addEventPort(port);
  else port.disconnect();
});

function acceptJobPort(port) {
  /** @type {Job|null} */
  let job = null;
  let starting = false;
  let gone = false; // cancelled or closed before the job existed
  port.onMessage.addListener(async (msg) => {
    if (!job) {
      if (starting && msg && msg.op === 'cancel') {
        gone = true;
        return undefined;
      }
      if (starting || !validStart(msg)) return refuse(port, 'extension_error');
      starting = true;
      let place;
      try {
        place = await placeOf(port.sender, msg.tabId);
      } catch {
        return refuse(port, 'not_allowed_site');
      }
      await settingsLoaded;
      if (gone) return undefined;
      job = createJob(port, msg, place, fromContentScript(port.sender)) || null;
      return undefined;
    }
    if (jobs.get(job.id) !== job) return undefined; // already ended; late chunks are dropped
    if (msg && msg.op === 'chunk') return onClientChunk(job, msg);
    return cancelJob(job, msg && msg.op === 'cancel' ? 'cancelled' : 'extension_error');
  });
  port.onDisconnect.addListener(() => {
    gone = true;
    if (job) cancelJob(job, 'cancelled');
  });
}

/** Side-panel ports that receive status and review changes. */
const eventPorts = new Set();

function addEventPort(port) {
  eventPorts.add(port);
  port.onDisconnect.addListener(() => eventPorts.delete(port));
  port.postMessage({ event: 'status', status: statusOf() });
  port.postMessage({ event: 'reviews', reviews: reviewList() });
}

function broadcast(msg) {
  for (const port of eventPorts) {
    try {
      port.postMessage(msg);
    } catch {
      eventPorts.delete(port);
    }
  }
}

function broadcastStatus() {
  if (eventPorts.size) broadcast({ event: 'status', status: statusOf() });
}

function broadcastReviews() {
  if (eventPorts.size) broadcast({ event: 'reviews', reviews: reviewList() });
}

/** Adapter self-check results per tab, as content scripts report them. */
const pageAdapters = new Map();

function statusOf(tabId) {
  const status = {
    state: host.state, version: host.version, error: host.error, jobs: jobs.size,
    reviews: reviewList().length, settings: { ...settings }, reviewMode: host.reviewMode, recent: recent.slice(),
  };
  if (Number.isInteger(tabId) && pageAdapters.has(tabId)) status.adapter = pageAdapters.get(tabId);
  return status;
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  const type = msg && typeof msg === 'object' ? msg.type : null;
  const page = fromExtensionPage(sender);
  const handlers = {
    'redactit/status': async () => {
      if (msg.probe === true && host.port && (await pingHost()) === null) dropHost('host_timeout');
      return statusOf(msg.tabId);
    },
    'redactit/start-host': async () => {
      startHost();
      return statusOf();
    },
    'redactit/page': async () => {
      if (!fromContentScript(sender) || !siteOf(sender.origin)) return null;
      if (sender.frameId === 0) {
        pageAdapters.set(sender.tab.id, { name: typeof msg.adapter === 'string' ? msg.adapter : null,
          active: msg.active === true ? true : msg.active === false ? false : null });
      }
      await settingsLoaded;
      if (settings.keepReady) startHost();
      return { keepReady: settings.keepReady };
    },
    'redactit/restart-host': page && (async () => {
      dropHost('cancelled');
      startHost();
      return statusOf();
    }),
    'redactit/cancel': page && (() => {
      const job = jobs.get(msg.job);
      if (job) cancelJob(job, 'cancelled');
      return { ok: Boolean(job) };
    }),
    'redactit/redact-text': page && (() => redactForPanel(msg)),
    'redactit/review-list': page && (() => reviewList()),
    'redactit/review-get': page && (() => {
      const job = jobs.get(msg.job);
      return job && job.review ? { ...reviewOf(job), text: reviewText(job) } : null;
    }),
    'redactit/review-decide': page && (() => ({ ok: decideReview(msg.job, msg.approve === true) })),
  };
  const handler = handlers[type];
  if (!handler) return false;
  Promise.resolve()
    .then(handler)
    .then(reply, () => reply({ ok: false, code: 'extension_error', message: reasonFor('extension_error') }));
  return true; // the reply may come later
});

/** "Redact & copy" from the side panel: the same job path, with an in-memory client. */
async function redactForPanel(msg) {
  const kind = 'text';
  const fail = (code) => ({ ok: false, code, message: reasonFor(code) });
  if (typeof msg.text !== 'string') return fail('extension_error');
  const bytes = new TextEncoder().encode(msg.text);
  if (bytes.length > MAX_PANEL_TEXT) return fail('too_large');
  let place;
  try {
    place = await placeOf({ url: ownOrigin }, msg.tabId);
  } catch {
    return fail('not_allowed_site');
  }
  await settingsLoaded;
  return new Promise((resolve) => {
    const parts = [];
    let header = null;
    const client = {
      postMessage(m) {
        if (m.op === 'result') header = m;
        else if (m.op === 'chunk') parts.push(m.data);
        else if (m.op === 'done') {
          const out = fromB64Chunks(parts, header.size);
          resolve({ ok: true, text: new TextDecoder('utf-8').decode(out) });
        } else if (m.op === 'blocked') resolve({ ok: false, code: m.code, message: m.message });
      },
      disconnect() {},
    };
    const total = chunkCount(bytes.length);
    const job = createJob(client, { op: 'start', kind, size: bytes.length, total }, place, false);
    if (!job) return;
    for (let seq = 0; seq < total && jobs.get(job.id) === job; seq += 1) {
      const data = toB64(bytes.subarray(seq * RAW_CHUNK, (seq + 1) * RAW_CHUNK));
      onClientChunk(job, { op: 'chunk', seq, total, data });
    }
  });
}

// --- base64 ------------------------------------------------------------------------------

function toB64(bytes) {
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

function fromB64Chunks(chunks, size) {
  const out = new Uint8Array(size);
  let at = 0;
  for (const data of chunks) {
    const bin = atob(data);
    for (let i = 0; i < bin.length; i += 1) out[at + i] = bin.charCodeAt(i);
    at += bin.length;
  }
  return out;
}
