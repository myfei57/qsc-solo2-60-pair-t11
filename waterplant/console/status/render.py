"""Plain text projection of one captured :class:`PlantStatus`.

Every number in this report is pulled out of the captured component states,
never read from a component a second time, so it reconciles line for line
with the JSON snapshot and descriptions of the same pass.
"""

from __future__ import annotations

from typing import Any

from waterplant.ns import Stage

from .model import MISSING, PlantStatus

_HEADER = "waterplant control report"
_FILTER_STAGE = Stage.FILTER


def render_text(status: PlantStatus) -> str:
    """Render the operator report from one coherent status capture."""

    states = status.states()

    rows = [
        _HEADER,
        (
            f"summary {status.summary_id} as_of={status.as_of} complete={status.complete} "
            f"present={status.present_count()}/{len(status.components)} "
            f"missing={','.join(status.missing) or '-'} attempts={status.attempts} "
            f"fingerprint={status.fingerprint} audit={status.audit_id or '-'}"
        ),
    ]

    for name in status.order:
        rows.append(f"{name}: {status.component(name).description}")

    rows.extend(_pipeline_rows(status))
    rows.append(f"audit kinds={_audit_kinds(states.get('audit'))}")
    rows.extend(_filter_rows(states))
    rows.extend(_backwash_rows(states))
    rows.extend(_metric_rows(states))
    return "\n".join(rows) + "\n"


def _pipeline_rows(status: PlantStatus) -> list[str]:
    pipeline = status.component("pipeline")
    if not pipeline.present:
        return [f"pipeline stages={MISSING} contains_filter={MISSING}"]
    view = pipeline.state
    return [
        f"pipeline stages={view.stage_count} contains_filter={view.contains(_FILTER_STAGE)}"
    ]


def _audit_kinds(audit_state: Any) -> list[str]:
    if audit_state is None:
        return []
    seen: list[str] = []
    for entry in audit_state.entries:
        kind = entry.get("kind", "")
        if kind not in seen:
            seen.append(kind)
    return seen


def _filter_rows(states: dict[str, Any]) -> list[str]:
    bank = states.get("filter")
    if bank is None:
        return [f"filter beds={MISSING} active={MISSING} total_load={MISSING}"]
    beds = bank.beds
    active = sum(1 for bed in beds if not bed.closed)
    total_load = sum(bed.load for bed in beds)
    bed_ids = [bed.id for bed in beds]
    return [
        f"filter beds={len(beds)} active={active} total_load={total_load:.4f}",
        f"filter bed_ids={bed_ids}",
    ]


def _backwash_rows(states: dict[str, Any]) -> list[str]:
    backwash = states.get("backwash")
    if backwash is None:
        return [f"backwash queue={MISSING} drains={MISSING}"]
    return [
        f"backwash queue={len(backwash.commands)} drains={len(backwash.drains)}",
        f"backwash commands={backwash.commands}",
    ]


def _metric_rows(states: dict[str, Any]) -> list[str]:
    rows: list[str] = []

    quota = states.get("quota")
    well = states.get("clearwell")
    if quota is not None and well is not None:
        rows.append(f"quota value={quota.value:.4f} clearwell level={well.level:.4f}")

    coag = states.get("coag")
    chlor = states.get("chlor")
    if coag is not None and chlor is not None:
        rows.append(f"coag ratio={coag.ratio:.4f} chlorine target={chlor.target:.4f}")

    ph = states.get("ph")
    schedule = states.get("schedule")
    if ph is not None and schedule is not None:
        rows.append(
            f"ph value={ph.value:.4f} stable={ph.stable} scheduled={len(schedule.due)}"
        )

    trend = states.get("trend")
    if trend is not None:
        rows.append(f"intake trend samples={trend.samples}")

    inventory = states.get("inventory")
    if inventory is not None:
        rows.append(
            f"inventory balance={inventory.balance:.4f} "
            f"alerts={len(inventory.needs_reorder)}"
        )

    return rows
