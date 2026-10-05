"""Immutable result of one full plant status pass.

A :class:`PlantStatus` is the single source every output is rendered from:
the JSON snapshot, the JSON descriptions and the plain text report are all
pure projections of this object, so a metric can never disagree between
them. A component whose read failed is kept as ``status="missing"`` with the
error recorded; it is never silently turned into a zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MISSING = "missing"
OK = "ok"


@dataclass(frozen=True)
class ComponentStatus:
    """One component captured at the summary instant."""

    name: str
    state: Any
    description: str
    error: str
    order: int

    @property
    def present(self) -> bool:
        return self.error == ""

    def as_dict(self) -> dict[str, object]:
        if self.present:
            return {"status": OK, "data": _jsonable(self.state.as_dict())}
        return {"status": MISSING, "error": self.error, "data": None}


@dataclass(frozen=True)
class PlantStatus:
    """Every component at one instant, plus how the pass went."""

    summary_id: str
    as_of: str
    components: tuple[ComponentStatus, ...]
    missing: tuple[str, ...]
    complete: bool
    attempts: int
    audit_id: str
    audit_recorded: bool
    fingerprint: str = ""
    order: tuple[str, ...] = field(default_factory=tuple)

    def component(self, name: str) -> ComponentStatus:
        for component in self.components:
            if component.name == name:
                return component
        raise KeyError(name)

    def states(self) -> dict[str, Any]:
        return {c.name: c.state for c in self.components if c.present}

    def present_count(self) -> int:
        return len(self.components) - len(self.missing)

    def snapshot_payload(self) -> dict[str, object]:
        """Legacy shaped snapshot, with every value taken from this pass."""

        payload: dict[str, object] = {}
        for component in self.components:
            if component.present:
                payload[component.name] = _jsonable(component.state.as_dict())
            else:
                # Explicitly marked as missing, never rendered as a zero.
                payload[component.name] = None
        return payload

    def envelope_payload(self) -> dict[str, object]:
        """Full envelope including per component status and pass metadata."""

        return {
            "summary_id": self.summary_id,
            "as_of": self.as_of,
            "complete": self.complete,
            "attempts": self.attempts,
            "present": self.present_count(),
            "total": len(self.components),
            "missing": list(self.missing),
            "audit_id": self.audit_id,
            "fingerprint": self.fingerprint,
            "components": {c.name: c.as_dict() for c in self.components},
        }

    def describe_payload(self) -> dict[str, str]:
        """One human readable line per component, from the captured state."""

        return {c.name: c.description for c in self.components}


def _jsonable(value: object) -> object:
    """Coerce frozen dataclass projections into plain JSON friendly values."""

    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
