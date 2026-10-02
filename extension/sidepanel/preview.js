/**
 * Shows a redacted result for review before it goes to a chat: an item in the review queue
 * (a page's paste or file the policy held) and the panel's copy when attaching it needs
 * review. A reviewer approves what they saw, so what is shown is the bytes Approve hands
 * over, not a description of them: text as text, an image as the image, a PDF as the PDF
 * itself (opened from this extension page in the browser's viewer) beside its page text.
 * Anything that cannot be shown that way is reported as not shown, and the caller keeps
 * Approve disabled.
 *
 * The blob URLs belong to this extension page; a site cannot load them. Each is revoked
 * with `revoke()` when the item goes.
 */

const IMAGE_TYPES = new Set(['image/png', 'image/jpeg']);
const PDF = 'application/pdf';

/**
 * @param {HTMLElement} box  holds .preview-text (textarea), .preview-image (img),
 *   .preview-file (a) and .preview-note (p), all hidden until used
 * @param {{text?: string|null, file?: Blob|null, fileMissing?: boolean}} content
 *   fileMissing: the result has a file part that could not be fetched to show
 * @returns {{shown: boolean, revoke: () => void}}  shown: everything Approve sends is on view
 */
export function showPreview(box, { text = null, file = null, fileMissing = false }) {
  const textEl = box.querySelector('.preview-text');
  const image = box.querySelector('.preview-image');
  const link = box.querySelector('.preview-file');
  const note = box.querySelector('.preview-note');
  let url = null;
  let shown = !fileMissing;
  textEl.hidden = typeof text !== 'string';
  if (typeof text === 'string') textEl.value = text;
  if (file) {
    url = URL.createObjectURL(file);
    if (IMAGE_TYPES.has(file.type)) {
      image.src = url;
      image.hidden = false;
    } else if (file.type === PDF) {
      link.href = url;
      link.hidden = false;
    } else {
      shown = false;
    }
  }
  if (!shown) {
    note.textContent = 'Redactit cannot show this file here, so it cannot be approved. Cancel it, then redact the '
      + 'file in this panel and download the copy instead.';
    note.hidden = false;
  }
  return {
    shown,
    revoke: () => {
      if (url) URL.revokeObjectURL(url);
      url = null;
    },
  };
}

/** Clears a preview box for its next use. */
export function clearPreview(box) {
  for (const el of box.querySelectorAll('.preview-text, .preview-image, .preview-file, .preview-note')) el.hidden = true;
  box.querySelector('.preview-text').value = '';
  box.querySelector('.preview-image').removeAttribute('src');
  box.querySelector('.preview-file').removeAttribute('href');
}

/** A file part sent as base64 by the worker, as a Blob of its own type. */
export function blobOf(base64, type) {
  const bin = atob(base64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  return new Blob([bytes], { type });
}
