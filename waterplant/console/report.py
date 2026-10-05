"""Plain text control report."""

from __future__ import annotations

from .plant import text_report_from_summary
from .runtime import Runtime


def text_report(rt: Runtime) -> str:
    """Render the operator report from one point-in-time plant summary."""

    return text_report_from_summary(rt.plant_summary.current())
