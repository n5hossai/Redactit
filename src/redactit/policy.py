"""Policy schema, managed+user layering, dial thresholds and redact/allow decisions.

Merge is tighten-only: a managed (admin) policy sets a floor that a user policy can
raise but never lower (docs/PLAN.md §6, docs/THREAT_MODEL.md T13).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from redactit.types import Decision, Span

ACTIONS = {"pseudonymize", "mask", "strike", "omit", "box"}
DIAL = {1: 0.90, 2: 0.80, 3: 0.65, 4: 0.50, 5: 0.35}
CONTACT_TYPES = {"EMAIL", "PHONE", "ADDRESS"}
CONTACT_DELTA = 0.05
# PERSON and ADDRESS come from the GLiNER model, whose probabilities run lower than pattern
# scores for the same certainty (its model card works at 0.3-0.5). On the synthetic corpus
# true names scored 0.65-0.99 and false positives stayed rare down to 0.35, so model types
# get their own offset, never below the 0.30 the model reports from.
MODEL_TYPES, MODEL_DELTA, MODEL_FLOOR = {"PERSON", "ADDRESS"}, 0.15, 0.30
_MODE_RANK = {"low_confidence_only": 0, "always": 1}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityConfig(_Strict):
    action: str | None = None  # None in a user file means "keep the admin's action"
    locked: bool = False
    enabled: bool = True

    @field_validator("action")
    @classmethod
    def _valid_action(cls, v: str | None) -> str | None:
        if v is not None and v not in ACTIONS:
            raise ValueError(f"must be one of {sorted(ACTIONS)}")
        return v


class DialConfig(_Strict):
    position: int = Field(default=3, ge=1, le=5)
    admin_floor: int = Field(default=3, ge=1, le=5)


class ReviewConfig(_Strict):
    mode: Literal["always", "low_confidence_only"] = "low_confidence_only"
    timeout_action: Literal["block"] = "block"


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
        """max(position, admin_floor, site dial if any) -- defensive even post-merge."""
        d = max(self.dial.position, self.dial.admin_floor)
        if site is not None and site in self.sites:
            d = max(d, self.sites[site].dial)
        return d

    def company_terms(self) -> list[str]:
        """Custom terms plus every line of the listed files, for the dictionary detector."""
        terms = list(self.custom_terms.terms)
        for f in self.custom_terms.files:
            p = Path(f)
            if p.is_file():
                terms += [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        return terms

    def _threshold(self, entity_type: str, locked: bool, dial: int) -> float:
        if locked:
            return DIAL[5]  # locked types always use the most inclusive threshold
        base = DIAL[dial]
        if entity_type in CONTACT_TYPES:
            base -= CONTACT_DELTA
        if entity_type in MODEL_TYPES:
            base = max(MODEL_FLOOR, base - MODEL_DELTA)
        return round(base, 2)

    def decide(self, text: str, spans: list[Span], site: str | None = None) -> list[Decision]:
        dial = self.effective_dial(site)
        allow = {a.casefold() for a in self.allowlist}
        candidates: list[tuple[Span, EntityConfig, float]] = []

        for span in spans:
            cfg = self.entities.get(span.entity_type)
            if cfg is None or not (cfg.locked or cfg.enabled):
                continue  # unknown or disabled type
            threshold = self._threshold(span.entity_type, cfg.locked, dial)
            passed = (cfg.locked and span.validated) or span.score >= threshold
            if not passed:
                continue
            if not cfg.locked and text[span.start : span.end].casefold() in allow:
                continue  # allowlist never applies to locked types
            candidates.append((span, cfg, threshold))

        # Overlap resolution: prefer validated, then higher score, then longer span.
        candidates.sort(key=lambda c: (c[0].validated, c[0].score, c[0].end - c[0].start), reverse=True)
        accepted: list[tuple[Span, EntityConfig, float]] = []
        for cand in candidates:
            s = cand[0]
            if any(s.start < a[0].end and a[0].start < s.end for a in accepted):
                continue
            accepted.append(cand)

        decisions = []
        for span, cfg, threshold in accepted:
            if self.review.mode == "always":
                needs_review = True
            else:
                needs_review = not span.validated and span.score < threshold + 0.15
            reason = f"{span.detector} score={span.score:.2f} dial={dial} threshold={threshold:.2f}"
            decisions.append(
                Decision(
                    span=span,
                    action=cfg.action,
                    rule_id=f"entities.{span.entity_type}",
                    reason=reason,
                    needs_review=needs_review,
                    threshold=threshold,
                )
            )
        decisions.sort(key=lambda d: d.span.start)
        return decisions


def _tighten_bool(field: str, m: EntityConfig | None, u: EntityConfig | None) -> bool:
    """The admin's effective value, which the user can switch on but never off.

    Checking only what each file explicitly set would let a user's `enabled: false` win
    whenever the admin left `enabled` at its default, which is a loosening.
    """
    if m is None:
        return getattr(u, field)
    return getattr(m, field) or (u is not None and field in u.model_fields_set and getattr(u, field))


def _dedup(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


def _merge(managed: Policy, user: Policy) -> Policy:
    admin_floor = managed.dial.admin_floor
    position = max(user.dial.position, admin_floor)

    entities: dict[str, EntityConfig] = {}
    for etype in managed.entities.keys() | user.entities.keys():
        m, u = managed.entities.get(etype), user.entities.get(etype)
        # Every action removes the raw value, so choosing one is formatting, not loosening.
        action = (u.action if u is not None else None) or (m.action if m is not None else None) or "strike"
        entities[etype] = EntityConfig(
            action=action,
            locked=_tighten_bool("locked", m, u),
            enabled=_tighten_bool("enabled", m, u),
        )

    sites: dict[str, SiteConfig] = {}
    for site in managed.sites.keys() | user.sites.keys():
        vals = [s.dial for s in (managed.sites.get(site), user.sites.get(site)) if s is not None]
        sites[site] = SiteConfig(dial=max(vals))

    mode = managed.review.mode if _MODE_RANK[managed.review.mode] >= _MODE_RANK[user.review.mode] else user.review.mode

    return Policy(
        version=user.version,
        dial=DialConfig(position=position, admin_floor=admin_floor),
        review=ReviewConfig(mode=mode, timeout_action="block"),
        entities=entities,
        custom_terms=CustomTerms(
            files=_dedup(managed.custom_terms.files + user.custom_terms.files),
            terms=_dedup(managed.custom_terms.terms + user.custom_terms.terms),
        ),
        allowlist=_dedup(managed.allowlist + user.allowlist),
        # Shorter retention is the safer side, so the tighter (smaller) value wins.
        vault=VaultConfig(retention_days=min(managed.vault.retention_days, user.vault.retention_days)),
        sites=sites,
    )


DEFAULT_POLICY = Path(__file__).with_name("policy.default.yaml")


def _read(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: policy file must be a mapping")
    return raw


def _deep_update(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_update(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _load_one(path: Path, base: dict | None = None) -> Policy:
    raw = _deep_update(base, _read(path)) if base else _read(path)
    try:
        policy = Policy(**raw)
    except ValidationError as e:
        first = e.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise ValueError(f"{path}: invalid value at '{loc}': {first['msg']}") from e
    base = path.parent
    policy.custom_terms.files = [
        f if Path(f).is_absolute() else str((base / f).resolve()) for f in policy.custom_terms.files
    ]
    return policy


def load_policy(user: Path | None, managed: Path | None = None) -> Policy:
    """Built-in defaults, overridden by the admin's managed file, then tightened by the user's.

    The defaults are the base layer so that having no policy file never means redacting
    nothing. The admin may loosen a default (the admin owns the risk); the user never can.
    """
    defaults = _read(DEFAULT_POLICY)
    admin = _load_one(managed, base=defaults) if managed is not None else _load_one(DEFAULT_POLICY)
    user_policy = _load_one(user) if user is not None else Policy()
    return _merge(admin, user_policy)
