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
from lm_eval.models.huggingface import HFLM  # noqa: F401
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
