"""Human readable component summaries, from the shared plant status capture."""

from __future__ import annotations

from .runtime import Runtime
from .status import StatusService


def collect(
    rt: Runtime, key: str = "", *, service: StatusService | None = None
) -> dict[str, str]:
    """Return one description line per component from one coherent pass."""

    status_service = service or rt.status
    return status_service.collect(key, audit=bool(key)).describe_payload()
