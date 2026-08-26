#!/usr/bin/env bash
# Launch vLLM's OpenAI server with the settings an Azure ML deployment passes in
# through environment_variables.
#
# Every flag below that is not obviously generic is here because of a measured
# constraint of the Qwen3.5/3.8 hybrid architecture (verified 2026-08-21):
#
#   --mamba-cache-mode align   REQUIRED. 48 of the 27B's 64 layers are Gated
#                              DeltaNet linear attention. vLLM's implementation
#                              raises NotImplementedError for mode "all".
#   --language-model-only      Qwen3.8-27B is multimodal (vision_config,
#                              image_token_id, video_token_id in its config).
#                              A Korean text SFT deployment should not pay VRAM
#                              for the vision tower.
#   --reasoning-parser qwen3   Qwen3 emits a <think> block. Without the parser
#                              it leaks into `content` and every downstream
#                              consumer sees reasoning text as the answer.
set -euo pipefail

resolve_model() {
    local root="${MODEL_PATH}"
    if [[ -f "${root}/config.json" ]]; then
        echo "${root}"
        return
    fi
    # Azure ML nests a registered model as <mount>/<name>/<version>/<files> and
    # picks the directory name itself, so hardcoding the path is the most common
    # reason a managed online endpoint comes up unhealthy. Find the shallowest
    # config.json so a checkpoint that itself contains sub-models is not
    # mistaken for the top-level one.
    # `|| true` is load-bearing, not defensive noise. MODEL_PATH is allowed to be
    # a bare Hugging Face repo id, in which case `find` exits 1 because there is
    # no such directory; under `set -euo pipefail` that failing assignment would
    # terminate the script before vLLM ever starts. It survives today only
    # because this sits in a function body called from a command substitution,
    # which is one of the contexts where bash stops honouring `set -e` -- an
    # accident of placement that inlining this function would silently undo.
    local found
    found="$(find "${root}" -maxdepth 4 -name config.json -printf '%d %h\n' 2>/dev/null \
             | sort -n | head -1 | cut -d' ' -f2- || true)"
    if [[ -n "${found}" ]]; then
        echo "${found}"
        return
    fi
    # Nothing mounted: treat MODEL_PATH as a Hugging Face repo id.
    echo "${MODEL_PATH}"
}

# Fetch stage: pull the checkpoint out of blob storage with the deployment's own
# managed identity, before anything tries to resolve a path.
#
# This runs at top level and NOT inside `resolve_model` for two separate
# reasons, either of which alone would be enough:
#
#   1. `resolve_model` is called from a command substitution, and that is one of
#      the contexts where bash stops honouring `set -e`. A failed download there
#      would be ignored and the script would fall through to the Hugging Face
#      branch -- serving the untuned base model from a deployment that believes
#      it is serving the fine-tune.
#   2. The fetcher writes progress to stdout. Inside `$(...)` that output would
#      be captured and become part of the model path.
#
# Here, a non-zero exit from the fetcher kills the container under `set -e`,
# which is the intended behaviour: no weights, no server.
if [[ -n "${MODEL_BLOB_URI}" ]]; then
    echo "[serve] fetching model from blob: ${MODEL_BLOB_URI}"
    python3 /usr/local/bin/fetch_model.py
    MODEL_PATH="${MODEL_CACHE_DIR}"
fi

MODEL="$(resolve_model)"
echo "[serve] model      : ${MODEL}"
echo "[serve] served as  : ${SERVED_MODEL_NAME}"
echo "[serve] max len    : ${MAX_MODEL_LEN}"
echo "[serve] gpu mem    : ${GPU_MEMORY_UTILIZATION}"
echo "[serve] tp size    : ${TENSOR_PARALLEL_SIZE}"
echo "[serve] mamba cache: ${MAMBA_CACHE_MODE}"

ARGS=(
    --model "${MODEL}"
    --served-model-name "${SERVED_MODEL_NAME}"
    --host 0.0.0.0
    --port "${VLLM_PORT}"
    --max-model-len "${MAX_MODEL_LEN}"
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}"
    --tensor-parallel-size "${TENSOR_PARALLEL_SIZE}"
    --trust-remote-code
)

# The three flags below are Qwen3.5/3.8-specific, and this repo exists to make
# the model swappable. A dense text model such as Qwen3-0.6B has no Mamba state
# and no vision tower, so passing them unconditionally would break every model
# except the one they were added for. Each is therefore opt-out by setting its
# variable to the empty string in the deployment's environment_variables.
if [[ -n "${MAMBA_CACHE_MODE}" ]]; then
    ARGS+=(--mamba-cache-mode "${MAMBA_CACHE_MODE}")
fi

# Asked of the artifact, not of the registry. `configs/models.yaml` marks
# qwen3.8-27b `multimodal: true`, which is true of the base model on the Hub and
# false of what actually gets served: `AutoModelForCausalLM` resolves the
# multimodal checkpoint to its text-only class, so the merge writes
# `Qwen3_5ForCausalLM` / `qwen3_5_text` with no vision_config at all (§36.2).
# Passing --language-model-only then makes vLLM look for the `language_model`
# submodule of a wrapper that is not there, and it dies in load_weights:
#   ValueError: There is no module or parameter named 'language_model' in
#   Qwen3_5Model
# -- job purple_wolf_g3hhc4q5qj, verbatim. The registry describes the base; only
# the checkpoint knows what the checkpoint is.
if [[ "${LANGUAGE_MODEL_ONLY}" == "1" ]]; then
    if [[ -f "${MODEL}/config.json" ]] && ! grep -q '"vision_config"' "${MODEL}/config.json"; then
        echo "[serve] language-model-only requested, but ${MODEL} has no vision_config -- dropping the flag"
    else
        ARGS+=(--language-model-only)
    fi
fi

if [[ -n "${REASONING_PARSER}" ]]; then
    ARGS+=(--reasoning-parser "${REASONING_PARSER}")
fi

# Int4/FP8 is how a 27B fits a 24 GB A10 at all: bf16 needs ~54 GB of weights
# and vLLM's own recipe calls for 1xH200 or 2xH100 at that precision.
if [[ -n "${QUANTIZATION}" ]]; then
    echo "[serve] quantized  : ${QUANTIZATION}"
    ARGS+=(--quantization "${QUANTIZATION}")
fi

# Runtime LoRA mode: keep one base resident and serve adapters by name. Each
# entry of LORA_MODULES is "name=/path"; clients then select one via the
# request's `model` field.
#
# Caveat worth reading before relying on this: vLLM's LoRA hooks attach to
# LinearBase-derived layers. The 16 full-attention layers and all MLPs are
# standard, but an adapter that also targets the Gated DeltaNet projections
# (in_proj_a / in_proj_b / in_proj_qkv / in_proj_z / out_proj) -- which is
# exactly what configs/models.yaml specifies for Qwen3.8 -- is not documented as
# tested. Verify against a merged baseline before shipping this mode.
if [[ "${ENABLE_LORA}" == "1" && -n "${LORA_MODULES}" ]]; then
    echo "[serve] lora       : ${LORA_MODULES} (max rank ${MAX_LORA_RANK})"
    # shellcheck disable=SC2206  # word splitting is intended: one arg per adapter
    LORA_ARR=(${LORA_MODULES})
    ARGS+=(--enable-lora --max-lora-rank "${MAX_LORA_RANK}" --lora-modules "${LORA_ARR[@]}")
fi

if [[ -n "${EXTRA_ARGS}" ]]; then
    # shellcheck disable=SC2206  # deliberate: pass through raw vLLM flags
    EXTRA_ARR=(${EXTRA_ARGS})
    ARGS+=("${EXTRA_ARR[@]}")
fi

echo "[serve] exec: vllm.entrypoints.openai.api_server ${ARGS[*]}"
exec python3 -m vllm.entrypoints.openai.api_server "${ARGS[@]}"
