/**
 * claude.ai adapter: where the composer and the file input are.
 *
 * UNVERIFIED: these selectors come from public descriptions of the site's markup (a
 * ProseMirror editor inside the prompt form) and have not been checked against the live
 * page. intercept.js self-checks them at load; if none matches, the adapter turns itself
 * off and the generic handling still cancels, redacts or blocks every paste and file.
 * Sites change their markup without notice, so the owner's manual check (PLAN §7) is
 * where these get confirmed or corrected.
 */
(globalThis.RedactitAdapters ||= []).push(Object.freeze({
  name: 'claude',
  hosts: ['claude.ai'],
  verified: false,
  /** First match wins: the editor itself, then looser fallbacks for a redesign. */
  composer: [
    'div.ProseMirror[contenteditable="true"]',
    'fieldset div[contenteditable="true"]',
    'div[contenteditable="true"][role="textbox"]',
  ],
  /** Used only when the site ignores a synthetic drop or paste of files. */
  fileInput: [
    'input[type="file"][data-testid="file-upload"]',
    'fieldset input[type="file"]',
    'input[type="file"]',
  ],
}));
