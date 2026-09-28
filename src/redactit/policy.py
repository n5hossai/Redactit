"""Policy: built-in defaults, overridden by the admin's managed file, then tightened (never
loosened) by the user's file; plus dial thresholds and the redaction decisions they drive.
See docs/PLAN.md §6 and docs/THREAT_MODEL.md T13.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from redactit.types import Decision, RedactitError, Span

Action = Literal["pseudonymize", "mask", "strike", "omit", "box"]
ACTIONS = set(get_args(Action))
DIAL = {1: 0.90, 2: 0.80, 3: 0.65, 4: 0.50, 5: 0.35}
CONTACT_TYPES = {"EMAIL", "PHONE", "ADDRESS"}
CONTACT_DELTA = 0.05
# PERSON and ADDRESS come from the GLiNER model, whose probabilities run lower than pattern
# scores for the same certainty (its model card works at 0.3-0.5). On the synthetic corpus
# true names scored 0.65-0.99 and false positives stayed rare down to 0.35, so model types
# get their own offset, never below the 0.30 the model reports from.
MODEL_TYPES, MODEL_DELTA, MODEL_FLOOR = {"PERSON", "ADDRESS"}, 0.15, 0.30
# In low_confidence_only mode, a model call this close above its threshold goes to review.
REVIEW_MARGIN = 0.15
_MODE_RANK = {"low_confidence_only": 0, "always": 1}
DEFAULT_POLICY = Path(__file__).with_name("policy.default.yaml")


class PolicyError(RedactitError, ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityConfig(_Strict):
    action: Action | None = None  # None in a user file keeps the admin's action
    locked: bool = False
    enabled: bool = True


class DialConfig(_Strict):
    position: int = Field(default=3, ge=1, le=5)
    admin_floor: int = Field(default=3, ge=1, le=5)


class ReviewConfig(_Strict):
    # An item awaiting review always blocks the send; no setting passes it through.
    mode: Literal["always", "low_confidence_only"] = "low_confidence_only"


class CustomTerms(_Strict):
    files: list[str] = []
    terms: list[str] = []


class SiteConfig(_Strict):
    dial: int = Field(ge=1, le=5)


class VaultConfig(_Strict):
    retention_days: int = Field(default=30, ge=0)


class Policy(_Strict):
    version: int = 1
    dial: DialConfig = DialConfig()
    review: ReviewConfig = ReviewConfig()
    entities: dict[str, EntityConfig] = {}
    custom_terms: CustomTerms = CustomTerms()
    allowlist: list[str] = []
    vault: VaultConfig = VaultConfig()
    sites: dict[str, SiteConfig] = {}

    def effective_dial(self, site: str | None = None) -> int:
        """max(position, admin_floor, the site's dial): a site rule can only tighten."""
        dial = max(self.dial.position, self.dial.admin_floor)
        return max(dial, self.sites[site].dial) if site in self.sites else dial

    def company_terms(self) -> list[str]:
        """Custom terms plus every line of the listed files, for the dictionary detector."""
        terms = list(self.custom_terms.terms)
        for f in map(Path, self.custom_terms.files):
            if not f.is_file():
                # Skipping it would silently stop redacting every client name it lists.
                raise PolicyError(f"custom_terms file not found: {f}")
            terms += [line.strip() for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        return terms

    def _threshold(self, entity_type: str, locked: bool, dial: int) -> float:
        if locked:
            return DIAL[5]  # locked types always use the most inclusive threshold
        base = DIAL[dial] - (CONTACT_DELTA if entity_type in CONTACT_TYPES else 0)
        if entity_type in MODEL_TYPES:
            base = max(MODEL_FLOOR, base - MODEL_DELTA)
        return round(base, 2)

    def decide(self, text: str, spans: list[Span], site: str | None = None) -> list[Decision]:
        dial = self.effective_dial(site)
        allow = {a.casefold() for a in self.allowlist}
        accepted = []
        for span in spans:
            cfg = self.entities.get(span.entity_type)
            if cfg is None or not (cfg.locked or cfg.enabled):
                continue  # unknown or disabled type
            threshold = self._threshold(span.entity_type, cfg.locked, dial)
            if not ((cfg.locked and span.validated) or span.score >= threshold):
                continue
            if not cfg.locked and text[span.start:span.end].casefold() in allow:
                continue  # the allowlist never applies to locked types
            accepted.append((span, cfg, threshold))
        return [self._decision(group, dial) for group in _overlapping(accepted)]

    def _decision(self, group: list, dial: int) -> Decision:
        """One decision covering a group of overlapping accepted spans.

        The union is redacted so no part of any accepted span survives: dropping the loser
        of an overlap once let "priya.okafor@northwind.com" keep its local part when the
        domain was a company term. The widest span names the entity; on a tie a validated,
        then higher-scoring, span wins.
        """
        span, cfg, threshold = max(group, key=lambda c: (c[0].end - c[0].start, c[0].validated, c[0].score))
        union = replace(span, start=min(c[0].start for c in group), end=max(c[0].end for c in group))
        needs_review = self.review.mode == "always" or (
            not span.validated and span.score < threshold + REVIEW_MARGIN
        )
        reason = f"{span.detector} score={span.score:.2f} dial={dial} threshold={threshold:.2f}"
        return Decision(union, cfg.action or "strike", f"entities.{span.entity_type}", reason, needs_review, threshold)


def _overlapping(accepted: list) -> list[list]:
    """Group accepted spans into runs that overlap each other, in text order."""
    groups: list[list] = []
    for c in sorted(accepted, key=lambda c: c[0].start):
        if groups and c[0].start < max(g[0].end for g in groups[-1]):
            groups[-1].append(c)
        else:
            groups.append([c])
    return groups


def _tighten_bool(field: str, m: EntityConfig | None, u: EntityConfig | None) -> bool:
    """The admin's effective value, which the user can switch on but never off.

    Checking only what each file explicitly set would let a user's `enabled: false` win
    whenever the admin left `enabled` at its default, which is a loosening.
    """
    if m is None:
        return getattr(u, field)
    return getattr(m, field) or (u is not None and field in u.model_fields_set and getattr(u, field))


def _merge(admin: Policy, user: Policy) -> Policy:
    floor = admin.dial.admin_floor
    # A user file that leaves the dial alone keeps the admin's position.
    position = user.dial.position if "position" in user.dial.model_fields_set else admin.dial.position
    entities = {}
    for etype in admin.entities.keys() | user.entities.keys():
        m, u = admin.entities.get(etype), user.entities.get(etype)
        # Every action removes the raw value, so choosing one is formatting, not loosening.
        action = (u.action if u else None) or (m.action if m else None) or "strike"
        entities[etype] = EntityConfig(
            action=action, locked=_tighten_bool("locked", m, u), enabled=_tighten_bool("enabled", m, u)
        )
    sites = {
        site: SiteConfig(dial=max(s.dial for s in (admin.sites.get(site), user.sites.get(site)) if s))
        for site in admin.sites.keys() | user.sites.keys()
    }
    mode = max(admin.review.mode, user.review.mode, key=_MODE_RANK.__getitem__)
    return Policy(
        dial=DialConfig(position=max(position, floor), admin_floor=floor),
        review=ReviewConfig(mode=mode),
        entities=entities,
        custom_terms=CustomTerms(
            files=list(dict.fromkeys(admin.custom_terms.files + user.custom_terms.files)),
            terms=list(dict.fromkeys(admin.custom_terms.terms + user.custom_terms.terms)),
        ),
        # Only the admin may exempt values: a user allowlist could exempt anything unlocked.
        allowlist=admin.allowlist,
        # Shorter retention keeps less data at rest, so the smaller value wins.
        vault=VaultConfig(retention_days=min(admin.vault.retention_days, user.vault.retention_days)),
        sites=sites,
    )


def _read(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise PolicyError(f"{path}: cannot read policy: {type(e).__name__}") from e
    if not isinstance(raw, dict):
        raise PolicyError(f"{path}: policy file must be a mapping")
    return raw


def _deep_update(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_update(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _parse(raw: dict, path: Path) -> Policy:
    try:
        policy = Policy(**raw)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise PolicyError(f"{path}: invalid value at '{loc}': {first['msg']}") from e
    # Term files are relative to the policy file that names them, not to the working dir.
    policy.custom_terms.files = [str((path.parent / f).resolve()) for f in policy.custom_terms.files]
    return policy


def load_policy(user: Path | None, managed: Path | None = None) -> Policy:
    """Built-in defaults, overridden by the admin's managed file, then tightened by the user's.

    The defaults are the base layer so that having no policy file never means redacting
    nothing. The admin may loosen a default (the admin owns that risk); the user never can.
    """
    defaults = _read(DEFAULT_POLICY)
    admin_raw = _deep_update(defaults, _read(managed)) if managed else defaults
    admin = _parse(admin_raw, managed or DEFAULT_POLICY)
    return _merge(admin, _parse(_read(user), user) if user else Policy())
