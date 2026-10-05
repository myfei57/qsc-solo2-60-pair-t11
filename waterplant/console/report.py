"""Plain text control report, rendered from the shared plant status capture."""

from __future__ import annotations

from .runtime import Runtime
from .status import StatusService, render_text


def text_report(
    rt: Runtime, key: str = "", *, service: StatusService | None = None
) -> str:
    """Render the operator report that mirrors the JSON snapshot exactly."""

    status_service = service or rt.status
    return render_text(status_service.collect(key, audit=bool(key)))
