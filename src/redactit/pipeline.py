"""One engine behind every interface: detect -> decide -> apply -> audit."""

import unicodedata
from collections import Counter
from dataclasses import dataclass, replace

from redactit import __version__, models, safety
from redactit.audit import AuditLog
from redactit.detect.ner import GlinerNer
from redactit.detect.registry import Detector
from redactit.policy import Policy
from redactit.pseudonym import Pseudonymizer, replacements, splice
from redactit.types import Decision, Span
from redactit.vault import Vault


@dataclass(frozen=True)
class Result:
    text: str
    decisions: list[Decision]  # sorted by start; offsets index into the original input text
    replacements: list[str]  # what each decision became, e.g. "[PERSON_1]" or "****"


# Synthetic warm-up input: no real person, and it never reaches the vault or the audit log.
WARM_TEXT = "Warm-up: Jane Roe lives at 12 Elm Street, Springfield, and pays with 4111 1111 1111 1111."
WARM_IMAGE_TEXT = "Warm-up 123"


class Engine:
    """Loads the analyzer and model once (seconds), then redacts many texts (milliseconds).

    Loading comes in two stages so a host can answer pastes before images: construction
    (plus `warm_text`) readies text, and `warm_images` then loads OCR and face detection.
    """

    def __init__(self, policy: Policy, vault: Vault, audit: AuditLog | None = None, *,
                 verified: models.Verification | None = None):
        """`verified`: a `models.verify(models.TEXT_MODELS)` started before the caller's
        imports, so the half-second hash runs beside them instead of after."""
        safety.block_network()  # here, not only in the CLI, so every interface runs behind it
        self.policy, self.vault, self.audit = policy, vault, audit
        self.detector = Detector(company_terms=policy.company_terms())
        # The files stay locked (or are hashed again) until the session holds them, so the
        # bytes loaded are the bytes hashed (THREAT_MODEL T15).
        with (verified or models.verify(models.TEXT_MODELS)) as paths:
            self.ner = GlinerNer(paths["gliner/model.onnx"], paths["gliner/tokenizer.json"])
        purged = vault.purge(policy.vault.retention_days)
        if audit:
            audit.write("engine_start", version=__version__)
            audit.write("policy_loaded", dial=policy.effective_dial(), admin_floor=policy.dial.admin_floor,
                        entity_count=len(policy.entities), locked_count=sum(e.locked for e in policy.entities.values()))
            self._audit_verified(models.TEXT_MODELS)
            audit.write("vault_purge", purged_count=purged, retention_days=policy.vault.retention_days)

    def warm_text(self) -> None:
        """Run both detectors once: the first call pays one-off allocations (about half a second)."""
        self.detector.detect(WARM_TEXT)
        self.ner.detect(WARM_TEXT)

    def warm_images(self) -> None:
        """Verify and load the OCR and face models and run each once, so the first image
        does not pay for it. Calls the detectors directly: nothing reaches the vault or audit."""
        from PIL import Image, ImageDraw, ImageFont

        from redactit import ocr
        from redactit.formats import image

        img = Image.new("RGB", (320, 80), "white")
        ImageDraw.Draw(img).text((10, 20), WARM_IMAGE_TEXT, fill="black", font=ImageFont.load_default(size=24))
        ocr.read_lines(img)  # the first read builds RapidOCR from verified files
        image._faces(img)  # verifies YuNet on every call
        image._barcodes(img)
        if self.audit:
            self._audit_verified((*models.OCR_MODELS, image.YUNET))

    def _audit_verified(self, names) -> None:
        for name in names:  # each was verified before its session was built, so the event is true
            pin = models.LOCK[name]
            if "package" in pin:
                source, revision = "package", pin["version"]
            else:
                source, revision = "download", pin["url"].split("/resolve/")[1].split("/")[0]
            self.audit.write("model_verified", model=name.replace("/", ".").lower(), source=source,
                             revision=revision, sha256=pin["sha256"])

    def redact(self, text: str, scope: str, *, file_type: str = "txt", destination: str = "cli",
               site: str | None = None) -> Result:
        """`scope` keeps pseudonyms consistent: one chat, one folder run, one CLI call."""
        clean, where = _canonical(text)
        spans = _join_address_fragments(clean, self.detector.detect(clean) + self.ner.detect(clean))
        decisions = [_to_source(d, where) for d in self.policy.decide(clean, spans, site)]
        decisions.sort(key=lambda d: d.span.start)  # the order replacements() numbers labels in
        subs = replacements(text, decisions, Pseudonymizer(self.vault, scope))
        result = Result(splice(text, decisions, subs), decisions, subs)
        if self.audit:
            self.audit.write(
                "redaction",
                file_type=file_type,
                destination=destination,
                dial=self.policy.effective_dial(site),
                entity_counts=dict(Counter(d.span.entity_type for d in decisions)),
                decisions=[_audit_entry(d) for d in decisions],
            )
        return result


def _canonical(text: str) -> tuple[str, list[int]]:
    """Text for the detectors: each character NFKC-folded, invisible format characters
    (soft hyphens, zero-width spaces) dropped, plus each output character's source index.

    "Pri­ya" or a card number with zero-width spaces inside would otherwise hide from
    every pattern; the redaction still lands on the original text, hidden characters and all.
    """
    out, where = [], []
    for i, ch in enumerate(text):
        if unicodedata.category(ch) == "Cf":
            continue
        for c in unicodedata.normalize("NFKC", ch):
            out.append(c)
            where.append(i)
    return "".join(out), where


ADDRESS_GAP = 40  # characters between two address fragments that still make one address


def _join_address_fragments(text: str, spans: list[Span]) -> list[Span]:
    """Add one span covering address fragments that sit close together on one line.

    A street pattern and a postcode pattern once matched both ends of "4351 Betty Grove
    Apt. 571, South Jasonport, YT K6X 5C8" and the middle survived; the policy merges the
    joined span with its parts. Two addresses on one line get joined too, over-redacting
    the words between them, which is the safe side.
    """
    parts = sorted((s for s in spans if s.entity_type == "ADDRESS"), key=lambda s: s.start)
    joined = [
        replace(a, end=b.end, score=max(a.score, b.score), detector="address.join", validated=False)
        for a, b in zip(parts, parts[1:])
        if 0 < b.start - a.end <= ADDRESS_GAP and "\n" not in text[a.end:b.start]
    ]
    return spans + joined


def _to_source(d: Decision, where: list[int]) -> Decision:
    return replace(d, span=replace(d.span, start=where[d.span.start], end=where[d.span.end - 1] + 1))


def _audit_entry(d: Decision) -> dict:
    # Structured fields only; the audit log refuses anything that looks like free text.
    s = d.span
    return {"entity_type": s.entity_type, "detector": s.detector.lower(), "score": round(s.score, 3),
            "threshold": d.threshold, "rule_id": d.rule_id, "action": d.action, "validated": s.validated,
            "needs_review": d.needs_review}
