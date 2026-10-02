/**
 * Status and settings: the host's state from the worker's events port (`redactit/events`),
 * the chat the panel is working for, the "Keep Redactit ready" switch, and the review
 * mode as the worker reports it.
 *
 * The review mode is shown, never set here: it comes from policy, so a panel cannot turn
 * review off for an organisation that requires it. A mode the panel does not know is
 * shown as off, the state that promises the user nothing.
 */
import { $, findTarget, icon, onTargetChange } from './common.js';

const REVIEW_MODES = {
  always: { value: 'Every paste and file', line: 'Every paste and file from a chat waits here for your review.' },
  low_confidence: {
    value: 'When Redactit is unsure',
    line: 'Pastes and files Redactit is unsure about wait here for your review.',
  },
  off: { value: 'Off', line: 'Review is off: redacted pastes and files go straight to the chat.' },
};

/** The worker's review mode: top-level `reviewMode`, or the older `settings.reviewMode`. */
export function reviewModeOf(status) {
  const mode = status ? status.reviewMode ?? status.settings?.reviewMode : null;
  return Object.prototype.hasOwnProperty.call(REVIEW_MODES, mode) ? mode : 'off';
}

/** [tone, short label, sentence] for the host's state. */
function describe(status) {
  const message = status.error?.message || '';
  switch (status.state) {
    case 'ready-all': return ['good', 'Ready', 'Ready for text, files and images.'];
    case 'ready-text': return ['good', 'Ready for text', 'Ready for text. Still loading what reads images; files wait for it.'];
    case 'warming': return ['warn', 'Warming up', 'Starting: loading the redaction engine.'];
    case 'starting': return ['warn', 'Starting', 'Starting.'];
    case 'unavailable': return ['bad', 'Engine unavailable', message || "Redactit's redaction engine could not start."];
    default: {
      const code = status.error?.code;
      if (code === 'host_missing') return ['bad', 'Not installed', message];
      if (code && !['host_down', 'cancelled'].includes(code)) return ['bad', 'Stopped', message];
      return ['idle', 'Starts when needed', 'Not running. It starts with your first paste or file.'];
    }
  }
}

/**
 * @param {{onReviews?: (reviews: object[], mode: string) => void,
 *          onTarget?: (target: {id: number, site: string}|null) => void}} hooks
 */
export function initStatus(hooks = {}) {
  const pill = $('hostPill');
  const keepReady = $('keepReady');
  let last = null;
  let reviews = [];
  let pillKey = '';

  function render(status) {
    last = status;
    const [tone, label, sentence] = describe(status);
    const key = `${tone}|${label}`;
    if (key !== pillKey) {
      pillKey = key;
      pill.className = `pill ${tone}`;
      pill.replaceChildren(icon({ good: 'good', warn: 'warn', bad: 'bad', idle: 'run' }[tone]));
      const span = document.createElement('span');
      span.id = 'hostPillText';
      span.textContent = label;
      pill.append(span);
      pill.setAttribute('aria-label', `Redactit's app: ${label}`);
    }
    const version = status.version ? ` Version ${status.version}.` : '';
    $('hostDetail').textContent = `${label}. ${sentence}${version}`;
    const mode = REVIEW_MODES[reviewModeOf(status)];
    $('reviewModeValue').textContent = mode.value;
    $('reviewModeLine').textContent = mode.line;
    if (status.settings && typeof status.settings.keepReady === 'boolean') keepReady.checked = status.settings.keepReady;
    hooks.onReviews?.(reviews, reviewModeOf(status));
  }

  function connect() {
    let port;
    try {
      port = chrome.runtime.connect({ name: 'redactit/events' });
    } catch {
      setTimeout(connect, 2000);
      return;
    }
    port.onMessage.addListener((m) => {
      if (!m || typeof m !== 'object') return;
      if (m.event === 'status' && m.status && typeof m.status === 'object') render(m.status);
      else if (m.event === 'reviews' && Array.isArray(m.reviews)) {
        reviews = m.reviews;
        hooks.onReviews?.(reviews, reviewModeOf(last));
      }
    });
    // The worker may be stopped by Chrome when idle; connecting again wakes it, and its
    // first messages bring the panel up to date.
    port.onDisconnect.addListener(() => setTimeout(connect, 500));
  }

  let targetId = null;
  async function showTarget() {
    const target = await findTarget();
    const line = $('target');
    if (target) {
      const strong = document.createElement('strong');
      strong.textContent = target.site;
      line.replaceChildren('Working for ', strong, ', the chat in view.');
    } else {
      line.textContent = 'Open claude.ai, chatgpt.com or gemini.google.com in this window to use Redactit here.';
    }
    const id = target ? target.id : null;
    if (id !== targetId) {
      targetId = id;
      hooks.onTarget?.(target);
    }
  }

  keepReady.addEventListener('change', () => {
    chrome.storage.local.set({ keepReady: keepReady.checked }).catch(() => {});
  });
  chrome.storage.local.get({ keepReady: false })
    .then((s) => { keepReady.checked = s.keepReady === true; })
    .catch(() => {});

  onTargetChange(showTarget);
  showTarget();
  connect();
}
