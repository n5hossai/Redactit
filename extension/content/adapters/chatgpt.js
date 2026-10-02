/**
 * chatgpt.com adapter: where the composer and the file input are.
 *
 * UNVERIFIED: these selectors come from public descriptions of the site's markup (the
 * composer is `#prompt-textarea`, a ProseMirror editor that was a plain textarea before)
 * and have not been checked against the live page. intercept.js self-checks them at load;
 * if none matches, the adapter turns itself off and the generic handling still cancels,
 * redacts or blocks every paste and file.
 */
(globalThis.RedactitAdapters ||= []).push(Object.freeze({
  name: 'chatgpt',
  hosts: ['chatgpt.com'],
  verified: false,
  composer: [
    '#prompt-textarea[contenteditable="true"]',
    'textarea#prompt-textarea',
    'form div.ProseMirror[contenteditable="true"]',
  ],
  fileInput: [
    'form input[type="file"]:not([accept^="image"])',
    'form input[type="file"]',
    'input[type="file"]',
  ],
}));
