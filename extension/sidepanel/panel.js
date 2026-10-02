/**
 * Redactit's side panel: an extension page, the only place real values behind pseudonyms
 * may appear (THREAT_MODEL T6), and the reviewer for held pastes and files. Each part is
 * its own module; this file only starts them.
 *
 * What the user types, drops or gets back lives in this page's memory and DOM only:
 * nothing here is logged, stored or sent anywhere but the service worker.
 */
import { initDropzone } from './dropzone.js';
import { initRemap } from './remap.js';
import { initResult } from './result.js';
import { initReviews } from './reviews.js';
import { initStatus } from './status.js';
import { initTextbox } from './textbox.js';

initDropzone(initResult());
initTextbox();
const reviews = initReviews();
const remap = initRemap();
initStatus({
  onReviews: (list, mode) => reviews.update(list, mode),
  onTarget: (target) => remap.targetChanged(target),
});
