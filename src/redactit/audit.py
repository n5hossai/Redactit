"""JSONL audit log. A fixed field allowlist per event, each with a strict value shape,
structurally prevents a raw PII value from ever reaching the log (docs/THREAT_MODEL.md T10).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from redactit.policy import ACTIONS

_REASON_CODES = {"host_down", "timeout", "review_timeout", "policy_block"}
_ENTITY_TYPE_RE = re.compile(r"^[A-Z_]{2,32}$")
# Lower-case ids (detector names are lower-cased by the pipeline), or "entities.<TYPE>" as
# the policy names its rules. Mixed case is refused, so "Okafor" never passes as an id.
_RULE_ID_RE = re.compile(r"^([a-z0-9_.]{1,64}|entities\.[A-Z_]{2,32})$")
_FILE_TYPE_RE = re.compile(r"^[a-z0-9]{1,8}$")
_HOSTNAME_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))+$")
_DESTINATIONS = {"outbox", "clipboard", "cli"}
_MODEL_SOURCES = {"download", "package"}  # fetched by setup-models, or shipped inside a package


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _is_dial(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_entity_type(v: Any) -> bool:
    return isinstance(v, str) and bool(_ENTITY_TYPE_RE.match(v))


def _is_rule_id(v: Any) -> bool:
    return isinstance(v, str) and bool(_RULE_ID_RE.match(v))


def _is_action(v: Any) -> bool:
    return v in ACTIONS


def _is_file_type(v: Any) -> bool:
    return isinstance(v, str) and bool(_FILE_TYPE_RE.match(v))


def _is_destination(v: Any) -> bool:
    return isinstance(v, str) and (v in _DESTINATIONS or bool(_HOSTNAME_RE.match(v)))


def _is_sha256(v: Any) -> bool:
    return isinstance(v, str) and bool(re.fullmatch(r"[0-9a-f]{64}", v))


def _is_model_source(v: Any) -> bool:
    return v in _MODEL_SOURCES


def _is_reason_code(v: Any) -> bool:
    return v in _REASON_CODES


def _is_entity_counts(v: Any) -> bool:
    return isinstance(v, dict) and all(_is_entity_type(k) and _is_count(n) for k, n in v.items())


_DECISION_FIELDS: dict[str, Callable[[Any], bool]] = {
    "entity_type": _is_entity_type,
    "detector": _is_rule_id,
    "score": _is_number,
    "threshold": _is_number,
    "rule_id": _is_rule_id,
    "action": _is_action,
    "validated": _is_bool,
    "needs_review": _is_bool,
}


def _is_decisions(v: Any) -> bool:
    if not isinstance(v, list):
        return False
    for d in v:
        if not isinstance(d, dict) or set(d) != set(_DECISION_FIELDS):
            return False
        if not all(check(d[name]) for name, check in _DECISION_FIELDS.items()):
            return False
    return True


# Fixed field allowlist per event. Anything not named here, for any event, is rejected.
# review_decision and upload_blocked are written by the extension host (Phases 4-5); their
# shapes are fixed now so no later event can carry free text.
_EVENT_FIELDS: dict[str, dict[str, Callable[[Any], bool]]] = {
    "engine_start": {"version": _is_rule_id},
    "policy_loaded": {
        "dial": _is_dial,
        "admin_floor": _is_dial,
        "entity_count": _is_count,
        "locked_count": _is_count,
    },
    "model_verified": {"model": _is_rule_id, "source": _is_model_source, "revision": _is_rule_id, "sha256": _is_sha256},
    "redaction": {
        "file_type": _is_file_type,
        "destination": _is_destination,
        "entity_counts": _is_entity_counts,
        "dial": _is_dial,
        "decisions": _is_decisions,
    },
    "review_decision": {"rule_id": _is_rule_id, "action": _is_action, "approved": _is_bool},
    "upload_blocked": {"destination": _is_destination, "reason_code": _is_reason_code},
    "vault_purge": {"purged_count": _is_count, "retention_days": _is_count},
}


class AuditLog:
    """Append-only JSONL writer. `write` is the only way in, and it validates every field."""

    def __init__(self, path: Path) -> None:
        self._path = path
        # The native host writes from more than one thread, and two appends racing on one
        # file can overwrite each other's line on Windows.
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields: Any) -> None:
        allowed = _EVENT_FIELDS.get(event)
        if allowed is None:
            raise ValueError(f"event: unknown event {event!r}")

        extra = set(fields) - set(allowed)
        if extra:
            raise ValueError(f"{event}: unexpected field(s) {sorted(extra)}")
        missing = set(allowed) - set(fields)
        if missing:
            raise ValueError(f"{event}: missing field(s) {sorted(missing)}")
        for name, value in fields.items():
            if not allowed[name](value):
                raise ValueError(f"{event}: invalid value for field {name!r}")

        record = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **fields}
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
