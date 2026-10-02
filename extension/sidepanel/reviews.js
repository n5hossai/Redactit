/**
 * The review queue: pastes and files held by the worker for review (review mode), each
 * with why it is held and its redacted text, to approve (handed to the page) or cancel
 * (blocked). Only extension pages may read or decide a review, so a site can never
 * approve its own paste; this panel is that reviewer.
 *
 * The list comes from the events port. Each item's text is fetched once with
 * `redactit/review-get` and shown read-only; it is the redacted version, never the input.
 */
import { $, announce, ask, setNote } from './common.js';

const KIND_NAMES = { text: 'Paste', txt: 'Text file', md: 'Markdown file', docx: 'Word file', pdf: 'PDF', image: 'Image' };
const REASONS = {
  always: 'Held because review is on for every paste and file.',
  low_confidence: 'Held because Redactit was unsure about part of it.',
};

export function initReviews() {
  const list = $('reviewList');
  const section = $('reviewSection');
  const title = $('reviewTitle');
  /** @type {Map<string, HTMLElement>} */
  const items = new Map();
  let mode = 'off';
  let clock = 0;
  title.tabIndex = -1; // where focus goes when the item it was on is decided

  function describe(review) {
    const kind = KIND_NAMES[review.kind] || 'Item';
    const site = typeof review.site === 'string' ? review.site : 'the chat';
    return `${kind} on ${site}`;
  }

  function reasonOf(review) {
    if (REASONS[review.reason]) return REASONS[review.reason];
    return mode === 'low_confidence' ? REASONS.low_confidence : REASONS.always;
  }

  function expiry(review) {
    const left = Math.round((review.expiresAt - Date.now()) / 60_000);
    return left >= 1 ? `${left} min left` : 'expires soon';
  }

  function add(review) {
    const li = /** @type {HTMLElement} */ ($('tpl-review').content.firstElementChild.cloneNode(true));
    const what = describe(review);
    li.dataset.job = review.job;
    li.querySelector('.review-kind').textContent = review.kind === 'text' ? 'paste' : review.kind;
    li.querySelector('.review-what').textContent = what;
    li.querySelector('.review-reason').textContent = reasonOf(review);
    li.querySelector('.review-expiry').textContent = expiry(review);
    const approve = li.querySelector('.review-approve');
    const cancel = li.querySelector('.review-cancel');
    approve.setAttribute('aria-label', `Approve the ${what.toLowerCase()}`);
    cancel.setAttribute('aria-label', `Cancel the ${what.toLowerCase()}`);
    approve.addEventListener('click', () => decide(li, review, true));
    cancel.addEventListener('click', () => decide(li, review, false));
    li.review = review;
    items.set(review.job, li);
    list.append(li);
    loadText(li, review);
  }

  async function loadText(li, review) {
    const held = await ask({ type: 'redactit/review-get', job: review.job });
    const box = li.querySelector('.review-text');
    const none = li.querySelector('.review-notext');
    if (held && typeof held.text === 'string') {
      box.value = held.text;
      return;
    }
    box.hidden = true;
    none.hidden = false;
    none.textContent = held
      ? `No text to show. Approve sends the redacted ${review.kind === 'image' ? 'image' : 'file'} as it is.`
      : 'This review has ended.';
  }

  async function decide(li, review, approve) {
    const buttons = li.querySelectorAll('button');
    buttons.forEach((b) => { b.disabled = true; });
    const reply = await ask({ type: 'redactit/review-decide', job: review.job, approve });
    if (reply && reply.ok === true) {
      announce(`${describe(review)} ${approve ? 'approved and sent to the chat' : 'cancelled. Nothing was sent'}.`);
      return;
    }
    buttons.forEach((b) => { b.disabled = false; });
    const note = document.createElement('p');
    setNote(note, 'This review has already ended.', 'warn');
    li.append(note);
  }

  /** The worker's current list: new items are added, ended ones removed, the rest kept. */
  function update(reviews, reviewMode) {
    mode = reviewMode;
    const seen = new Set();
    for (const review of reviews) {
      if (!review || typeof review.job !== 'string') continue;
      seen.add(review.job);
      if (!items.has(review.job)) {
        add(review);
        announce(`${describe(review)} is waiting for your review.`);
      }
    }
    for (const [job, li] of items) {
      if (seen.has(job)) continue;
      const hadFocus = li.contains(document.activeElement);
      li.remove();
      items.delete(job);
      if (hadFocus) title.focus();
    }
    $('reviewCount').textContent = String(items.size);
    section.classList.toggle('has-items', items.size > 0);
    $('reviewEmpty').hidden = items.size > 0;
    clearInterval(clock);
    if (items.size) {
      clock = setInterval(() => {
        for (const li of items.values()) li.querySelector('.review-expiry').textContent = expiry(li.review);
      }, 30_000);
    }
  }

  return { update };
}
