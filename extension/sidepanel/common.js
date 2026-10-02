/**
 * Small helpers the panel's parts share: the chat a request is for, one-shot messages to
 * the service worker, the live region, notes and copying. Nothing here stores or logs
 * anything: what the user types or gets back lives only in this page's DOM.
 */

export const $ = (id) => /** @type {any} */ (document.getElementById(id));

const SITES = ['claude.ai', 'chatgpt.com', 'gemini.google.com'];
/** Match patterns for tabs.query; siteOf() then keeps only https pages. */
const SITE_PATTERNS = SITES.map((s) => `*://${s}/*`);

/** The AI site a URL is on, or null. Chrome shows tab URLs only for the sites the
 * extension may run on, which are exactly these. */
export function siteOf(url) {
  try {
    const u = new URL(url);
    return u.protocol === 'https:' && SITES.includes(u.hostname) ? u.hostname : null;
  } catch {
    return null;
  }
}

/**
 * The chat tab requests are for: its site picks the rules and its chat the pseudonym
 * scope, so the panel must name it (`tabId` in the worker's API). In the side panel that
 * is the active tab of the panel's window, so what is redacted always matches the chat
 * in view. When this page is open in a tab of its own, the active tab is the page
 * itself, so the chat tab used most recently is taken instead. `chat` is the page's
 * origin and path: a tab that moves to another chat is another target.
 * @returns {Promise<{id: number, site: string, chat: string}|null>}
 */
export async function findTarget() {
  let self = null;
  try {
    self = await chrome.tabs.getCurrent();
  } catch {
    self = null;
  }
  try {
    let tab;
    if (!self) {
      [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    } else {
      const tabs = await chrome.tabs.query({ url: SITE_PATTERNS });
      tabs.sort((a, b) => (b.lastAccessed || 0) - (a.lastAccessed || 0));
      tab = tabs.find((t) => siteOf(t.url));
    }
    const site = tab && siteOf(tab.url);
    if (!site) return null;
    const url = new URL(tab.url);
    return { id: tab.id, site, chat: url.origin + url.pathname };
  } catch {
    return null;
  }
}

/** Calls `fn` whenever the chat in view may have changed. */
export function onTargetChange(fn) {
  chrome.tabs.onActivated.addListener(() => fn());
  chrome.tabs.onRemoved.addListener(() => fn());
  chrome.tabs.onUpdated.addListener((_id, info) => {
    if (info.url || info.status === 'complete') fn();
  });
  chrome.windows?.onFocusChanged?.addListener(() => fn());
}

/**
 * One message to the worker. A type the worker does not handle (an older worker, or a
 * message not added yet) rejects in Chrome; that comes back as null, so callers can say
 * "not available" instead of failing.
 */
export async function ask(message) {
  try {
    const reply = await chrome.runtime.sendMessage(message);
    return reply === undefined ? null : reply;
  } catch {
    return null;
  }
}

let announceTimer = 0;
/** Says `msg` through the polite live region; cleared first so a repeat is read again. */
export function announce(msg) {
  const el = $('announce');
  el.textContent = '';
  clearTimeout(announceTimer);
  announceTimer = setTimeout(() => { el.textContent = msg; }, 60);
}

/** A copy of one of the icons kept as templates in panel.html. */
export function icon(name) {
  const tpl = $(`icon-${name}`);
  return tpl ? tpl.content.firstElementChild.cloneNode(true) : document.createTextNode('');
}

/** Shows `text` in a note element with a tone ('' | 'good' | 'warn' | 'bad'). */
export function setNote(el, text, tone = '') {
  el.className = `note${tone ? ` ${tone}` : ''}`;
  el.replaceChildren(icon(tone === 'bad' ? 'bad' : tone === 'good' ? 'good' : tone === 'warn' ? 'warn' : 'info'));
  const span = document.createElement('span');
  span.textContent = text;
  el.append(span);
  el.hidden = false;
}

/**
 * Copies `text` after a click. The async clipboard first; where that is refused, a copy
 * command with the text set on the event, so no extra element ever holds it.
 * @returns {Promise<boolean>}
 */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const onCopy = (e) => {
      e.clipboardData.setData('text/plain', text);
      e.preventDefault();
    };
    document.addEventListener('copy', onCopy);
    try {
      return document.execCommand('copy');
    } catch {
      return false;
    } finally {
      document.removeEventListener('copy', onCopy);
    }
  }
}

/** "1.4 s", "12 s", "2 min 5 s". */
export function fmtSeconds(s) {
  if (s < 10) return `${s.toFixed(1)} s`;
  if (s < 60) return `${Math.round(s)} s`;
  return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
}

/** UTF-8 bytes to text; a result that is not UTF-8 is not shown. */
export function decodeText(bytes) {
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
}
