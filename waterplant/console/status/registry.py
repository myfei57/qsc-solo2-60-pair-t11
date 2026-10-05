"""Registry of every plant component covered by a status summary.

The order is fixed by the output convention, which follows the treatment
line: intake -> coagulant -> chlorine -> filter -> backwash -> clear well ->
quota -> audit, with each stage's auxiliary components (turbidity, meter
calibration, ph, schedule, trend, inventory) grouped next to the stage they
serve. Every summary pass walks the components in exactly this order, so the
snapshot, the descriptions and the text report always list components the
same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from waterplant.ns import Stage, treatment_line
from waterplant.store import describe_store, store_state

from ..runtime import Runtime

T = TypeVar("T")


@dataclass(frozen=True)
class ComponentSpec(Generic[T]):
    """How one component is read once and rendered from that single read."""

    name: str
    read: Callable[[Runtime], T]
    render: Callable[[Runtime, T], str]
    payload: Callable[[T], object] = lambda state: state.as_dict()


class _PipelineView:
    """Read only view of the static treatment line for one summary pass."""

    def __init__(self) -> None:
        line = treatment_line()
        self.name = line.name
        self.steps = line.steps()

    def as_dict(self) -> list[dict[str, str]]:
        return [step.as_dict() for step in self.steps]

    @property
    def stage_count(self) -> int:
        return len(self.steps)

    def contains(self, stage: Stage) -> bool:
        return any(step.stage == stage for step in self.steps)

    def describe(self) -> str:
        return treatment_line().describe()


def _registry() -> tuple[ComponentSpec[object], ...]:
    return (
        ComponentSpec(
            name="pipeline",
            read=lambda rt: _PipelineView(),
            render=lambda rt, view: view.describe(),
        ),
        ComponentSpec(
            name="store",
            read=lambda rt: store_state(rt.store),
            render=lambda rt, state: describe_store(rt.store, state),
        ),
        ComponentSpec(
            name="intake",
            read=lambda rt: rt.flow_repository.state(),
            render=lambda rt, state: rt.flow_repository.describe(state),
        ),
        ComponentSpec(
            name="coag",
            read=lambda rt: rt.coag_doser.state(),
            render=lambda rt, state: rt.coag_doser.describe(state),
        ),
        ComponentSpec(
            name="chlor",
            read=lambda rt: rt.chlor_doser.state(),
            render=lambda rt, state: rt.chlor_doser.describe(state),
        ),
        ComponentSpec(
            name="filter",
            read=lambda rt: rt.bank.state(),
            render=lambda rt, state: rt.bank.describe(state),
        ),
        ComponentSpec(
            name="backwash",
            read=lambda rt: rt.backwash.state(),
            render=lambda rt, state: rt.backwash.describe(state),
        ),
        ComponentSpec(
            name="turbidity",
            read=lambda rt: rt.sampler.state(),
            render=lambda rt, state: rt.sampler.describe(state),
        ),
        ComponentSpec(
            name="flow",
            read=lambda rt: rt.calibration.state(),
            render=lambda rt, state: rt.calibration.describe(state),
        ),
        ComponentSpec(
            name="clearwell",
            read=lambda rt: rt.well.state(),
            render=lambda rt, state: rt.well.describe(state),
        ),
        ComponentSpec(
            name="quota",
            read=lambda rt: rt.accumulator.state(),
            render=lambda rt, state: rt.accumulator.describe(state),
        ),
        ComponentSpec(
            name="audit",
            read=lambda rt: rt.auditor.state(),
            render=lambda rt, state: rt.auditor.describe(state),
        ),
        ComponentSpec(
            name="ph",
            read=lambda rt: rt.stabilizer.state(),
            render=lambda rt, state: rt.stabilizer.describe(state),
        ),
        ComponentSpec(
            name="schedule",
            read=lambda rt: rt.scheduler.state(rt.bank),
            render=lambda rt, state: rt.scheduler.describe(rt.bank, state),
        ),
        ComponentSpec(
            name="trend",
            read=lambda rt: rt.trend.stats(),
            render=lambda rt, state: rt.trend.describe(state),
        ),
        ComponentSpec(
            name="inventory",
            read=lambda rt: rt.inventory.state(),
            render=lambda rt, state: rt.inventory.describe(state),
        ),
    )


COMPONENT_ORDER: tuple[str, ...] = tuple(spec.name for spec in _registry())


def component_specs() -> tuple[ComponentSpec[object], ...]:
    """Return a fresh registry tuple; readers may be overridden in tests."""

    return _registry()
