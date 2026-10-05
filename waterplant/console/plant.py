"""Consistent whole-plant status summary.

The summary has one collector and three projections (snapshot, descriptions and
the text report).  A component that cannot be read is recorded as ``missing``
or ``failed``; zero is reserved for a successful reading whose value really is
zero.  Collection is serialised with component updates by ``Runtime.guard``.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Callable

from waterplant.ns import Stage, treatment_line
from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .runtime import Runtime

OK = "ok"
MISSING = "missing"
FAILED = "failed"
DEGRADED = "degraded"

SUMMARY_AUDIT_KEY = "plant:summary:audit"
SUMMARY_KEY_PREFIX = "plant:summary:"
LATEST_SUMMARY_KEY = "plant:summary:latest"

ComponentReader = Callable[[Runtime], tuple[object, str]]


@dataclass(frozen=True)
class ComponentObservation:
    """The frozen state and description produced by one component read."""

    name: str
    stage: str
    order: int
    status: str
    state: object = field(default_factory=dict)
    description: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "stage": self.stage,
            "order": self.order,
            "status": self.status,
            "state": self.state,
            "description": self.description,
        }
        if self.error:
            payload["error"] = self.error
        return payload


@dataclass(frozen=True)
class PlantSummary:
    """All projections and metadata for one point-in-time collection."""

    summary_id: str
    collected_at: str
    order: str
    policy: str
    status: str
    components: dict[str, ComponentObservation]
    telemetry: dict[str, object]
    fingerprint: str
    idempotency_key: str
    replayed: bool = False
    audit: dict[str, object] = field(default_factory=dict)

    @property
    def missing(self) -> list[str]:
        return [name for name, item in self.components.items() if item.status == MISSING]

    @property
    def failed(self) -> list[str]:
        return [name for name, item in self.components.items() if item.status == FAILED]

    def as_dict(self) -> dict[str, object]:
        return {
            "summary_id": self.summary_id,
            "collected_at": self.collected_at,
            "order": self.order,
            "policy": self.policy,
            "status": self.status,
            "components": {
                name: component.as_dict() for name, component in self.components.items()
            },
            "missing": self.missing,
            "failed": self.failed,
            "snapshot": snapshot_from_summary(self),
            "describe": describe_from_summary(self),
            "telemetry": self.telemetry,
            "fingerprint": self.fingerprint,
            "idempotency_key": self.idempotency_key,
            "replayed": self.replayed,
            "audit": self.audit,
            "report": text_report_from_summary(self),
        }


def _read_pipeline(rt: Runtime) -> tuple[dict[str, object] | list[dict[str, str]], str]:
    line = treatment_line()
    return [step.as_dict() for step in line.steps()], line.describe()


def _read_store(rt: Runtime) -> tuple[dict[str, object], str]:
    # The summary's own audit/payload keys are bookkeeping, not plant state.
    keys = [key for key in rt.store.keys() if not key.startswith(SUMMARY_KEY_PREFIX)]
    return {"keys": keys, "size": len(keys)}, f"store keys={len(keys)}"


def _read_pair(reader: Callable[[], object]) -> tuple[dict[str, object], str]:
    state = reader.state()
    return state.as_dict(), reader.describe()


def _read_intake(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.flow_repository)


def _read_coag(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.coag_doser)


def _read_chlor(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.chlor_doser)


def _read_filter(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.bank)


def _read_backwash(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.backwash)


def _read_turbidity(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.sampler)


def _read_flow(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.calibration)


def _read_clearwell(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.well)


def _read_quota(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.accumulator)


def _read_audit(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.auditor)


def _read_ph(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.stabilizer)


def _read_schedule(rt: Runtime) -> tuple[dict[str, object], str]:
    state = rt.scheduler.state(rt.bank)
    return state.as_dict(), rt.scheduler.describe(rt.bank)


def _read_trend(rt: Runtime) -> tuple[dict[str, object], str]:
    return rt.trend.stats().as_dict(), rt.trend.describe()


def _read_inventory(rt: Runtime) -> tuple[dict[str, object], str]:
    return _read_pair(rt.inventory)


@dataclass(frozen=True)
class ComponentSpec:
    """Canonical source and treatment-line position of one component."""

    name: str
    stage: Stage
    order: int
    attribute: str | None
    reader: ComponentReader


# Output order follows the existing operator report, which follows the process
# where possible.  Auxiliary instruments remain after the last treatment stage.
def component_specs() -> tuple[ComponentSpec, ...]:
    """Return the fixed plant component catalogue in collection order."""

    stage_order = {stage: index for index, stage in enumerate(treatment_line().stages)}
    auxiliary = len(stage_order)

    def aux(stage: Stage, position: int) -> int:
        return auxiliary * 100 + stage_order.get(stage, auxiliary) * 10 + position

    return (
        ComponentSpec("pipeline", Stage.INTAKE, -1, None, _read_pipeline),
        ComponentSpec("store", Stage.INTAKE, -2, "store", _read_store),
        ComponentSpec(
            "intake", Stage.INTAKE, stage_order[Stage.INTAKE], "flow_repository", _read_intake
        ),
        ComponentSpec("coag", Stage.COAG, stage_order[Stage.COAG], "coag_doser", _read_coag),
        ComponentSpec("chlor", Stage.CHLOR, stage_order[Stage.CHLOR], "chlor_doser", _read_chlor),
        ComponentSpec("filter", Stage.FILTER, stage_order[Stage.FILTER], "bank", _read_filter),
        ComponentSpec("backwash", Stage.BACKWASH, aux(Stage.FILTER, 1), "backwash", _read_backwash),
        ComponentSpec("turbidity", Stage.TURBIDITY, aux(Stage.COAG, 1), "sampler", _read_turbidity),
        ComponentSpec("flow", Stage.INTAKE, aux(Stage.INTAKE, 1), "calibration", _read_flow),
        ComponentSpec(
            "clearwell",
            Stage.CLEARWELL,
            stage_order[Stage.CLEARWELL],
            "well",
            _read_clearwell,
        ),
        ComponentSpec("quota", Stage.QUOTA, stage_order[Stage.QUOTA], "accumulator", _read_quota),
        ComponentSpec("audit", Stage.AUDIT, stage_order[Stage.AUDIT], "auditor", _read_audit),
        ComponentSpec("ph", Stage.COAG, aux(Stage.COAG, 2), "stabilizer", _read_ph),
        ComponentSpec(
            "schedule", Stage.BACKWASH, aux(Stage.FILTER, 2), "scheduler", _read_schedule
        ),
        ComponentSpec("trend", Stage.INTAKE, aux(Stage.INTAKE, 2), "trend", _read_trend),
        ComponentSpec("inventory", Stage.QUOTA, aux(Stage.QUOTA, 1), "inventory", _read_inventory),
    )


def _observe(spec: ComponentSpec, rt: Runtime) -> ComponentObservation:
    if spec.attribute is not None and getattr(rt, spec.attribute, None) is None:
        return _broken(spec, MISSING, RuntimeError("component is not configured"))
    try:
        state_value, description = spec.reader(rt)
        state = state_value
    except Exception as exc:  # noqa: BLE001 - one component is a partial failure
        return _broken(spec, FAILED, exc)
    return ComponentObservation(
        name=spec.name,
        stage=spec.stage.value,
        order=spec.order,
        status=OK,
        state=state,
        description=description,
    )


def _broken(spec: ComponentSpec, status: str, exc: Exception) -> ComponentObservation:
    return ComponentObservation(
        name=spec.name,
        stage=spec.stage.value,
        order=spec.order,
        status=status,
        state={},
        description=f"component {status} - {exc}",
        error=str(exc),
    )


def _state(item: ComponentObservation) -> object:
    return item.state if item.status == OK else {}


def _number(item: ComponentObservation, name: str) -> float | None:
    if item.status != OK:
        return None
    value = item.state.get(name)
    return float(value) if isinstance(value, (int, float)) else None


def telemetry_from_observations(
    observations: dict[str, ComponentObservation]
) -> dict[str, object]:
    """Derive flat counters from the same frozen component observations."""

    store = observations["store"]
    filter_bank = observations["filter"]
    backwash = observations["backwash"]
    audit = observations["audit"]
    quota = observations["quota"]
    clearwell = observations["clearwell"]
    coag = observations["coag"]
    chlor = observations["chlor"]
    turbidity = observations["turbidity"]
    ph = observations["ph"]
    schedule = observations["schedule"]
    trend = observations["trend"]
    inventory = observations["inventory"]

    filter_state = _state(filter_bank)
    beds = filter_state.get("beds", [])
    backwash_state = _state(backwash)
    commands = backwash_state.get("commands", [])
    drains = backwash_state.get("drains", [])
    schedule_state = _state(schedule)
    due = schedule_state.get("due", [])
    needs_reorder = _state(inventory).get("needs_reorder", [])

    return {
        "store_keys": _number(store, "size"),
        "filter_beds": _number(filter_bank, "count"),
        "filter_active": (
            sum(1 for bed in beds if isinstance(bed, dict) and not bed.get("closed"))
            if filter_bank.status == OK and isinstance(beds, list)
            else None
        ),
        "filter_load": (
            sum(
                float(bed["load"])
                for bed in beds
                if isinstance(bed, dict) and isinstance(bed.get("load"), (int, float))
            )
            if filter_bank.status == OK and isinstance(beds, list)
            else None
        ),
        "backwash_queue": len(commands) if isinstance(commands, list) else None,
        "backwash_drains": len(drains) if isinstance(drains, list) else None,
        "last_drain": drains[-1] if isinstance(drains, list) and drains else "",
        "audit_entries": _number(audit, "count"),
        "quota_value": _number(quota, "value"),
        "clearwell_level": _number(clearwell, "level"),
        "coag_ratio": _number(coag, "ratio"),
        "chlor_target": _number(chlor, "target"),
        "turbidity_verdict": _number(turbidity, "last_verdict"),
        "ph_value": _number(ph, "value"),
        "ph_stable": _state(ph).get("stable") if ph.status == OK else None,
        "schedule_due": len(due) if isinstance(due, list) else None,
        "trend_samples": _number(trend, "samples"),
        "inventory_balance": _number(inventory, "balance"),
        "inventory_alerts": len(needs_reorder) if isinstance(needs_reorder, list) else None,
    }


def collect_observations(rt: Runtime) -> dict[str, ComponentObservation]:
    """Read every component once, in the fixed treatment-line order."""

    return {spec.name: _observe(spec, rt) for spec in component_specs()}


def build_summary(
    *,
    observations: dict[str, ComponentObservation],
    telemetry: dict[str, object],
    summary_id: str,
    collected_at: str,
    fingerprint: str,
    idempotency_key: str = "",
    replayed: bool = False,
    audit: dict[str, object] | None = None,
) -> PlantSummary:
    """Assemble the frozen summary shared by every output projection."""

    status = OK
    if any(item.status != OK for item in observations.values()):
        # Missing components are marked and collection continues by policy.
        status = DEGRADED
    return PlantSummary(
        summary_id=summary_id,
        collected_at=collected_at,
        order="treatment_line",
        policy="mark_missing_continue",
        status=status,
        components=observations,
        telemetry=telemetry,
        fingerprint=fingerprint,
        idempotency_key=idempotency_key,
        replayed=replayed,
        audit=audit or {},
    )


def snapshot_from_summary(summary: PlantSummary) -> dict[str, object]:
    """Project the frozen summary into the historical snapshot shape."""

    return {
        name: observation.state if observation.status == OK else _missing_state(observation)
        for name, observation in summary.components.items()
    }


def describe_from_summary(summary: PlantSummary) -> dict[str, str]:
    """Project the same frozen observations into one description per component."""

    return {name: item.description for name, item in summary.components.items()}


def _missing_state(observation: ComponentObservation) -> dict[str, object]:
    return {"status": observation.status, "error": observation.error}


def _format_value(value: object, precision: int | None = None) -> str:
    if value is None:
        return "MISSING"
    if precision is not None and isinstance(value, (int, float)):
        return f"{float(value):.{precision}f}"
    return str(value)


def _text_telemetry(summary: PlantSummary) -> list[str]:
    telemetry = summary.telemetry
    filter_state = summary.components["filter"].state
    backwash_state = summary.components["backwash"].state

    def value(name: str) -> object:
        return telemetry.get(name)

    rows = [
        "audit kinds={}".format(
            []
            if summary.components["audit"].status != OK
            else _audit_kinds(summary.components["audit"].state.get("entries", []))
        ),
        (
            f"filter beds={_format_value(value('filter_beds'))} "
            f"active={_format_value(value('filter_active'))} "
            f"total_load={_format_value(value('filter_load'), 4)}"
        ),
        "filter bed_ids={}".format(
            "MISSING" if summary.components["filter"].status != OK else [
                bed.get("id", "") for bed in filter_state.get("beds", [])
            ]
        ),
        (
            f"backwash queue={_format_value(value('backwash_queue'))} "
            f"drains={_format_value(value('backwash_drains'))}"
        ),
        "backwash commands={}".format(
            "MISSING"
            if summary.components["backwash"].status != OK
            else backwash_state.get("commands", [])
        ),
        (
            f"quota value={_format_value(value('quota_value'), 4)} "
            f"clearwell level={_format_value(value('clearwell_level'), 4)}"
        ),
        (
            f"coag ratio={_format_value(value('coag_ratio'), 4)} "
            f"chlorine target={_format_value(value('chlor_target'), 4)}"
        ),
        (
            f"ph value={_format_value(value('ph_value'), 4)} "
            f"stable={_format_value(value('ph_stable'))} "
            f"scheduled={_format_value(value('schedule_due'))}"
        ),
        f"intake trend samples={_format_value(value('trend_samples'))}",
        (
            f"inventory balance={_format_value(value('inventory_balance'), 4)} "
            f"alerts={_format_value(value('inventory_alerts'))}"
        ),
    ]
    return rows


def _audit_kinds(entries: object) -> list[str]:
    kinds: list[str] = []
    if not isinstance(entries, list):
        return kinds
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if isinstance(kind, str) and kind not in kinds:
            kinds.append(kind)
    return kinds


def text_report_from_summary(summary: PlantSummary) -> str:
    """Render the operator report solely from the frozen summary."""

    descriptions = describe_from_summary(summary)
    line = summary.components["pipeline"].state
    pipeline_steps = line.get("steps", []) if isinstance(line, dict) else line
    stages = pipeline_steps if isinstance(pipeline_steps, list) else []
    stage_names = {
        item.get("stage") for item in stages if isinstance(item, dict)
    }
    rows = [
        "waterplant control report",
        f"summary_id={summary.summary_id} collected_at={summary.collected_at} "
        f"status={summary.status} order={summary.order} policy={summary.policy}",
        f"pipeline: {descriptions['pipeline']}",
        f"pipeline stages={len(stages)} contains_filter={Stage.FILTER.value in stage_names}",
        f"store: {descriptions['store']}",
        f"intake: {descriptions['intake']}",
        f"coagulant: {descriptions['coag']}",
        f"chlorine: {descriptions['chlor']}",
        f"filter: {descriptions['filter']}",
        f"backwash: {descriptions['backwash']}",
        f"turbidity: {descriptions['turbidity']}",
        f"flow: {descriptions['flow']}",
        f"clearwell: {descriptions['clearwell']}",
        f"quota: {descriptions['quota']}",
        f"audit: {descriptions['audit']}",
        *_text_telemetry(summary),
    ]
    if summary.missing:
        rows.append(f"missing={summary.missing}")
    if summary.failed:
        rows.append(f"failed={summary.failed}")
    audit_id = summary.audit.get("id", "")
    if audit_id:
        rows.append(f"audit_record id={audit_id} kind=plant_summary")
    return "\n".join(rows) + "\n"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_hash(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return uuid.uuid5(uuid.NAMESPACE_URL, f"plant-summary:{encoded}").hex


def _summary_key(summary_id: str) -> str:
    return f"{SUMMARY_KEY_PREFIX}{summary_id}"


def _load_json(store: Store, key: str) -> tuple[object, bool]:
    raw, present = store.get(key)
    if not present:
        return None, False
    try:
        return json.loads(raw), True
    except ValueError:
        return None, False


def _save_json(store: Store, key: str, payload: object) -> None:
    store.put(key, json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _plant_audit_entries(store: Store) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for raw_item in load_commands(store, SUMMARY_AUDIT_KEY):
        try:
            item = json.loads(raw_item)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries


def _find_replay(
    store: Store,
    idempotency_key: str,
    fingerprint: str,
) -> tuple[dict[str, object] | None, dict[str, object] | None, str]:
    for entry in _plant_audit_entries(store):
        if entry.get("idempotency_key") != idempotency_key:
            continue
        if entry.get("fingerprint") != fingerprint:
            return None, None, "idempotency key was already used for different plant state"
        summary_id = str(entry.get("summary_id", ""))
        payload, present = _load_json(store, _summary_key(summary_id))
        if present and isinstance(payload, dict):
            return payload, None, ""
        # The audit write is durable but the payload write was interrupted.
        return None, entry, ""
    return None, None, ""


def summary_from_payload(payload: dict[str, object]) -> PlantSummary:
    """Reconstruct a persisted summary without touching component state."""

    component_payload = payload.get("components", {})
    components: dict[str, ComponentObservation] = {}
    if isinstance(component_payload, dict):
        for name, item in component_payload.items():
            if not isinstance(item, dict):
                continue
            raw_state = item.get("state", {})
            state = raw_state if isinstance(raw_state, (dict, list)) else {}
            components[str(name)] = ComponentObservation(
                name=str(item.get("name", name)),
                stage=str(item.get("stage", "")),
                order=int(item.get("order", 0)),
                status=str(item.get("status", FAILED)),
                state=state,  # type: ignore[arg-type]
                description=str(item.get("description", "")),
                error=str(item.get("error", "")),
            )
    audit = payload.get("audit", {})
    telemetry_payload = payload.get("telemetry", {})
    telemetry = dict(telemetry_payload) if isinstance(telemetry_payload, dict) else {}
    return PlantSummary(
        summary_id=str(payload.get("summary_id", "")),
        collected_at=str(payload.get("collected_at", "")),
        order=str(payload.get("order", "treatment_line")),
        policy=str(payload.get("policy", "mark_missing_continue")),
        status=str(payload.get("status", DEGRADED)),
        components=components,
        telemetry=telemetry,
        fingerprint=str(payload.get("fingerprint", "")),
        idempotency_key=str(payload.get("idempotency_key", "")),
        replayed=bool(payload.get("replayed", False)),
        audit=dict(audit) if isinstance(audit, dict) else {},
    )


def _persist_summary(store: Store, summary: PlantSummary) -> None:
    payload = summary.as_dict()
    _save_json(store, _summary_key(summary.summary_id), payload)
    _save_json(store, LATEST_SUMMARY_KEY, summary.summary_id)


def _record_plant_audit(
    store: Store,
    *,
    entry_id: str,
    summary_id: str,
    collected_at: str,
    fingerprint: str,
    idempotency_key: str,
    status: str,
    missing: list[str],
    failed: list[str],
) -> dict[str, object]:
    entry = {
        "id": entry_id,
        "time": collected_at,
        "kind": "plant_summary",
        "summary_id": summary_id,
        "fingerprint": fingerprint,
        "idempotency_key": idempotency_key,
        "status": status,
        "missing": missing,
        "failed": failed,
    }
    append_command(
        store, SUMMARY_AUDIT_KEY, json.dumps(entry, ensure_ascii=False, sort_keys=True)
    )
    return entry


class PlantSummaryService:
    """Creates, audits and replays idempotent plant summaries."""

    def __init__(self, rt: Runtime) -> None:
        self._rt = rt
        self._lock = threading.RLock()
        self._latest: PlantSummary | None = None
        self._current: PlantSummary | None = None

    def current(self) -> PlantSummary:
        """Return a fresh, unaudited summary for legacy read endpoints."""

        with self._rt.guard:
            if self._current is None:
                self._current = self._build_summary(audit_result={})
            return self._current

    def latest(self) -> PlantSummary | None:
        """Return the most recent audited summary, loading it after a restart."""

        with self._rt.guard:
            if self._latest is not None:
                return self._latest
            summary_id, present = self._rt.store.get(LATEST_SUMMARY_KEY)
            if not present:
                return None
            payload, loaded = _load_json(self._rt.store, _summary_key(summary_id))
            if not loaded or not isinstance(payload, dict):
                return None
            summary = summary_from_payload(payload)
            self._latest = summary
            self._current = summary
            return summary

    def collect(self, idempotency_key: str = "") -> tuple[PlantSummary, bool, str]:
        """Collect and audit a summary.

        Returns the summary, whether it was replayed, and a conflict message.
        The same idempotency key plus unchanged state is replayed unchanged.
        """

        with self._rt.guard:
            return self._collect_idempotent(idempotency_key)

    def invalidate(self) -> None:
        """Drop only the in-memory convenience copy; the audit trail remains."""

        with self._lock:
            self._latest = None
            self._current = None

    def _collect_idempotent(self, idempotency_key: str) -> tuple[PlantSummary, bool, str]:
        observations = collect_observations(self._rt)
        telemetry = telemetry_from_observations(observations)
        fingerprint = _observations_fingerprint(observations)

        if idempotency_key:
            replay_payload, recovery_audit, conflict = _find_replay(
                self._rt.store, idempotency_key, fingerprint
            )
            if conflict:
                summary = build_summary(
                    observations=observations,
                    telemetry=telemetry,
                    summary_id=uuid.uuid4().hex,
                    collected_at=_now(),
                    fingerprint=fingerprint,
                    idempotency_key=idempotency_key,
                )
                return summary, False, conflict
            if replay_payload is not None:
                summary = replace(summary_from_payload(replay_payload), replayed=True)
                self._latest = summary
                self._current = summary
                return summary, True, ""
            if recovery_audit is not None:
                summary = build_summary(
                    observations=observations,
                    telemetry=telemetry,
                    summary_id=str(recovery_audit.get("summary_id", "")),
                    collected_at=str(recovery_audit.get("time", _now())),
                    fingerprint=fingerprint,
                    idempotency_key=idempotency_key,
                    audit=recovery_audit,
                )
                _persist_summary(self._rt.store, summary)
                self._latest = summary
                self._current = summary
                return replace(summary, replayed=True), True, ""

        summary_id = uuid.uuid4().hex
        audit_id = uuid.uuid4().hex
        collected_at = _now()
        provisional = build_summary(
            observations=observations,
            telemetry=telemetry,
            summary_id=summary_id,
            collected_at=collected_at,
            fingerprint=fingerprint,
            idempotency_key=idempotency_key,
        )
        audit_result = _record_plant_audit(
            self._rt.store,
            entry_id=audit_id,
            summary_id=summary_id,
            collected_at=collected_at,
            fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            status=provisional.status,
            missing=provisional.missing,
            failed=provisional.failed,
        )
        summary = build_summary(
            observations=observations,
            telemetry=telemetry,
            summary_id=summary_id,
            collected_at=collected_at,
            fingerprint=fingerprint,
            idempotency_key=idempotency_key,
            audit=audit_result,
        )
        _persist_summary(self._rt.store, summary)
        self._latest = summary
        self._current = summary
        return summary, False, ""

    def _build_summary(
        self,
        *,
        audit_result: dict[str, object],
        summary_id: str | None = None,
        collected_at: str | None = None,
        idempotency_key: str = "",
    ) -> PlantSummary:
        observations = collect_observations(self._rt)
        telemetry = telemetry_from_observations(observations)
        return build_summary(
            observations=observations,
            telemetry=telemetry,
            summary_id=summary_id or uuid.uuid4().hex,
            collected_at=collected_at or _now(),
            fingerprint=_observations_fingerprint(observations),
            idempotency_key=idempotency_key,
            audit=audit_result,
        )


def _observations_fingerprint(observations: dict[str, ComponentObservation]) -> str:
    return _json_hash(
        [
            {
                "name": name,
                "status": item.status,
                "state": item.state,
                "description": item.description,
                "error": item.error,
            }
            for name, item in observations.items()
        ]
    )
