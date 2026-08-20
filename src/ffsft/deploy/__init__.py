"""Deployment of a tuned model.

Merges LoRA adapters and hosts them through one of the swappable serving
patterns in `configs/serving.yaml`: an Azure ML managed online endpoint, a batch
endpoint on a LowPriority cluster, AKS, or a local vLLM server.
"""

from .registry import ServingRegistry, get_serving, get_serving_registry
from .spec import AdapterMode, AdapterModeSpec, Engine, ServingSpec, Surface

__all__ = [
    "AdapterMode",
    "AdapterModeSpec",
    "Engine",
    "ServingRegistry",
    "ServingSpec",
    "Surface",
    "get_serving",
    "get_serving_registry",
]
