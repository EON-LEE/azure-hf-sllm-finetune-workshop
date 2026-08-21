"""Fail the image build, not a GPU job, if the library upgrade broke the base.

Run as a build step after the pip upgrade. Two things are checked, and both were
learned the expensive way.

The torch assertion is the reason ACPT is worth using at all: its value is a
torch/CUDA/NCCL combination that Microsoft validated against the AmlCompute host
drivers, and a transitive requirement quietly pulling a different torch build
from PyPI throws that away. Without the check the swap is invisible until a CUDA
error on the node, an hour and a cluster allocation later.

The imports are deliberately the *real* entry points rather than bare module
imports. `import transformers` alone is not enough: the first job to reach the
GPU died on `from numpy import Inf` raised deep inside scipy, pulled in by
transformers' object-detection losses, because ACPT pairs numpy 2.2 with a scipy
that predates numpy 2. Importing what training actually imports moves that class
of failure from a 20-minute GPU round trip to a build step.
"""

from __future__ import annotations

import inspect
import sys

import accelerate
import bitsandbytes
import datasets
import peft
import torch
import transformers
import trl

# The evaluator is chained onto the training job rather than submitted
# separately, so a missing harness would surface only after the GPU had already
# been paid for and the model trained. `ffsft.eval.run` imports HFLM lazily
# inside a function, which is good for the unit tests and useless as a build
# check -- so import the same symbol here.
from lm_eval.models.huggingface import HFLM
from peft import LoraConfig, prepare_model_for_kbit_training  # noqa: F401
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig  # noqa: F401
from trl import SFTConfig, SFTTrainer  # noqa: F401

with open("/tmp/base-constraints.txt") as fh:
    expected = fh.read().strip().split("==", 1)[1]

if torch.__version__ != expected:
    raise SystemExit(
        f"torch was replaced during the upgrade: {torch.__version__} != {expected}. "
        "Something in the requirement set asked for a different build; pin it or "
        "relax the floor in pyproject.toml instead of letting ACPT's stack drift."
    )

# Qwen3.5/3.6/3.8 checkpoints declare `model_type: qwen3_5`, which transformers
# below 5.8 cannot resolve -- it fails with a KeyError at load time.
if tuple(int(p) for p in transformers.__version__.split(".")[:2]) < (5, 8):
    raise SystemExit(f"transformers {transformers.__version__} cannot load qwen3_5 checkpoints")

print("python", sys.version.split()[0])
print("torch", torch.__version__, "cuda", torch.version.cuda)
for module in (transformers, accelerate, peft, trl, bitsandbytes, datasets):
    print(module.__name__, module.__version__)

# Three GPU jobs died in HFLM's constructor before the evaluator ever scored a
# token: `load_in_4bit` (a transformers v4 shim v5 deleted), then
# `quantization_config` twice (a name HFLM derives from the checkpoint's own
# config and passes itself, so ours arrived as a duplicate). Each cost an image
# build plus a training run to discover, because the evaluator only runs after
# training succeeds.
#
# `ffsft.eval.run` now builds the model itself and hands HFLM the object, which
# skips `_create_model` entirely. That only holds while these parameter names
# mean what they mean here, so assert the contract at build time -- it is free,
# offline, and catches the whole class of breakage in the layer that introduced
# it.
_hflm_params = inspect.signature(HFLM.__init__).parameters
for _name in ("pretrained", "backend", "tokenizer", "max_length", "batch_size"):
    if _name not in _hflm_params:
        raise SystemExit(
            f"lm_eval HFLM no longer accepts `{_name}`; ffsft.eval.run.harness_kwargs "
            "and load_for_eval need updating before this image is usable"
        )
if "quantization_config" in _hflm_params:
    raise SystemExit(
        "lm_eval HFLM now takes `quantization_config` directly. ffsft.eval.run "
        "quantises the model itself to work around its absence -- reconcile the two "
        "before something passes it twice again."
    )
_pretrained = str(_hflm_params["pretrained"].annotation)
if "PreTrainedModel" not in _pretrained:
    raise SystemExit(
        f"lm_eval HFLM `pretrained` is now {_pretrained}; it no longer takes a "
        "preloaded model, so ffsft.eval.run cannot bypass its loader"
    )
print("lm_eval HFLM accepts a preloaded model")
