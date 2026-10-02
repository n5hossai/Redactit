/**
 * Closes the two ways an AI site could read the user's content without passing through a
 * paste, a drop or a file input, the paths intercept.js checks (THREAT_MODEL §5).
 *
 * Runs in the page's own JavaScript world ("world": "MAIN") at document_start, so before
 * any of the site's scripts, on the three AI sites only. It makes
 * navigator.clipboard.read() and readText(), and window.showOpenFilePicker() and
 * showDirectoryPicker(), reject with NotAllowedError, as the browser does when the user
 * denies permission. A site then falls back to the paste and file-input paths, which
 * Redactit intercepts. Writing to the clipboard is untouched.
 *
 * What a page cannot do: the replacements are non-writable, non-configurable properties,
 * so assigning, redefining or deleting them fails (silently, or with a TypeError in strict
 * code), and they hold their own references to Promise and DOMException, so replacing
 * those globals changes nothing. The page can see that the functions are not native.
 *
 * What a page can still do: take untouched copies from another realm, such as a new
 * same-origin iframe read in the same task that creates it, before this script runs
 * there, or a worker. This is a guard that steers sites to the intercepted paths, not a
 * sandbox against a site set on bypassing it; the clipboard read would still need the
 * user's permission, and the pickers a user gesture.
 */
(() => {
  'use strict';

  const reject = Promise.reject.bind(Promise);
  const NotAllowed = DOMException;
  const defineProperty = Object.defineProperty;
  const MESSAGE = 'Redactit: paste or use the upload button instead, so the content is checked first.';

  /** Replaces `name` on `target` for good, if the browser offers it at all. */
  function lock(target, name, refuse) {
    if (!(name in target)) return;
    defineProperty(target, name, { value: refuse, writable: false, enumerable: true, configurable: false });
  }

  const refused = () => reject(new NotAllowed(MESSAGE, 'NotAllowedError'));
  // Named functions, so a site's error reports still say which call was refused.
  if (typeof Clipboard === 'function') {
    lock(Clipboard.prototype, 'read', function read() { return refused(); });
    lock(Clipboard.prototype, 'readText', function readText() { return refused(); });
  }
  lock(window, 'showOpenFilePicker', function showOpenFilePicker() { return refused(); });
  lock(window, 'showDirectoryPicker', function showDirectoryPicker() { return refused(); });
})();
