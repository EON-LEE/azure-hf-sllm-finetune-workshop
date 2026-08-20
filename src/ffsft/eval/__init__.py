"""Korean benchmark evaluation.

Runs the suites declared in `configs/benchmarks.yaml` (KMMLU, HAE-RAE,
IFEval-Ko, LogicKor, ...). Every benchmark is eval-only by construction, so
this package never feeds data back into training.
"""

from .registry import (
    BenchmarkRegistry,
    BenchmarkSpec,
    SuiteSpec,
    get_benchmark,
    get_benchmark_registry,
)

__all__ = [
    "BenchmarkRegistry",
    "BenchmarkSpec",
    "SuiteSpec",
    "get_benchmark",
    "get_benchmark_registry",
]
