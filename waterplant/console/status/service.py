"""Single collection point for the whole plant status.

Policy fixed here:

* One pass reads every component exactly once, in registry (treatment line)
  order, under one lock. The lock serialises a summary pass against other
  passes so a pass sees one coherent instant rather than a mix of updates.
* A component that cannot be read is a *partial failure*: it is recorded as
  missing and the pass continues. Missing is never the same as zero.
* A pass is idempotent under its key: the first attempt collects and audits
  once; repeating the same key returns the cached result without collecting
  or auditing again. A key that finished incomplete can be recovered, which
  re-reads only the missing components and writes at most one recovery audit
  entry when something actually comes back.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Callable

from ..runtime import Runtime
from .model import ComponentStatus, PlantStatus, _jsonable
from .registry import COMPONENT_ORDER, ComponentSpec, component_specs

AUDIT_KIND = "plant-status"
MISSING_MARKER = "__missing__"
_CACHE_LIMIT = 128


class StatusService:
    """Collects and remembers full plant status summaries for a runtime."""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime
        self._lock = threading.RLock()
        self._specs: dict[str, ComponentSpec[object]] = {
            spec.name: spec for spec in component_specs()
        }
        self._readers: dict[str, Callable[[Runtime], object]] = {}
        self._cache: dict[str, PlantStatus] = {}

    # -- test / wiring hooks -------------------------------------------------

    def fail_reader(self, name: str, message: str = "injected read failure") -> None:
        """Force one component's read to fail, simulating a partial outage."""

        if name not in self._specs:
            raise KeyError(name)

        def broken(rt: Runtime) -> object:
            raise RuntimeError(message)

        self._readers[name] = broken

    def restore_reader(self, name: str) -> None:
        self._readers.pop(name, None)

    # -- public API ----------------------------------------------------------

    def collect(
        self,
        key: str = "",
        *,
        audit: bool = True,
        force: bool = False,
    ) -> PlantStatus:
        """Return one coherent full plant status.

        ``key`` makes the call idempotent: the first attempt wins, later
        calls with the same key return it (after a recovery when needed).
        ``audit=False`` produces an unaudited projection for internal use.
        ``force=True`` starts a brand new pass even for a known key.
        """

        with self._lock:
            cached = None if force or not key else self._cache.get(key)
            if cached is not None:
                if cached.complete:
                    return cached
                recovered = self._recover(cached)
                if recovered is cached:
                    # Nothing came back: the retry is idempotent, return as-is.
                    return cached
                if key:
                    self._remember(key, recovered)
                return recovered

            status = self._full_pass(audit=audit)
            if key:
                self._remember(key, status)
            return status

    # -- passes --------------------------------------------------------------

    def _full_pass(self, *, audit: bool) -> PlantStatus:
        as_of = _now()
        summary_id = uuid.uuid4().hex
        attempts = 1
        components = tuple(self._read_component(spec) for spec in self._ordered_specs())
        missing = tuple(c.name for c in components if not c.present)
        fingerprint = _fingerprint(components)

        audit_id = ""
        audit_recorded = False
        if audit:
            entry = self._runtime.auditor.record(
                AUDIT_KIND,
                json.dumps(
                    {
                        "summary_id": summary_id,
                        "as_of": as_of,
                        "attempt": attempts,
                        "present": len(components) - len(missing),
                        "total": len(components),
                        "missing": list(missing),
                        "complete": not missing,
                        "fingerprint": fingerprint,
                    },
                    ensure_ascii=False,
                ),
            )
            audit_id = entry.id
            audit_recorded = True

        return PlantStatus(
            summary_id=summary_id,
            as_of=as_of,
            components=components,
            missing=missing,
            complete=not missing,
            attempts=attempts,
            audit_id=audit_id,
            audit_recorded=audit_recorded,
            fingerprint=fingerprint,
            order=COMPONENT_ORDER,
        )

    def _recover(self, previous: PlantStatus) -> PlantStatus:
        """Re-read only the components that were missing on the cached pass."""

        still_missing: list[str] = []
        recovered_names: list[str] = []
        components: list[ComponentStatus] = []
        for component in previous.components:
            if component.present:
                components.append(component)
                continue
            spec = self._specs[component.name]
            fresh = self._read_component(spec)
            components.append(fresh)
            if fresh.present:
                recovered_names.append(fresh.name)
            else:
                still_missing.append(fresh.name)

        if not recovered_names:
            # Failed retries change nothing: recovery stays idempotent.
            return previous

        attempts = previous.attempts + 1
        fingerprint = _fingerprint(tuple(components))
        audit_id = previous.audit_id
        audit_recorded = previous.audit_recorded

        # One recovery audit entry, written only when something came back.
        entry = self._runtime.auditor.record(
            AUDIT_KIND,
            json.dumps(
                {
                    "summary_id": previous.summary_id,
                    "as_of": previous.as_of,
                    "recovered_at": _now(),
                    "attempt": attempts,
                    "recovered": recovered_names,
                    "missing": still_missing,
                    "complete": not still_missing,
                    "fingerprint": fingerprint,
                },
                ensure_ascii=False,
            ),
        )
        audit_id = entry.id
        audit_recorded = True

        return PlantStatus(
            summary_id=previous.summary_id,
            as_of=previous.as_of,
            components=tuple(components),
            missing=tuple(still_missing),
            complete=not still_missing,
            attempts=attempts,
            audit_id=audit_id,
            audit_recorded=audit_recorded,
            fingerprint=fingerprint,
            order=previous.order,
        )

    # -- internals -----------------------------------------------------------

    def _ordered_specs(self) -> list[ComponentSpec[object]]:
        return [self._specs[name] for name in COMPONENT_ORDER]

    def _read_component(self, spec: ComponentSpec[object]) -> ComponentStatus:
        order = COMPONENT_ORDER.index(spec.name)
        reader = self._readers.get(spec.name, spec.read)
        try:
            state = reader(self._runtime)
        except Exception as exc:  # noqa: BLE001 - one dead component is a partial failure
            return ComponentStatus(
                name=spec.name,
                state=None,
                description=f"{spec.name}: MISSING ({exc})",
                error=str(exc) or exc.__class__.__name__,
                order=order,
            )
        try:
            description = spec.render(self._runtime, state)
        except Exception as exc:  # noqa: BLE001 - a broken projection is unreadable too
            return ComponentStatus(
                name=spec.name,
                state=None,
                description=f"{spec.name}: MISSING ({exc})",
                error=f"render failed: {exc}",
                order=order,
            )
        return ComponentStatus(
            name=spec.name, state=state, description=str(description), error="", order=order
        )

    def _remember(self, key: str, status: PlantStatus) -> None:
        self._cache[key] = status
        if len(self._cache) > _CACHE_LIMIT:
            oldest = next(iter(self._cache))
            self._cache.pop(oldest, None)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fingerprint(components: tuple[ComponentStatus, ...]) -> str:
    """Hash of every captured value, so outputs can prove shared口径."""

    # Every output of one pass carries this hash; matching hashes mean the
    # snapshot, descriptions and report were rendered from identical values.
    body: dict[str, object] = {}
    for component in components:
        if component.present:
            body[component.name] = _jsonable(component.state.as_dict())
        else:
            body[component.name] = MISSING_MARKER
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
