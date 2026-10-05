"""Full component snapshot exposed by the console."""

from __future__ import annotations

from .plant import snapshot_from_summary
from .runtime import Runtime


def collect(rt: Runtime) -> dict[str, object]:
    """Project one point-in-time plant summary into snapshot form."""

    return snapshot_from_summary(rt.plant_summary.current())
