/**
 * gemini.google.com adapter: where the composer and the file input are.
 *
 * UNVERIFIED: these selectors come from public descriptions of the site's markup (a Quill
 * editor, `.ql-editor`, inside a `rich-textarea` element) and have not been checked
 * against the live page. Gemini's UI changes often (PLAN §1), so this adapter is the
 * likeliest to turn itself off; the generic handling still cancels, redacts or blocks
 * every paste and file, and the side panel's "Redact & copy" covers the rest.
 */
(globalThis.RedactitAdapters ||= []).push(Object.freeze({
  name: 'gemini',
  hosts: ['gemini.google.com'],
  verified: false,
  composer: [
    'rich-textarea div.ql-editor[contenteditable="true"]',
    'div.ql-editor[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"][aria-label*="prompt" i]',
  ],
  fileInput: [
    'input[type="file"][name="Filedata"]',
    'input[type="file"]',
  ],
}));
