"""Benchmark registry: loads `configs/benchmarks.yaml`.

Kept separate from the model registry for one reason that is not stylistic:
every entry here is `eval_only: true`. Most Korean benchmarks ship under CC-BY-ND
or CC-BY-NC licences, so folding them into a training mix is simultaneously a
licence violation and test-set contamination. `BenchmarkSpec` refuses to be
marked trainable, and `ffsft.data.korean` must never read this file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

_DEFAULT_REGISTRY = Path(__file__).resolve().parents[3] / "configs" / "benchmarks.yaml"
_ENV_VAR = "FFSFT_BENCHMARK_REGISTRY"


class BenchmarkSpec(BaseModel):
    """One Korean evaluation benchmark."""

    key: str
    dataset_id: str
    license: str = "unknown"

    #: Always true. Present in the schema so the constraint is explicit in YAML
    #: rather than implied, and validated so it cannot be flipped by accident.
    eval_only: bool = True

    metric: str = "accuracy"

    #: Task name in EleutherAI's lm-evaluation-harness, when one exists. Absent
    #: means the benchmark needs a bespoke runner (or a judge).
    harness_task: str | None = None

    #: Needs a separate judge LLM to score free-form generations.
    judge_required: bool = False

    description: str = ""

    @field_validator("eval_only")
    @classmethod
    def _must_be_eval_only(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "benchmarks are eval_only by construction: most Korean benchmarks are "
                "CC-BY-ND/NC, and training on them is both a licence violation and "
                "test-set contamination"
            )
        return v

    @property
    def runnable_by_harness(self) -> bool:
        return self.harness_task is not None and not self.judge_required


class SuiteSpec(BaseModel):
    """A named group of benchmarks, so 'run the standard eval' is one flag."""

    key: str
    description: str = ""
    benchmarks: list[str] = Field(default_factory=list)


class BenchmarkRegistry:
    def __init__(
        self,
        specs: list[BenchmarkSpec],
        suites: list[SuiteSpec] | None = None,
        default_suite: str | None = None,
    ) -> None:
        duplicates = {s.key for s in specs if sum(o.key == s.key for o in specs) > 1}
        if duplicates:
            raise ValueError(f"duplicate benchmark keys: {sorted(duplicates)}")
        self._specs = {s.key: s for s in specs}
        self._suites = {s.key: s for s in (suites or [])}

        for suite in self._suites.values():
            unknown = [b for b in suite.benchmarks if b not in self._specs]
            if unknown:
                raise ValueError(f"suite '{suite.key}' references unknown benchmarks: {unknown}")
        if default_suite and default_suite not in self._suites:
            raise ValueError(f"default suite '{default_suite}' is not defined")
        self._default_suite = default_suite

    @classmethod
    def from_yaml(cls, path: str | os.PathLike[str]) -> BenchmarkRegistry:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        entries = raw.get("benchmarks", [])
        if not isinstance(entries, list):
            raise ValueError(f"{path}: 'benchmarks' must be a list")
        suites = [
            SuiteSpec(key=k, **(v or {})) for k, v in (raw.get("suites") or {}).items()
        ]
        return cls(
            [BenchmarkSpec.model_validate(e) for e in entries],
            suites,
            (raw.get("defaults") or {}).get("suite"),
        )

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self):
        return iter(self._specs.values())

    def __contains__(self, key: object) -> bool:
        return key in self._specs

    @property
    def keys(self) -> list[str]:
        return list(self._specs)

    @property
    def suite_keys(self) -> list[str]:
        return list(self._suites)

    @property
    def default_suite(self) -> SuiteSpec:
        if not self._default_suite:
            raise KeyError("benchmark registry declares no defaults.suite")
        return self._suites[self._default_suite]

    def get(self, key: str) -> BenchmarkSpec:
        try:
            return self._specs[key]
        except KeyError:
            raise KeyError(
                f"unknown benchmark '{key}'. Available: {', '.join(sorted(self._specs))}"
            ) from None

    def suite(self, key: str) -> list[BenchmarkSpec]:
        """Expand a suite name into its benchmark specs."""
        try:
            suite = self._suites[key]
        except KeyError:
            raise KeyError(
                f"unknown suite '{key}'. Available: {', '.join(sorted(self._suites))}"
            ) from None
        return [self._specs[b] for b in suite.benchmarks]

    def resolve(self, names: list[str] | None) -> list[BenchmarkSpec]:
        """Accept a mix of suite names and benchmark keys; default suite if empty."""
        if not names:
            return self.suite(self.default_suite.key)
        out: list[BenchmarkSpec] = []
        seen: set[str] = set()
        for name in names:
            picked = self.suite(name) if name in self._suites else [self.get(name)]
            for spec in picked:
                if spec.key not in seen:
                    seen.add(spec.key)
                    out.append(spec)
        return out


@lru_cache(maxsize=8)
def _cached_registry(resolved: str) -> BenchmarkRegistry:
    return BenchmarkRegistry.from_yaml(resolved)


def get_benchmark_registry(path: str | os.PathLike[str] | None = None) -> BenchmarkRegistry:
    resolved = Path(path or os.environ.get(_ENV_VAR) or _DEFAULT_REGISTRY)
    if not resolved.is_file():
        raise FileNotFoundError(
            f"benchmark registry not found at {resolved}. Set {_ENV_VAR} or pass a path."
        )
    return _cached_registry(str(resolved))


def get_benchmark(key: str, path: str | os.PathLike[str] | None = None) -> BenchmarkSpec:
    return get_benchmark_registry(path).get(key)
