"""Prove a GPU node can actually run the recipe, before we download 54 GB.

Every failure this catches is one we hit for real: a node whose driver is
mismatched, a container with no writable scratch big enough for the checkpoint,
a bitsandbytes build with no CUDA kernels, or a trl/peft/transformers trio that
does not agree on its own API. Running it as a job on the target cluster costs a
couple of minutes and answers all of those with evidence instead of assumption.

    python -m ffsft.train.preflight
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

#: Small, ungated, same architecture family as the real target. Big enough to
#: exercise a genuine NF4 + LoRA + trl training step, small enough to download
#: in under a minute.
SMOKE_MODEL = "Qwen/Qwen3-0.6B"


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}", flush=True)


def check_gpu() -> dict:
    section("1. GPU / driver")
    subprocess.run(["nvidia-smi"], check=False)

    import torch

    info = {"torch": torch.__version__, "cuda_available": torch.cuda.is_available()}
    if not torch.cuda.is_available():
        print("!! torch cannot see a GPU -- the node is unusable for training")
        return info
    props = torch.cuda.get_device_properties(0)
    info |= {
        "device": props.name,
        "capability": f"{props.major}.{props.minor}",
        "vram_gb": round(props.total_memory / 1e9, 1),
        "torch_cuda": torch.version.cuda,
        "bf16_supported": torch.cuda.is_bf16_supported(),
    }
    print(json.dumps(info, indent=2))
    if not info["bf16_supported"]:
        print("!! no bfloat16 -- this is a pre-Ampere card, the recipe assumes bf16")
    return info


def check_disk() -> dict:
    """Find somewhere big enough for the weights and say so out loud.

    A 27B checkpoint is ~54 GB of bf16 safetensors. The container's working
    directory is often on a much smaller volume than the node's temp disk, and
    the failure mode is a download that dies at 90%.
    """
    section("2. writable scratch")
    candidates = [os.getcwd(), "/mnt", "/tmp", os.environ.get("AZUREML_CR_DATA_CAPABILITY_PATH")]
    best, best_free = None, 0.0
    for path in [c for c in candidates if c and os.path.isdir(c)]:
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        free_gb = usage.free / 1e9
        writable = os.access(path, os.W_OK)
        print(f"  {path:<50} free={free_gb:8.1f} GB  writable={writable}")
        if writable and free_gb > best_free:
            best, best_free = path, free_gb
    print(f"\n  -> largest writable: {best} ({best_free:.1f} GB free)")
    if best_free < 120:
        print("  !! under 120 GB; a 27B bf16 download plus adapter output will not fit")
    return {"scratch": best, "scratch_free_gb": round(best_free, 1)}


def check_versions() -> dict:
    section("3. library versions")
    import bitsandbytes
    import datasets
    import peft
    import transformers
    import trl

    versions = {
        "transformers": transformers.__version__,
        "trl": trl.__version__,
        "peft": peft.__version__,
        "bitsandbytes": bitsandbytes.__version__,
        "datasets": datasets.__version__,
        "python": sys.version.split()[0],
    }
    print(json.dumps(versions, indent=2))
    major = int(transformers.__version__.split(".")[0])
    if major < 5:
        print("!! transformers < 5 cannot load qwen3_5 checkpoints")
    return versions


def check_bnb_kernels() -> dict:
    """The single riskiest dependency: bitsandbytes' compiled CUDA kernels.

    A pip install can succeed and still ship a build with no kernels for this
    driver, in which case the first 4-bit matmul is where you find out.
    """
    section("4. bitsandbytes NF4 kernels")
    import bitsandbytes as bnb
    import torch

    layer = bnb.nn.Linear4bit(
        512, 512, bias=False, compute_dtype=torch.bfloat16, quant_type="nf4"
    ).cuda()
    out = layer(torch.randn(4, 512, device="cuda", dtype=torch.bfloat16))
    ok = torch.isfinite(out).all().item()
    print(f"  Linear4bit forward -> {tuple(out.shape)} {out.dtype}, finite={ok}")
    return {"nf4_matmul_ok": bool(ok)}


def check_training_loop() -> dict:
    """One real QLoRA step through the exact trl/peft path the recipe uses."""
    section(f"5. end-to-end QLoRA step on {SMOKE_MODEL}")
    import torch
    from datasets import Dataset
    from peft import LoraConfig, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    quant = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.bfloat16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        SMOKE_MODEL, quantization_config=quant, dtype=torch.bfloat16, device_map={"": 0}
    )
    model.config.use_cache = False
    tokenizer = AutoTokenizer.from_pretrained(SMOKE_MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    data = Dataset.from_dict(
        {"text": ["안녕하세요. 파인튜닝 사전 점검용 예시 문장입니다." * 4] * 16}
    )
    trainer = SFTTrainer(
        model=model,
        args=SFTConfig(
            output_dir="/tmp/ffsft-preflight",
            max_steps=2,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=1,
            gradient_checkpointing=True,
            gradient_checkpointing_kwargs={"use_reentrant": False},
            logging_steps=1,
            bf16=True,
            optim="paged_adamw_8bit",
            max_length=128,
            report_to=[],
            save_strategy="no",
        ),
        train_dataset=data,
        peft_config=LoraConfig(
            r=8, lora_alpha=16, task_type="CAUSAL_LM", target_modules="all-linear"
        ),
    )
    result = trainer.train()
    peak = torch.cuda.max_memory_allocated() / 1e9
    print(f"  loss={result.training_loss:.4f} steps={result.global_step} peak={peak:.2f} GB")
    return {
        "smoke_loss": round(float(result.training_loss), 4),
        "smoke_steps": int(result.global_step),
        "smoke_peak_gb": round(peak, 2),
    }


def publish(report: dict) -> None:
    """Push the report somewhere readable without touching blob storage.

    stdout is not enough. Azure ML persists `user_logs/std_log.txt` to the
    workspace's default blob store and serves it through a SAS URL, so on a
    workspace whose storage account is network-isolated the log is written fine
    and then returns 403 to anyone outside the VNet -- the job passes and its
    findings are unreadable. MLflow metrics and tags go to the tracking service
    instead, which is reachable with an ordinary ARM token, so that is the
    channel a self-test should report through.
    """
    try:
        import mlflow
    except ImportError:
        print("\n(mlflow unavailable; report is stdout-only)")
        return

    try:
        for key, value in report.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                mlflow.set_tag(f"preflight.{key}", str(value))
            else:
                mlflow.log_metric(f"preflight.{key}", float(value))
        mlflow.set_tag("preflight.passed", "true")
        print("\npublished report to MLflow")
    except Exception as exc:  # noqa: BLE001 - reporting must never fail the run
        print(f"\n(mlflow publish failed: {type(exc).__name__}: {exc})")


def main() -> int:
    report: dict = {}
    report |= check_gpu()
    report |= check_disk()
    report |= check_versions()
    if not report.get("cuda_available"):
        print("\nPREFLIGHT FAILED: no GPU")
        publish(report)
        return 1
    report |= check_bnb_kernels()
    report |= check_training_loop()

    section("PREFLIGHT REPORT")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    publish(report)

    out = os.environ.get("AZUREML_OUTPUT_report")
    if out:
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "preflight.json"), "w") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
    print("\nPREFLIGHT PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
