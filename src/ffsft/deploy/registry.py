"""Serving registry: loads `ServingSpec` entries from `configs/serving.yaml`.

Mirrors `ffsft.models.registry` deliberately -- one loading convention for the
whole asset. Resolution order is: explicit path, then ``FFSFT_SERVING_REGISTRY``,
then the shipped YAML.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

from .spec import AdapterMode, AdapterModeSpec, Engine, ServingSpec, Surface

_DEFAULT_REGISTRY = Path(__file__).resolve().parents[3] / "configs" / "serving.yaml"
_ENV_VAR = "FFSFT_SERVING_REGISTRY"


class ServingRegistry:
    """An in-memory collection of `ServingSpec`, keyed by `ServingSpec.key`."""

    def __init__(
        self,
        specs: list[ServingSpec],
        adapter_modes: list[AdapterModeSpec] | None = None,
        default_key: str | None = None,
    ) -> None:
        duplicates = {s.key for s in specs if sum(o.key == s.key for o in specs) > 1}
        if duplicates:
            raise ValueError(f"duplicate serving keys in registry: {sorted(duplicates)}")
        self._specs: dict[str, ServingSpec] = {s.key: s for s in specs}
        self._adapter_modes: dict[AdapterMode, AdapterModeSpec] = {
            m.key: m for m in (adapter_modes or [])
        }
        if default_key and default_key not in self._specs:
            raise ValueError(f"default serving pattern '{default_key}' is not defined")
        self._default_key = default_key

    # -- construction ---------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | os.PathLike[str]) -> ServingRegistry:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        entries = raw.get("patterns", [])
        if not isinstance(entries, list):
            raise ValueError(f"{path}: 'patterns' must be a list")
        modes = raw.get("adapter_modes", []) or []
        return cls(
            [ServingSpec.model_validate(e) for e in entries],
            [AdapterModeSpec.model_validate(m) for m in modes],
            (raw.get("defaults") or {}).get("pattern"),
        )

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> ServingRegistry:
        resolved = Path(path or os.environ.get(_ENV_VAR) or _DEFAULT_REGISTRY)
        if not resolved.is_file():
            raise FileNotFoundError(
                f"serving registry not found at {resolved}. "
                f"Set {_ENV_VAR} or pass an explicit path."
            )
        return cls.from_yaml(resolved)

    # -- lookup ---------------------------------------------------------

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, key: object) -> bool:
        return key in self._specs

    def __iter__(self):
        return iter(self._specs.values())

    @property
    def keys(self) -> list[str]:
        return list(self._specs)

    @property
    def default(self) -> ServingSpec:
        if not self._default_key:
            raise KeyError("serving registry declares no defaults.pattern")
        return self._specs[self._default_key]

    def get(self, key: str) -> ServingSpec:
        try:
            return self._specs[key]
        except KeyError:
            raise KeyError(
                f"unknown serving pattern '{key}'. Available: {', '.join(sorted(self._specs))}"
            ) from None

    def adapter_mode(self, key: AdapterMode | str) -> AdapterModeSpec:
        mode = AdapterMode(key)
        try:
            return self._adapter_modes[mode]
        except KeyError:
            raise KeyError(
                f"adapter mode '{mode.value}' is not documented in the registry"
            ) from None

    @property
    def adapter_modes(self) -> list[AdapterModeSpec]:
        return list(self._adapter_modes.values())

    def filter(
        self,
        *,
        surface: Surface | None = None,
        engine: Engine | None = None,
        openai_compatible: bool | None = None,
        low_priority_only: bool = False,
        load_testable: bool | None = None,
    ) -> list[ServingSpec]:
        """Shortlist serving patterns. Arguments left as None are ignored."""
        out = list(self._specs.values())
        if surface is not None:
            out = [s for s in out if s.surface is surface]
        if engine is not None:
            out = [s for s in out if s.engine is engine]
        if openai_compatible is not None:
            out = [s for s in out if s.openai_compatible is openai_compatible]
        if low_priority_only:
            out = [s for s in out if s.allows_low_priority]
        if load_testable is not None:
            out = [s for s in out if s.load_testable is load_testable]
        return sorted(out, key=lambda s: s.key)


@lru_cache(maxsize=8)
def _cached_registry(resolved: str) -> ServingRegistry:
    return ServingRegistry.from_yaml(resolved)


def get_serving_registry(path: str | os.PathLike[str] | None = None) -> ServingRegistry:
    """Process-wide cached registry accessor."""
    resolved = Path(path or os.environ.get(_ENV_VAR) or _DEFAULT_REGISTRY)
    if not resolved.is_file():
        raise FileNotFoundError(
            f"serving registry not found at {resolved}. Set {_ENV_VAR} or pass an explicit path."
        )
    return _cached_registry(str(resolved))


def get_serving(key: str, path: str | os.PathLike[str] | None = None) -> ServingSpec:
    return get_serving_registry(path).get(key)
