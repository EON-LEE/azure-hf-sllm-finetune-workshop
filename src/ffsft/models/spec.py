"""Model abstraction: every model in this asset is described by a `ModelSpec`.

The whole point of this module is swappability. Training / evaluation / deployment
code never hardcodes a model id. It asks the registry for a `ModelSpec` by key and
reads capabilities off it, so switching from a Qwen model to Phi, Llama, EXAONE or
an Azure OpenAI model is a config change, not a code change.
"""

from __future__ import annotations

import sys
from enum import Enum

from pydantic import BaseModel, Field, model_validator

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    # Azure ML's curated deep-learning base images (aifx/acpt/*) are all built on
    # Python 3.10 -- there is no 3.11+ ACPT tag -- and we would rather run on the
    # image Microsoft validates for GPU training than drop it to keep one stdlib
    # import. `StrEnum` is only sugar over the classic mixin, but the mixin alone
    # is not equivalent: on 3.10 `str(Provider.HF)` yields "Provider.HF" while
    # `f"{Provider.HF}"` yields "hf". Pinning both dunders to `str`'s makes the
    # two behave identically here and on 3.11+, so YAML round-trips and log lines
    # do not silently change meaning with the interpreter.
    class StrEnum(str, Enum):  # type: ignore[no-redef]
        """Backport of :class:`enum.StrEnum` for Python 3.10."""

        __str__ = str.__str__
        __format__ = str.__format__


class Provider(StrEnum):
    """Where the weights come from and who runs the training loop."""

    #: Open weights pulled from Hugging Face, trained by our own script.
    #: Runs on any GPU: local workstation, Azure VM, or an Azure ML command job.
    HF = "hf"

    #: Foundry / Azure ML *managed compute*: our own training container and script,
    #: scheduled by Azure ML onto a GPU SKU. Still Hugging Face weights underneath.
    FOUNDRY_MANAGED = "foundry_managed"

    #: Foundry *serverless* fine-tuning (Models-as-a-Service). Black-box training
    #: loop owned by Microsoft; we only submit JSONL + hyperparameters.
    FOUNDRY_SERVERLESS = "foundry_serverless"

    #: Azure OpenAI fine-tuning (gpt-4.1-mini etc). SFT / DPO / RFT, also black box.
    AZURE_OPENAI = "azure_openai"

    #: Inference only. No customer-facing fine-tuning path exists.
    #: Used to document models like the Microsoft MAI family.
    INFERENCE_ONLY = "inference_only"


class TuningMethod(StrEnum):
    LORA = "lora"
    QLORA = "qlora"
    FULL = "full"
    SERVERLESS_SFT = "serverless_sft"
    SERVERLESS_DPO = "serverless_dpo"
    SERVERLESS_RFT = "serverless_rft"


class KoreanTier(StrEnum):
    """Qualitative Korean-language capability, used for candidate shortlisting."""

    NATIVE = "native"  # Korean-first model, trained by a Korean lab
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    UNKNOWN = "unknown"


class VramProfile(BaseModel):
    """Rough peak VRAM in GB per tuning method, at the asset's default seq length."""

    lora: int | None = None
    qlora: int | None = None
    full: int | None = None


class ModelSpec(BaseModel):
    """A single swappable model target."""

    key: str = Field(description="Short stable alias used everywhere, e.g. 'qwen3-4b'.")
    display_name: str

    provider: Provider
    supports: list[TuningMethod] = Field(default_factory=list)

    #: Hugging Face repo id. Required for HF / FOUNDRY_MANAGED providers.
    hf_id: str | None = None
    #: Name of the model as it appears in the Foundry catalog / as a deployment target.
    foundry_model: str | None = None

    params_b: float | None = Field(default=None, description="Total params in billions.")
    active_params_b: float | None = Field(
        default=None, description="Active params for MoE models, in billions."
    )
    context_length: int | None = None

    license: str = "unknown"
    commercial_use: bool | None = None

    korean_tier: KoreanTier = KoreanTier.UNKNOWN
    korean_notes: str = ""

    vram_gb: VramProfile = Field(default_factory=VramProfile)
    recommended_sku: str | None = Field(
        default=None, description="Azure ML GPU SKU that comfortably fits LoRA SFT."
    )

    #: Extra kwargs forwarded to `tokenizer.apply_chat_template`,
    #: e.g. {"enable_thinking": false} for Qwen3 hybrid-reasoning models.
    chat_template_kwargs: dict[str, object] = Field(default_factory=dict)

    #: Modules PEFT wraps with LoRA adapters. Empty means "fall back to PEFT's
    #: per-architecture default", which is only safe for plain transformer stacks.
    #: It is actively wrong for hybrid attention models: on Qwen3.5/3.6/3.8 the
    #: default {q,k,v,o}_proj set exists only on the 1-in-4 full-attention layers,
    #: so 48 of 64 layers would silently receive no adapter. Always set this
    #: explicitly for hybrid models. Verified with scripts/probe_architecture.py.
    lora_target_modules: list[str] = Field(default_factory=list)

    notes: str = ""
    source_url: str | None = None

    @model_validator(mode="after")
    def _check_identifiers(self) -> ModelSpec:
        needs_hf = {Provider.HF, Provider.FOUNDRY_MANAGED}
        if self.provider in needs_hf and not self.hf_id:
            raise ValueError(f"model '{self.key}': provider={self.provider.value} requires hf_id")

        needs_foundry = {
            Provider.FOUNDRY_SERVERLESS,
            Provider.AZURE_OPENAI,
            Provider.INFERENCE_ONLY,
        }
        if self.provider in needs_foundry and not self.foundry_model:
            raise ValueError(
                f"model '{self.key}': provider={self.provider.value} requires foundry_model"
            )

        if self.provider is Provider.INFERENCE_ONLY and self.supports:
            raise ValueError(
                f"model '{self.key}': provider=inference_only cannot declare tuning methods"
            )
        return self

    @property
    def is_open_weights(self) -> bool:
        return self.hf_id is not None

    @property
    def trainable(self) -> bool:
        return bool(self.supports)

    @property
    def recommended_method(self) -> TuningMethod | None:
        """The default recipe for this model.

        By convention the first entry of `supports` in the YAML registry is the
        recommended one, so a 27B model can list `[qlora, lora, full]` and the
        training entry point picks QLoRA unless the user overrides it.
        """
        return self.supports[0] if self.supports else None

    def supports_method(self, method: TuningMethod) -> bool:
        return method in self.supports

    def require_method(self, method: TuningMethod) -> None:
        if not self.supports_method(method):
            allowed = ", ".join(m.value for m in self.supports) or "none"
            raise ValueError(
                f"model '{self.key}' does not support tuning method '{method.value}'. "
                f"Supported: {allowed}."
            )
