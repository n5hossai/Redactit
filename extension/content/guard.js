/**
 * Closes the ways an AI site could get the user's content without passing through the
 * paths intercept.js checks (THREAT_MODEL §5): its own clipboard reads, its own file
 * pickers, and a file input whose events intercept.js cannot see.
 *
 * Runs in the page's own JavaScript world ("world": "MAIN") at document_start, so before
 * any of the site's scripts, on the three AI sites only.
 *
 * 1. navigator.clipboard.read() and readText(), and window.showOpenFilePicker() and
 *    showDirectoryPicker(), reject with NotAllowedError, as the browser does when the user
 *    denies permission. A site then falls back to paste and the file input, which Redactit
 *    intercepts. Writing to the clipboard is untouched.
 *
 * 2. A script can never open the browser's file chooser for one of the page's file inputs.
 *    intercept.js takes a pick from the `input` event on `window`, but an input that is not
 *    in the document (the usual `createElement('input').click()` upload button), or one in
 *    a shadow root, sends its events past `window` or hides itself from it, and a page can
 *    move an input out of the document while the chooser is open. So HTMLElement's click(),
 *    HTMLInputElement's showPicker() and EventTarget's dispatchEvent() of a click are
 *    replaced: when the element they would activate is a file input (itself, or a label's
 *    control, walked the way the browser builds the event's path, through slots and shadow
 *    roots), nothing is dispatched to the page's input. Instead intercept.js is asked
 *    (`redactit-pick` on window) to open its own chooser, on an input of its own that is in
 *    no document and that no page script can reach. What the user picks there is redacted,
 *    and only the redacted files come back (`redactit-picked`), which this script puts in
 *    the page's input with an `input` and a `change` event, as the browser would have. If
 *    intercept.js does not take the request, nothing opens: the pick is refused. Clicks
 *    the user makes are handled in intercept.js, which sees them first on `window`.
 *    Element.attachShadow() is wrapped only to remember each shadow root, closed ones
 *    included, so the walk can follow a slot inside one.
 *
 * What a page cannot do: every replacement is a non-writable, non-configurable property,
 * so assigning, redefining or deleting it fails (silently, or with a TypeError in strict
 * code). Everything they call was taken when this script ran, before any page script: the
 * browser's own functions and getters, called directly, never looked up again on objects a
 * page can change. A page can see that the functions are not native, and that a click()
 * which opens a file chooser dispatches no click event.
 *
 * What a page can still do, all of it deliberate (THREAT_MODEL §5; tests/e2e/test_guard.py
 * records the browser's side of it). Frames and popups run this script too, and one with
 * no document to load (an iframe without src, or about:blank, a window.open('')) has it in
 * the very task that creates it; workers have neither the clipboard nor file pickers. But
 * an iframe given srcdoc, a blob: URL or a same-origin URL, or a popup opened on one, gets
 * this script only when that document starts, and in the task that creates it its window,
 * which that document goes on to use, is one this script has not reached. A page set on
 * bypassing Redactit can take the browser's own click(), dispatchEvent() or clipboard
 * reads from there, and so open the browser's chooser on its own input, or listen in that
 * frame for a paste before intercept.js does. A frame from another origin inside the site
 * gets neither script. The clipboard read would still need the user's permission, and a
 * chooser a user gesture.
 */
(() => {
  'use strict';

  const reject = Promise.reject.bind(Promise);
  const NotAllowed = DOMException;
  const PageEvent = Event;
  const AskEvent = CustomEvent;
  const NoMap = WeakMap;
  const { defineProperty, getOwnPropertyDescriptor } = Object;
  const MESSAGE = 'Redactit: paste or use the upload button instead, so the content is checked first.';

  /** `fn` as a plain function of (this, ...args), immune to later changes to call or bind. */
  const uncurry = (fn) => Function.prototype.call.bind(fn);
  const getter = (proto, name) => uncurry(getOwnPropertyDescriptor(proto, name).get);
  /** The value of a brand-checked getter, or undefined for an object of another kind. */
  const as = (get, obj) => {
    try {
      return get(obj);
    } catch {
      return undefined;
    }
  };

  const nativeClick = uncurry(HTMLElement.prototype.click);
  const nativePicker = typeof HTMLInputElement.prototype.showPicker === 'function'
    ? uncurry(HTMLInputElement.prototype.showPicker) : null;
  const nativeDispatch = uncurry(EventTarget.prototype.dispatchEvent);
  const nativeAttachShadow = uncurry(Element.prototype.attachShadow);
  const listen = uncurry(EventTarget.prototype.addEventListener);
  const preventDefault = uncurry(Event.prototype.preventDefault);
  const eventType = getter(Event.prototype, 'type');
  const bubbles = getter(Event.prototype, 'bubbles');
  const composed = getter(Event.prototype, 'composed');
  const cancelled = getter(Event.prototype, 'defaultPrevented');
  const parentNode = getter(Node.prototype, 'parentNode');
  const hostOf = getter(ShadowRoot.prototype, 'host');
  const elementSlot = getter(Element.prototype, 'assignedSlot');
  const textSlot = getter(Text.prototype, 'assignedSlot');
  const hasAttribute = uncurry(Element.prototype.hasAttribute);
  const inputType = getter(HTMLInputElement.prototype, 'type');
  const inputDisabled = getter(HTMLInputElement.prototype, 'disabled');
  const inputMultiple = getter(HTMLInputElement.prototype, 'multiple');
  const inputAccept = getter(HTMLInputElement.prototype, 'accept');
  const inputDirectory = getter(HTMLInputElement.prototype, 'webkitdirectory');
  const setInputFiles = uncurry(getOwnPropertyDescriptor(HTMLInputElement.prototype, 'files').set);
  const labelControl = getter(HTMLLabelElement.prototype, 'control');
  const buttonType = getter(HTMLButtonElement.prototype, 'type');
  const anchorHref = getter(HTMLAnchorElement.prototype, 'href');
  const areaHref = getter(HTMLAreaElement.prototype, 'href');
  const querySelectorAll = uncurry(DocumentFragment.prototype.querySelectorAll);
  const listLength = getter(NodeList.prototype, 'length');
  const listItem = uncurry(NodeList.prototype.item);
  const assignedNodes = uncurry(HTMLSlotElement.prototype.assignedNodes);
  const dragData = getter(DragEvent.prototype, 'dataTransfer');
  const transferFiles = getter(DataTransfer.prototype, 'files');
  const mapGet = uncurry(WeakMap.prototype.get);
  const mapSet = uncurry(WeakMap.prototype.set);

  /** Replaces `name` on `target` for good, if the browser offers it at all. */
  function lock(target, name, replacement) {
    if (!(name in target)) return;
    defineProperty(target, name, { value: replacement, writable: false, enumerable: true, configurable: false });
  }

  // --- 1. the site's own clipboard reads and file pickers ----------------------------------

  const refused = () => reject(new NotAllowed(MESSAGE, 'NotAllowedError'));
  // Named functions, so a site's error reports still say which call was refused.
  if (typeof Clipboard === 'function') {
    lock(Clipboard.prototype, 'read', function read() { return refused(); });
    lock(Clipboard.prototype, 'readText', function readText() { return refused(); });
  }
  lock(window, 'showOpenFilePicker', function showOpenFilePicker() { return refused(); });
  lock(window, 'showDirectoryPicker', function showDirectoryPicker() { return refused(); });

  // --- 2. file inputs ----------------------------------------------------------------------

  /** Every shadow root made here, closed ones too: a slot inside one is part of a click's path. */
  const roots = new NoMap();
  /** The page's file input waiting for the files picked in Redactit's chooser. */
  let pending = null;

  const isFileInput = (node) => as(inputType, node) === 'file';

  /** The slot `node` is shown in, also inside a closed shadow root, or null. */
  function slotOf(node) {
    const open = as(elementSlot, node) || as(textSlot, node);
    if (open) return open;
    const root = mapGet(roots, as(parentNode, node) || {});
    if (!root) return null;
    const slots = querySelectorAll(root, 'slot');
    for (let i = 0; i < listLength(slots); i += 1) {
      const slot = listItem(slots, i);
      const nodes = assignedNodes(slot);
      for (let j = 0; j < nodes.length; j += 1) if (nodes[j] === node) return slot;
    }
    return null;
  }

  /** The next node on an event's path: the slot, else the parent, else a shadow root's host
   * when the event leaves the root. */
  function parentOnPath(node, leavesRoots) {
    const slot = slotOf(node);
    if (slot) return slot;
    const parent = as(parentNode, node);
    if (!parent) return null;
    const host = as(hostOf, parent);
    if (host === undefined) return parent;
    return leavesRoots ? host : null;
  }

  /** Elements whose own activation comes before any label around them. */
  function activatesOtherwise(node) {
    const type = as(inputType, node);
    if (type !== undefined) return type !== 'hidden';
    if (as(buttonType, node) !== undefined) return true;
    return (as(anchorHref, node) !== undefined || as(areaHref, node) !== undefined) && hasAttribute(node, 'href');
  }

  /**
   * The file input a click on `target` would open, as the browser picks its activation
   * target: the first node on the path that has an activation behaviour. A label counts as
   * its control. Anything this cannot place counts as not a file input: the click then goes
   * through, and intercept.js still sees it on `window`, or the pick, if the input stays in
   * the document.
   */
  function fileInputFor(target, goesUp, leavesRoots) {
    for (let node = target; node; node = goesUp ? parentOnPath(node, leavesRoots) : null) {
      if (isFileInput(node)) return node;
      const control = as(labelControl, node);
      if (control !== undefined) return isFileInput(control) ? control : null;
      if (activatesOtherwise(node)) return null;
    }
    return null;
  }

  /**
   * Asks intercept.js for its chooser, for the page's `input`. True if it took the request;
   * otherwise nothing opens. Never opens the browser's chooser for the page's input.
   */
  function pick(input) {
    const options = (as(inputMultiple, input) ? 'm' : '-') + (as(inputDirectory, input) ? 'd' : '-')
      + (as(inputAccept, input) || '');
    const ask = new AskEvent('redactit-pick', { __proto__: null, cancelable: true, detail: options });
    nativeDispatch(window, ask);
    if (!cancelled(ask)) return false;
    pending = input;
    return true;
  }

  /** The redacted files from Redactit's chooser go into the page's input, as a pick would. */
  listen(window, 'redactit-picked', (e) => {
    const input = pending;
    pending = null;
    const data = as(dragData, e);
    const files = data && as(transferFiles, data);
    if (!input || !files) return;
    setInputFiles(input, files);
    nativeDispatch(input, new PageEvent('input', { __proto__: null, bubbles: true, composed: true }));
    nativeDispatch(input, new PageEvent('change', { __proto__: null, bubbles: true }));
    preventDefault(e); // tells intercept.js the files went in
  }, true);

  listen(window, 'redactit-pick-cancelled', () => {
    const input = pending;
    pending = null;
    if (input) nativeDispatch(input, new PageEvent('cancel', { __proto__: null, bubbles: true }));
  }, true);

  lock(HTMLElement.prototype, 'click', function click() {
    const input = fileInputFor(this, true, true);
    if (!input) return nativeClick(this);
    if (!as(inputDisabled, input)) pick(input);
    return undefined;
  });

  if (nativePicker) {
    lock(HTMLInputElement.prototype, 'showPicker', function showPicker() {
      if (!isFileInput(this)) return nativePicker(this);
      if (as(inputDisabled, this)) throw new NotAllowed(MESSAGE, 'InvalidStateError');
      if (!pick(this)) throw new NotAllowed(MESSAGE, 'NotAllowedError');
      return undefined;
    });
  }

  lock(EventTarget.prototype, 'dispatchEvent', function dispatchEvent(event) {
    const type = as(eventType, event);
    // Only a click opens a chooser in Chromium; DOMActivate is held to the same rule in case.
    if (type === 'click' || type === 'DOMActivate') {
      const input = fileInputFor(this, as(bubbles, event) === true, as(composed, event) === true);
      if (input) {
        if (!as(inputDisabled, input)) pick(input);
        return true;
      }
    }
    return nativeDispatch(this, event);
  });

  lock(Element.prototype, 'attachShadow', function attachShadow(init) {
    const root = nativeAttachShadow(this, init);
    mapSet(roots, this, root);
    return root;
  });
})();
