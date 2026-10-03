/**
 * The drop zone's face: the right folder holds the file you dropped and drains, the left
 * folder fills with the redacted copy at the same rate, and the smile is the progress bar
 * with the percentage inside. This is the approved design's drawing, kept as it was; only
 * what drives it changed: the panel moves it to real progress from the service worker
 * instead of to simulated timings.
 *
 * The folders show an abstract page or screenshot, never the file itself: the panel draws
 * nothing from the user's content.
 */

// Folder geometry, in SVG units: the front panel runs from y 110 to y 208.
const PANEL_BOTTOM = 208;
const PANEL_H = 98;
/** The smile's rounded ends: a sliver of progress still shows as a cap, not a hairline. */
const CAP = 12;
/** Share of the remaining distance covered per frame: quick to follow, never a jump. */
const EASE = 0.14;

/** Abstract contents. `s` marks the parts that stand for sensitive values. */
const SHAPES = {
  doc: [
    { t: 'line', x: 0, y: 120, w: 30 }, { t: 'line', x: 34, y: 120, w: 44, s: 1 }, { t: 'line', x: 82, y: 120, w: 42 },
    { t: 'line', x: 0, y: 133, w: 128 },
    { t: 'line', x: 0, y: 146, w: 52 }, { t: 'line', x: 56, y: 146, w: 56, s: 1 },
    { t: 'line', x: 0, y: 159, w: 112 },
    { t: 'line', x: 0, y: 172, w: 22 }, { t: 'line', x: 26, y: 172, w: 38, s: 1 }, { t: 'line', x: 68, y: 172, w: 64 },
    { t: 'line', x: 0, y: 185, w: 92 },
    { t: 'line', x: 0, y: 198, w: 40 }, { t: 'line', x: 44, y: 198, w: 48, s: 1 },
  ],
  image: [
    { t: 'frame', x: 0, y: 116, w: 140, h: 86 },
    { t: 'dots', x: 7, y: 123 },
    { t: 'rule', x: 0, y: 129, w: 140 },
    { t: 'face', cx: 16, cy: 146, r: 9 },
    { t: 'line', x: 32, y: 142, w: 46, s: 1 },
    { t: 'line', x: 32, y: 153, w: 70 },
    { t: 'line', x: 8, y: 170, w: 92 },
    { t: 'line', x: 8, y: 181, w: 74 },
    { t: 'line', x: 8, y: 192, w: 50, s: 1 },
    { t: 'code', x: 106, y: 166, w: 26 },
  ],
};

export class Face {
  /**
   * @param {Document} doc  the panel's document, holding the face's SVG (ids as in panel.html)
   */
  constructor(doc) {
    const $ = (id) => /** @type {any} */ (doc.getElementById(id));
    this.svg = $('face');
    this.levelL = $('levelL');
    this.levelR = $('levelR');
    this.surfL = $('surfL');
    this.surfR = $('surfR');
    this.progPath = $('progPath');
    this.numOff = $('numOff');
    this.numOn = $('numOn');
    this.groups = { outDoc: $('outDoc'), outImage: $('outImage'), inDoc: $('inDoc'), inImage: $('inImage') };
    let len = 0;
    try {
      len = this.progPath.getTotalLength();
    } catch {
      len = 0;
    }
    this.progLen = len || 360;
    this.progPath.setAttribute('stroke-dasharray', `${this.progLen} ${this.progLen}`);

    const motion = globalThis.matchMedia ? globalThis.matchMedia('(prefers-reduced-motion: reduce)') : null;
    this.reduced = Boolean(motion && motion.matches);
    motion?.addEventListener?.('change', () => { this.reduced = motion.matches; });

    this.shown = 0;
    this.target = 0;
    this.loaded = false;
    this.frame = 0;

    this.paint(this.groups.outDoc, SHAPES.doc, 124, true);
    this.paint(this.groups.outImage, SHAPES.image, 124, true);
    this.paint(this.groups.inDoc, SHAPES.doc, 336, false);
    this.paint(this.groups.inImage, SHAPES.image, 336, false);
    this.draw(0, false);
  }

  /** A page for documents and text, a screenshot for images. */
  setVariant(kind) {
    const image = kind === 'image';
    this.groups.outDoc.toggleAttribute('hidden', image);
    this.groups.inDoc.toggleAttribute('hidden', image);
    this.groups.outImage.toggleAttribute('hidden', !image);
    this.groups.inImage.toggleAttribute('hidden', !image);
  }

  /** Straight to `p` (0 to 1), no easing: a new file, a reset, or the end of a run. */
  jump(p, loaded) {
    cancelAnimationFrame(this.frame);
    this.frame = 0;
    this.shown = this.target = p;
    this.loaded = loaded;
    this.draw(p, loaded);
  }

  /** Eases toward `p`. Progress only moves forward within a run. */
  moveTo(p) {
    this.target = Math.max(this.target, Math.min(1, p));
    this.loaded = true;
    if (this.reduced) {
      this.shown = this.target;
      this.draw(this.shown, true);
      return;
    }
    if (!this.frame) this.frame = requestAnimationFrame(() => this.tick());
  }

  tick() {
    this.frame = 0;
    const gap = this.target - this.shown;
    this.shown = Math.abs(gap) < 0.002 ? this.target : this.shown + gap * EASE;
    this.draw(this.shown, this.loaded);
    if (this.shown !== this.target) this.frame = requestAnimationFrame(() => this.tick());
  }

  /** p from 0 to 1: the right folder drains while the left fills at the same rate. */
  draw(p, loaded) {
    // With reduced motion the levels move in steps of 5%, not a continuous slide.
    const v = this.reduced && p > 0 && p < 1 ? Math.floor(p * 20) / 20 : p;
    const hL = PANEL_H * v;
    const hR = loaded ? PANEL_H * (1 - v) : 0;
    this.levelL.setAttribute('y', (PANEL_BOTTOM - hL).toFixed(2));
    this.levelL.setAttribute('height', hL.toFixed(2));
    this.levelR.setAttribute('y', (PANEL_BOTTOM - hR).toFixed(2));
    this.levelR.setAttribute('height', hR.toFixed(2));
    const mid = v > 0 && v < 1;
    setSurface(this.surfL, PANEL_BOTTOM - hL, mid);
    setSurface(this.surfR, PANEL_BOTTOM - hR, mid && loaded);
    const shown = v <= 0 ? 0 : v >= 1 ? this.progLen : CAP + (this.progLen - 2 * CAP) * v;
    this.progPath.setAttribute('stroke-dashoffset', (this.progLen - shown).toFixed(2));
    const n = String(p >= 1 ? 100 : Math.max(0, Math.floor(p * 100)));
    if (this.numOff.textContent !== n) {
      this.numOff.textContent = n;
      this.numOn.textContent = n;
    }
  }

  node(tag, attrs, parent) {
    // The SVG namespace is read from the face itself rather than written out here.
    const n = this.svg.ownerDocument.createElementNS(this.svg.namespaceURI, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    parent.appendChild(n);
    return n;
  }

  paint(group, shapes, ox, redacted) {
    for (const s of shapes) {
      const x = ox + (s.x || 0);
      if (s.t === 'line') {
        if (s.s && redacted) this.node('rect', { class: 'bar', x: x - 2, y: s.y - 4.5, width: s.w + 4, height: 9, rx: 1.5 }, group);
        else this.node('rect', { class: s.s ? 'key-mark' : 'ln', x, y: s.y - 2, width: s.w, height: 4, rx: 2 }, group);
      } else if (s.t === 'frame') {
        this.node('rect', { class: 'frame', x, y: s.y, width: s.w, height: s.h, rx: 4 }, group);
      } else if (s.t === 'rule') {
        this.node('rect', { class: 'ln', x, y: s.y - 0.75, width: s.w, height: 1.5 }, group);
      } else if (s.t === 'dots') {
        for (let i = 0; i < 3; i += 1) this.node('circle', { class: 'ln', cx: x + i * 6, cy: s.y, r: 1.8 }, group);
      } else if (s.t === 'face') {
        const cx = ox + s.cx;
        if (redacted) {
          this.node('rect', { class: 'bar', x: cx - 11, y: s.cy - 11, width: 22, height: 22, rx: 2 }, group);
        } else {
          this.node('circle', { class: 'key-mark', cx, cy: s.cy, r: s.r }, group);
          this.node('circle', { class: 'paper', cx, cy: s.cy - 2, r: 3.3 }, group);
          this.node('path', { class: 'paper', d: `M${cx - 5.6},${s.cy + 6.6} a5.6,4.6 0 0 1 11.2,0 z` }, group);
        }
      } else if (s.t === 'code') {
        if (redacted) {
          this.node('rect', { class: 'bar', x: x - 2, y: s.y - 2, width: s.w + 4, height: s.w + 4, rx: 2 }, group);
        } else {
          this.node('rect', { class: 'frame-key', x, y: s.y, width: s.w, height: s.w, rx: 1.5 }, group);
          for (const [dx, dy] of [[3, 3], [s.w - 10, 3], [3, s.w - 10]]) {
            this.node('rect', { class: 'key-mark', x: x + dx, y: s.y + dy, width: 7, height: 7, rx: 1 }, group);
          }
          this.node('rect', { class: 'key-mark', x: x + s.w - 9, y: s.y + s.w - 9, width: 4, height: 4 }, group);
          this.node('rect', { class: 'key-mark', x: x + 12, y: s.y + 12, width: 3, height: 3 }, group);
        }
      }
    }
  }
}

function setSurface(line, y, show) {
  line.setAttribute('y1', y.toFixed(2));
  line.setAttribute('y2', y.toFixed(2));
  line.setAttribute('visibility', show ? 'visible' : 'hidden');
}
