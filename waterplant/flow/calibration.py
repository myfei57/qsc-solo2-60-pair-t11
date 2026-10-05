"""Persisted flow calibration factor."""

from __future__ import annotations

from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .report import CalibState

CALIBRATION_KEY = "flow:calibration"
DEFAULT_FACTOR = 1.0


class Calibration:
    """Tracks the multiplier applied to raw flow readings."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def current(self) -> float:
        value, present = load_float(self._store, CALIBRATION_KEY)
        return value if present else DEFAULT_FACTOR

    def replace(self, factor: float) -> None:
        save_float(self._store, CALIBRATION_KEY, factor)

    def state(self) -> CalibState:
        return CalibState(factor=self.current())

    def describe(self, state: CalibState | None = None) -> str:
        factor = self.current() if state is None else state.factor
        return f"flow calibration factor={factor:.4f}"
