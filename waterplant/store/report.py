"""Read only projections over the store."""

from __future__ import annotations

from dataclasses import dataclass

from .store import Store


@dataclass(frozen=True)
class StoreState:
    """Sorted keys plus the total number of entries."""

    keys: list[str]
    size: int

    def as_dict(self) -> dict[str, object]:
        return {"keys": self.keys, "size": self.size}


def store_state(store: Store) -> StoreState:
    keys = store.keys()
    return StoreState(keys=keys, size=len(keys))


def describe_store(store: Store, state: StoreState | None = None) -> str:
    count = store.count() if state is None else state.size
    return f"store keys={count}"


def export_state(store: Store) -> dict[str, str]:
    """Return a plain copy of every entry for diagnostics."""

    return {key: store.get(key)[0] for key in store.keys()}


def has_key(store: Store, key: str) -> bool:
    return store.get(key)[1]
