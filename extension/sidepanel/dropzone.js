/**
 * The face drop zone: a file dropped on the card, or picked through the right folder,
 * goes through the worker's job port, and the face follows what really happens.
 *
 * Progress is built from real milestones only, never from a timer: the upload, the
 * host's `queued` and `redacting` stages, PDF pages (page N of M), and the result's
 * chunks. A stage with no finer report (one image, one text) holds its level while the
 * divider's dots show the work is alive; an elapsed clock says how long it has taken.
 *
 * One file at a time: a second drop while one runs is refused with a reason rather than
 * cancelling the first, so a stray drop cannot throw away a long PDF.
 */
import { Face } from './face.js';
import { $, announce, decodeText, findTarget, fmtSeconds, icon } from './common.js';
import { Blocked, MAX_PAYLOAD, extOf, kindOf, runJob } from './job.js';

/** Where each milestone puts the smile (0 to 1). */
const AT = { start: 0.02, uploaded: 0.1, queued: 0.12, redacting: 0.18, pagesEnd: 0.88, received: 0.99 };
const KIND_NAMES = { txt: 'Text file', md: 'Markdown file', docx: 'Word file', pdf: 'PDF', image: 'Image' };
const WORKING = {
  pdf: 'Opening the PDF',
  image: 'Reading the image and covering what it finds',
  text: 'Finding names, numbers and addresses',
};

/**
 * @typedef {object} Redacted
 * @property {string} job        the worker's id for the job (for attaching it to the chat)
 * @property {number} tabId      the chat it was redacted for
 * @property {string} name       a neutral file name: a name can identify someone
 * @property {Blob} blob         the redacted file
 * @property {string|null} text  the redacted text part, if any (all of a text file; a PDF's page text)
 */

/**
 * @param {{show: (r: Redacted) => void, clear: () => void}} result  the result actions
 */
export function initDropzone(result) {
  const face = new Face(document);
  const stage = $('stage');
  const dropBtn = $('dropBtn');
  const dragOut = $('dragOut');
  const picker = $('picker');
  const progress = $('progress');
  let run = null; // {cancel, file, started, timer, level}
  let count = 0;

  function setMode(mode) {
    stage.classList.toggle('running', mode === 'running' || mode === 'held');
    stage.classList.toggle('held', mode === 'held');
    stage.classList.toggle('done', mode === 'done');
    stage.classList.toggle('blocked', mode === 'blocked');
    if (mode !== 'done') stage.classList.remove('celebrate', 'lifting');
    const busy = mode === 'running' || mode === 'held';
    $('dropHint').hidden = busy;
    dropBtn.setAttribute('aria-disabled', String(busy));
    $('runActions').hidden = !busy;
    $('outHint').hidden = mode === 'done' || busy;
    const ready = mode === 'done';
    dragOut.classList.toggle('ready', ready);
    dragOut.setAttribute('draggable', String(ready));
    dragOut.setAttribute('aria-disabled', String(!ready));
    dragOut.tabIndex = ready ? 0 : -1;
    if (!ready) {
      dragOut.setAttribute('aria-label', busy
        ? 'Redacted copy, still filling.'
        : 'Redacted copy, empty. Drop a file on the right folder first.');
    }
  }

  let stepKind = null;
  function setStep(text, kind) {
    if (kind !== stepKind) {
      $('stepIcon').replaceChildren(kind === 'idle' ? '' : icon({ run: 'run', held: 'warn', done: 'good', err: 'bad' }[kind]));
      $('step').className = `step ${kind}`;
      stepKind = kind;
    }
    if ($('stepText').textContent !== text) $('stepText').textContent = text;
  }

  /** Moves the smile and the folders; the progressbar's value is the real one, not the eased frame. */
  function level(p) {
    if (!run) return;
    run.level = Math.max(run.level, p);
    face.moveTo(run.level);
    progress.setAttribute('aria-valuenow', String(Math.floor(run.level * 100)));
  }

  function tickClock() {
    if (run) $('clock').textContent = fmtSeconds((performance.now() - run.started) / 1000);
  }

  function refuse(text) {
    announce(text);
    setStep(text, 'err');
  }

  async function take(files) {
    const file = files && files[0];
    if (!file) return;
    if (run) {
      announce(`Redactit is still working on ${run.file.name}. Cancel it first.`);
      return;
    }
    const extra = files.length > 1 ? ` One file at a time: took ${file.name}.` : '';
    result.clear();
    $('fileName').textContent = file.name;
    const kind = kindOf(file);
    $('fileMeta').textContent = `${KIND_NAMES[kind] || 'File'}, ${sizeOf(file.size)}`;
    $('clock').textContent = '';
    face.setVariant(kind === 'image' ? 'image' : 'doc');
    face.jump(0, false);
    progress.setAttribute('aria-valuenow', '0');
    if (!kind) return block(new Blocked('unsupported'));
    if (file.size > MAX_PAYLOAD) return block(new Blocked('too_large'));
    const target = await findTarget();
    if (!target) return block(new Blocked('no_chat'));

    run = { file, started: performance.now(), level: 0, cancel: () => {}, timer: setInterval(tickClock, 200) };
    face.jump(0, true);
    setMode('running');
    setStep('Handing the file to Redactit', 'run');
    level(AT.start);
    announce(`Redacting ${file.name} for ${target.site}.${extra}`);

    let jobId = null;
    let lastPage = 0;
    const work = kind === 'pdf' || kind === 'image' ? WORKING[kind] : WORKING.text;
    const job = runJob(file, kind, target.id, {
      accepted: (id) => { jobId = id; },
      upload: (sent, total) => level(AT.start + (AT.uploaded - AT.start) * (sent / total)),
      held: (state) => {
        setMode('held');
        const why = state === 'ready-text'
          ? 'Redactit is still loading what reads images.'
          : "Redactit's app is starting.";
        setStep(`${why} Your file waits here, for up to 30 s.`, 'held');
        announce(`${why} Your file waits.`);
      },
      progress: (stage, page, pages) => {
        setMode('running');
        if (stage === 'queued') {
          level(AT.queued);
          setStep('Waiting its turn', 'run');
        } else if (page && pages) {
          level(AT.redacting + (AT.pagesEnd - AT.redacting) * ((page - 1) / pages));
          setStep(`Redacting page ${page} of ${pages}`, 'run');
          if (page !== lastPage) announce(`Page ${page} of ${pages}.`);
          lastPage = page;
        } else {
          level(AT.redacting);
          setStep(work, 'run');
        }
      },
      review: () => {
        setMode('held');
        setStep('Waiting for review below.', 'held');
      },
      receive: (received, total) => {
        setMode('running');
        level(AT.pagesEnd + (AT.received - AT.pagesEnd) * (received / Math.max(1, total)));
        setStep('Receiving the redacted copy', 'run');
      },
    });
    run.cancel = job.cancel;
    let out;
    try {
      out = await job.done;
    } catch (e) {
      return block(e instanceof Blocked ? e : new Blocked('extension_error'));
    }
    let redacted;
    try {
      redacted = toRedacted(out, kind, file.name, jobId, target.id, (count += 1));
    } catch {
      return block(new Blocked('extension_error'));
    }
    const seconds = (performance.now() - run.started) / 1000;
    stopRun();
    face.jump(1, true);
    progress.setAttribute('aria-valuenow', '100');
    $('clock').textContent = fmtSeconds(seconds);
    setMode('done');
    if (!face.reduced) {
      stage.classList.remove('celebrate');
      void stage.offsetWidth; // restart the animation
      stage.classList.add('celebrate');
    }
    setStep(`Redacted in ${fmtSeconds(seconds)}. The copy is ready.`, 'done');
    dragOut.setAttribute('aria-label', `Redacted file ${redacted.name}. Press Enter to attach it to the chat.`);
    result.show(redacted);
    announce(`Done. ${redacted.name} is redacted and ready.`);
    return undefined;
  }

  function stopRun() {
    if (!run) return;
    clearInterval(run.timer);
    run = null;
  }

  function block(e) {
    stopRun();
    face.jump(0, false);
    progress.setAttribute('aria-valuenow', '0');
    if (e.code === 'cancelled') {
      setMode('idle');
      setStep('Cancelled. Nothing was kept.', 'idle');
      announce('Cancelled.');
      return;
    }
    setMode('blocked');
    refuse(`Blocked. ${e.reason}`);
  }

  // Choosing or dropping a file
  dropBtn.addEventListener('click', () => {
    if (run) announce(`Redactit is still working on ${run.file.name}.`);
    else picker.click();
  });
  picker.addEventListener('change', () => {
    const files = [...picker.files];
    picker.value = '';
    take(files);
  });
  $('cancelBtn').addEventListener('click', () => {
    if (!run) return;
    setStep('Cancelling…', 'run');
    run.cancel();
  });
  const hasFiles = (dt) => Boolean(dt && dt.types && [...dt.types].includes('Files'));
  stage.addEventListener('dragover', (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = run ? 'none' : 'copy';
    if (!run) stage.classList.add('over');
  });
  stage.addEventListener('dragleave', (e) => {
    if (!stage.contains(e.relatedTarget)) stage.classList.remove('over');
  });
  stage.addEventListener('drop', (e) => {
    if (!hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    stage.classList.remove('over');
    take([...e.dataTransfer.files]);
  });
  // A file dropped anywhere else must not make the panel navigate to it.
  document.addEventListener('dragover', (e) => {
    if (e.defaultPrevented || !hasFiles(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'none';
  });
  document.addEventListener('drop', (e) => {
    if (hasFiles(e.dataTransfer)) e.preventDefault();
  });

  setMode('idle');
  setStep($('stepText').textContent, 'idle');
}

/**
 * The finished copy. It gets a neutral name (`redacted-N.ext`), as files handed to a page
 * do (PLAN §7): the original name can identify someone, and the engine does not check it.
 * @returns {Redacted}
 */
function toRedacted(out, kind, original, job, tabId, n) {
  const text = out.text ? decodeText(out.text) : null;
  if (kind === 'pdf' || kind === 'image') {
    if (!out.file) throw new Error('no file part');
    const ext = kind === 'pdf' ? 'pdf' : out.fileType === 'image/jpeg' ? 'jpg' : 'png';
    return { job, tabId, name: `redacted-${n}.${ext}`, blob: new Blob([out.file], { type: out.fileType }), text };
  }
  if (text === null) throw new Error('no text part');
  const ownExt = extOf(original);
  const ext = kind === 'txt' ? (/^[a-z0-9]{1,5}$/.test(ownExt) ? ownExt : 'txt') : 'md'; // Word comes back as Markdown
  const type = kind === 'txt' ? 'text/plain' : 'text/markdown';
  return { job, tabId, name: `redacted-${n}.${ext}`, blob: new Blob([out.text], { type }), text };
}

function sizeOf(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
