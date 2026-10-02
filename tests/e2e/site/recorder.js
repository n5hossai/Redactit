// The stand-in site's own scripts: a chat composer that behaves like the real ones, and
// a recorder of everything the site could observe. The browser tests read
// window.__seen, window.__attachments and window.__dom and check that no raw value
// ever reached them. Loaded first in <head>, as a site would load its earliest script.
(() => {
  const seen = (window.__seen = []);
  const attachments = (window.__attachments = []);
  const dom = (window.__dom = []);

  const describe = (f) => ({ name: f.name, size: f.size, type: f.type });

  function record(e) {
    const entry = { type: e.type, trusted: e.isTrusted, phase: e.eventPhase };
    const data = e.clipboardData || e.dataTransfer;
    if (data) {
      entry.text = data.getData('text/plain');
      entry.html = data.getData('text/html');
      entry.files = [...data.files].map(describe);
    }
    const t = e.target;
    if (t && t.type === 'file' && t.files) entry.files = [...t.files].map(describe);
    if ('data' in e) entry.data = e.data;
    if (t && typeof t.value === 'string' && t.type !== 'file') entry.value = t.value;
    seen.push(entry);
  }

  // As early and as wide as a page can listen: capture on window, bubble on document.
  for (const type of ['paste', 'drop', 'input', 'change', 'beforeinput']) {
    window.addEventListener(type, record, true);
    document.addEventListener(type, record, false);
  }

  new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === 'characterData') dom.push(r.target.data);
      for (const node of r.addedNodes) dom.push(node.textContent || '');
    }
  }).observe(document, { subtree: true, childList: true, characterData: true });

  document.addEventListener('DOMContentLoaded', () => {
    // A rich editor handles pastes itself, as ProseMirror does.
    const editor = document.querySelector('[data-role="rich"]');
    editor?.addEventListener('paste', (e) => {
      if (editor.dataset.handlesPaste !== 'yes') return;
      e.preventDefault();
      const text = e.clipboardData.getData('text/plain');
      if (text) editor.append(document.createTextNode(text));
      attachments.push(...e.clipboardData.files);
    });
    const zone = document.getElementById('dropzone');
    zone?.addEventListener('dragover', (e) => e.preventDefault());
    zone?.addEventListener('drop', (e) => {
      e.preventDefault();
      attachments.push(...e.dataTransfer.files);
    });
    document.getElementById('upload')?.addEventListener('change', (e) => {
      attachments.push(...e.target.files);
    });
  });

  /** Base64 of an attachment's bytes, for the test to inspect. */
  window.__read = async (i) => {
    const bytes = new Uint8Array(await attachments[i].arrayBuffer());
    let bin = '';
    for (let j = 0; j < bytes.length; j += 0x8000) bin += String.fromCharCode(...bytes.subarray(j, j + 0x8000));
    return btoa(bin);
  };
})();
