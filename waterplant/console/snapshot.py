"""Full component snapshot, rendered from the shared plant status capture."""

from __future__ import annotations

from .runtime import Runtime
from .status import StatusService


def collect(
    rt: Runtime, key: str = "", *, service: StatusService | None = None
) -> dict[str, object]:
    """Return the snapshot projection of one coherent status pass."""

    status_service = service or rt.status
    return status_service.collect(key, audit=bool(key)).snapshot_payload()
