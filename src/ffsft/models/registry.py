"""Model registry: loads `ModelSpec` entries from YAML so models are swappable
without touching code.

Resolution order for the registry file:
1. explicit `path` argument
2. ``FFSFT_MODEL_REGISTRY`` environment variable
3. ``configs/models.yaml`` shipped with the repo

Users add a model by appending a YAML entry, then referring to it by `key`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

from .spec import KoreanTier, ModelSpec, Provider, TuningMethod

_DEFAULT_REGISTRY = Path(__file__).resolve().parents[3] / "configs" / "models.yaml"
_ENV_VAR = "FFSFT_MODEL_REGISTRY"


class ModelRegistry:
    """An in-memory collection of `ModelSpec`, keyed by `ModelSpec.key`."""

    def __init__(self, specs: list[ModelSpec]) -> None:
        duplicates = {s.key for s in specs if sum(o.key == s.key for o in specs) > 1}
        if duplicates:
            raise ValueError(f"duplicate model keys in registry: {sorted(duplicates)}")
        self._specs: dict[str, ModelSpec] = {s.key: s for s in specs}

    # -- construction ---------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | os.PathLike[str]) -> ModelRegistry:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        entries = raw.get("models", [])
        if not isinstance(entries, list):
            raise ValueError(f"{path}: 'models' must be a list")
        return cls([ModelSpec.model_validate(e) for e in entries])

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> ModelRegistry:
        resolved = Path(path or os.environ.get(_ENV_VAR) or _DEFAULT_REGISTRY)
        if not resolved.is_file():
            raise FileNotFoundError(
                f"model registry not found at {resolved}. "
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

    def get(self, key: str) -> ModelSpec:
        try:
            return self._specs[key]
        except KeyError:
            raise KeyError(
                f"unknown model key '{key}'. Available: {', '.join(sorted(self._specs))}"
            ) from None

    def by_hf_id(self, hf_id: str) -> ModelSpec | None:
        """Spec whose `hf_id` matches `hf_id`, or None if the registry has none.

        Deploying with `--hf-model` names a Hub repo, not a registry key, so the
        serving flags this registry *measured* for that exact checkpoint --
        `mamba_cache_mode`, `reasoning_parser` -- were silently dropped for the
        one model they were measured on. Returns None rather than raising:
        pointing the server at an arbitrary Hub id is a legitimate deployment.
        """
        if not hf_id:
            return None
        wanted = hf_id.strip().lower()
        for spec in self._specs.values():
            if spec.hf_id and spec.hf_id.strip().lower() == wanted:
                return spec
        return None

    def filter(
        self,
        *,
        provider: Provider | None = None,
        method: TuningMethod | None = None,
        korean_tier: KoreanTier | None = None,
        commercial_use: bool | None = None,
        open_weights: bool | None = None,
        max_params_b: float | None = None,
    ) -> list[ModelSpec]:
        """Shortlist models. Every argument left as None is ignored."""
        out = list(self._specs.values())
        if provider is not None:
            out = [s for s in out if s.provider is provider]
        if method is not None:
            out = [s for s in out if s.supports_method(method)]
        if korean_tier is not None:
            out = [s for s in out if s.korean_tier is korean_tier]
        if commercial_use is not None:
            out = [s for s in out if s.commercial_use is commercial_use]
        if open_weights is not None:
            out = [s for s in out if s.is_open_weights is open_weights]
        if max_params_b is not None:
            out = [s for s in out if s.params_b is not None and s.params_b <= max_params_b]
        return sorted(out, key=lambda s: (s.params_b or 0.0, s.key))


@lru_cache(maxsize=8)
def _cached_registry(resolved: str) -> ModelRegistry:
    return ModelRegistry.from_yaml(resolved)


def get_registry(path: str | os.PathLike[str] | None = None) -> ModelRegistry:
    """Process-wide cached registry accessor."""
    resolved = Path(path or os.environ.get(_ENV_VAR) or _DEFAULT_REGISTRY)
    if not resolved.is_file():
        raise FileNotFoundError(
            f"model registry not found at {resolved}. Set {_ENV_VAR} or pass an explicit path."
        )
    return _cached_registry(str(resolved))


def get_model(key: str, path: str | os.PathLike[str] | None = None) -> ModelSpec:
    return get_registry(path).get(key)
