"""Human readable component summaries."""

from __future__ import annotations

from .runtime import Runtime


def collect(rt: Runtime) -> dict[str, str]:
    """Return the descriptions from one point-in-time plant summary."""

    return {
        name: observation.description
        for name, observation in rt.plant_summary.current().components.items()
    }
