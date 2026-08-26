#!/usr/bin/env bash
# Serve the merged model and load-test it, both inside one Azure ML command job.
#
# The server is `serve_entrypoint.sh` -- the same script a managed online
# endpoint would run, reading the same environment variables -- started in the
# background rather than reimplemented. The client is `ffsft.serve.loadtest`,
# the same sweep that would have been pointed at a scoring URI. Only the
# transport changes: 127.0.0.1 instead of an HTTPS front door.
#
# What that means for the numbers, stated once so the report is not misread:
# TTFT and TPOT here exclude TLS, the endpoint's auth check and one WAN hop.
# For a 27B decoding at tens of tokens per second those are small next to the
# model, but they are not zero, and nothing measured here says anything about
# Azure's routing layer -- only about the model on the GPU.
set -euo pipefail

# Positional, not environment. The first attempt passed these as a command
# prefix (`MODEL_PATH=... bash bench_entrypoint.sh`) and vLLM still came up
# with `--model /var/azureml-app/azureml-models`, the value
# docker/Dockerfile.serve:45 bakes into the image this one is FROM. An
# inherited ENV that is always set also disarms the `:?` guard below: a
# missing binding stops being an error and becomes a server that quietly
# loads the wrong thing. An argument cannot be shadowed by the image.
MODEL_ROOT="${1:?usage: bench_entrypoint.sh <model-root> <output-dir>}"
OUTPUT_DIR="${2:?usage: bench_entrypoint.sh <model-root> <output-dir>}"

# ------------------------------------------------------------- reporting ----
# Set up before anything can fail, because reporting is the only way anything
# leaves this node. This workspace's storage account has publicNetworkAccess
# disabled, so ./outputs, the SAS link and `jobs.stream` are all 403 from
# outside the VNet -- job helpful_jelly_gndv8d135q streamed three lines, none
# of them its own. MLflow reaches the tracking service instead of blob, so a
# run that dies here still says why. RUN_PHASE names the phase in progress,
# which means whatever it holds at exit is the phase that failed. It is
# deliberately not BENCH_*: that namespace is the job's configuration knobs,
# every one of which `bench_env` must set, and this is internal state no job
# should be able to hand in.
mkdir -p "${OUTPUT_DIR}"
VLLM_LOG="${OUTPUT_DIR}/vllm.log"
RUN_PHASE="resolving_model"

report() {
    echo "[bench] publishing report (status ${RUN_PHASE})"
    python3 -c "from ffsft.serve.bench_report import main; raise SystemExit(main())" \
        --output-dir "${OUTPUT_DIR}" \
        --status "${RUN_PHASE}" \
        --vllm-log "${VLLM_LOG}" || echo "[bench] report failed (non-fatal)"
}

# Reaches every exit path, including the ones that fire before there is a
# server to stop -- `SERVER_PID` is guarded rather than assumed. The server is
# killed first so its log is flushed before the tail is read, and so a
# LowPriority node is not held while the report goes out.
cleanup() {
    if [[ -n "${SERVER_PID:-}" ]] && kill -0 "${SERVER_PID}" 2>/dev/null; then
        echo "[bench] stopping vLLM (pid ${SERVER_PID})"
        kill "${SERVER_PID}" 2>/dev/null || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
    report
}
trap cleanup EXIT

# Resolve here rather than leaving it to serve_entrypoint.sh. That script's
# resolve_model() searches -maxdepth 4 and, finding nothing, falls back to
# treating the path as a Hugging Face repo id -- so a path problem surfaces
# roughly five minutes later as an HFValidationError from deep inside vLLM's
# config loader, which reads like a model problem and is not. Azure ML nests a
# registered model as <root>/<name>/<version>/<the run's own output layout>,
# and this asset carries an extra azureml/<run-id>/merged/ of its own, so the
# depth is not knowable from here. Search deep, take the shallowest hit, and
# fail with the directory listing if there is none.
MODEL_PATH="$(find "${MODEL_ROOT}" -maxdepth 8 -name config.json -printf '%d %h\n' 2>/dev/null \
              | sort -n | head -1 | cut -d' ' -f2- || true)"
if [[ -z "${MODEL_PATH}" ]]; then
    RUN_PHASE="model_not_found"
    echo "[bench] FATAL: no config.json under ${MODEL_ROOT}"
    echo "[bench] --- what is actually there ---"
    ls -la "${MODEL_ROOT}" 2>&1 | head -40 || true
    find "${MODEL_ROOT}" -maxdepth 4 2>/dev/null | head -60 || true
    exit 1
fi
export MODEL_PATH OUTPUT_DIR

PORT="${VLLM_PORT:-8000}"
BASE="http://127.0.0.1:${PORT}"
MODEL_NAME="${SERVED_MODEL_NAME:-ffsft}"

# A 27B in bf16 is ~54 GB of safetensors read off local disk and moved onto the
# card. 60 minutes is not an estimate of how long that takes; it is the point
# past which something is wrong and waiting longer only wastes a node. The loop
# below exits far earlier than this on both success and crash -- the timeout
# only covers the case where vLLM is alive and making no progress.
STARTUP_TIMEOUT="${BENCH_STARTUP_TIMEOUT:-3600}"
CONCURRENCY="${BENCH_CONCURRENCY:-1,2,4,8,16,32}"
REQS="${BENCH_REQUESTS_PER_LEVEL:-16}"
MAX_TOKENS="${BENCH_MAX_TOKENS:-128}"
TTFT_SLO="${BENCH_TTFT_SLO:-1.0}"
# JSON object forwarded to every chat completion. Empty means "send nothing",
# which is not the same as "{}" -- the server's own default then applies, and
# for Qwen3 that default is thinking on. `bench_job.bench_env` fills this from
# the registry so the client asks for the same mode the server is flagged for.
CHAT_TEMPLATE_KWARGS="${BENCH_CHAT_TEMPLATE_KWARGS:-}"

echo "[bench] model root : ${MODEL_ROOT}"
echo "[bench] model path : ${MODEL_PATH}"
echo "[bench] served as  : ${MODEL_NAME}"
echo "[bench] output dir : ${OUTPUT_DIR}"
echo "[bench] sweep      : ${CONCURRENCY} x ${REQS} req, ${MAX_TOKENS} tok"
echo "[bench] --- nvidia-smi ---"
nvidia-smi || echo "[bench] nvidia-smi unavailable"

# ---------------------------------------------------------------- server ----
# Overridable so this script can be exercised against scripts/mock_vllm_server.py
# on a laptop. Everything below -- backgrounding, the liveness-before-health
# ordering, the trap, the smoke call, the sweep -- is shell that fails the same
# way with or without a GPU, and finding those failures on a LowPriority A100
# costs an allocation wait plus several minutes of weight loading per attempt.
# Production never sets this; the default is the real server.
SERVE_CMD=(${BENCH_SERVE_CMD:-/usr/local/bin/serve_entrypoint.sh})

echo "[bench] starting server: ${SERVE_CMD[*]} (log -> ${VLLM_LOG})"
"${SERVE_CMD[@]}" > "${VLLM_LOG}" 2>&1 &
SERVER_PID=$!

# The EXIT trap installed above now has a pid to act on. Killing matters
# because a LowPriority node that ends while vLLM still holds the GPU is a node
# the next job waits for.

# ---------------------------------------------------------------- health ----
# Polls /health, but checks liveness of the process first. That ordering is the
# whole point: if vLLM dies on an unsupported flag it dies in seconds, and a
# loop that only watched the port would keep a 45-minute-old A100 node busy
# discovering nothing. Dumping the tail here also means the failure is legible
# in the job's own stdout, which survives even when the output upload does not.
RUN_PHASE="waiting_for_health"
echo "[bench] waiting for ${BASE}/health (timeout ${STARTUP_TIMEOUT}s)"
START=$(date +%s)
while true; do
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        wait "${SERVER_PID}" 2>/dev/null && RC=0 || RC=$?
        RUN_PHASE="server_exited_${RC}"
        echo "[bench] FATAL: vLLM exited with ${RC} before becoming healthy"
        echo "[bench] --- last 120 lines of vllm.log ---"
        tail -n 120 "${VLLM_LOG}" || true
        exit 1
    fi
    if python3 -c "
import sys, urllib.request
try:
    with urllib.request.urlopen('${BASE}/health', timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        ELAPSED=$(( $(date +%s) - START ))
        echo "[bench] healthy after ${ELAPSED}s"
        break
    fi
    NOW=$(( $(date +%s) - START ))
    if [[ "${NOW}" -ge "${STARTUP_TIMEOUT}" ]]; then
        RUN_PHASE="health_timeout"
        echo "[bench] FATAL: not healthy after ${NOW}s"
        echo "[bench] --- last 120 lines of vllm.log ---"
        tail -n 120 "${VLLM_LOG}" || true
        exit 1
    fi
    if (( NOW % 60 < 10 )); then
        echo "[bench] still loading (${NOW}s)"
    fi
    sleep 10
done

# ----------------------------------------------------------------- smoke ----
# /health returns 200 once the engine is constructed, which is not the same as
# the model being able to decode. One real Korean completion is the difference
# between "it started" and "it serves", and it is worth the 20 seconds: a sweep
# against a server that 500s on every request produces a full table of zeros
# that looks like a measurement.
RUN_PHASE="smoke"
echo "[bench] smoke test"
python3 - "${BASE}" "${MODEL_NAME}" "${OUTPUT_DIR}/smoke.json" "${CHAT_TEMPLATE_KWARGS}" <<'PY'
import json, sys, urllib.error, urllib.request

base, model, out = sys.argv[1], sys.argv[2], sys.argv[3]
ctk = json.loads(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4].strip() else None
payload_body = {
    "model": model,
    "messages": [{"role": "user", "content":
                  "한국의 수도는 어디이고, 그곳이 수도가 된 역사적 배경을 두 문장으로 설명해줘."}],
    "max_tokens": 200,
    "temperature": 0.7,
}
# Same mode as the sweep and as training. A smoke test that asks in a different
# mode answers a question nobody asked.
if ctk:
    payload_body["chat_template_kwargs"] = ctk
body = json.dumps(payload_body).encode()
req = urllib.request.Request(
    f"{base}/v1/chat/completions", data=body,
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=300) as r:
        payload = json.load(r)
except urllib.error.HTTPError as exc:
    print(f"[bench] smoke FAILED {exc.code}: {exc.read().decode()[:500]}")
    raise SystemExit(1)

reply = payload["choices"][0]["message"]["content"]
print("[bench] --- reply ---")
print(reply)
print("[bench] --- usage ---")
print(payload.get("usage"))
with open(out, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2, ensure_ascii=False)
PY

# ------------------------------------------------------------------ sweep ----
RUN_PHASE="sweeping"
echo "[bench] load test"
set +e
# Not `-m ffsft.serve.loadtest`: `ffsft/serve/__init__.py` imports the module, so
# runpy finds it already in sys.modules and re-executes it under a second name,
# warning that the result "may result in unpredictable behaviour". That warning
# is harmless here and would still be the wrong thing to print in the log of a
# measurement run. The package is on PYTHONPATH rather than pip-installed, so
# the `ffsft-loadtest` console script does not exist in this image.
python3 -c "from ffsft.serve.loadtest import main; raise SystemExit(main())" \
    --base-url "${BASE}/v1" \
    --model "${MODEL_NAME}" \
    --concurrency "${CONCURRENCY}" \
    --requests-per-level "${REQS}" \
    --max-tokens "${MAX_TOKENS}" \
    --ttft-slo "${TTFT_SLO}" \
    ${CHAT_TEMPLATE_KWARGS:+--chat-template-kwargs "${CHAT_TEMPLATE_KWARGS}"} \
    --timeout 600 \
    --output "${OUTPUT_DIR}/loadtest.json" \
    2>&1 | tee "${OUTPUT_DIR}/loadtest.log"
RC="${PIPESTATUS[0]}"
set -e
# "swept" is the only value that means the measurements exist. `build_report`
# keys the vllm.log tail off it: on any other status the tail is a failure
# reason worth carrying, on this one it is throughput chatter.
RUN_PHASE="swept"

# The GPU memory line is written after the sweep on purpose: read before it, it
# shows the reservation, and after it, what the sweep actually touched.
echo "[bench] --- nvidia-smi after sweep ---"
nvidia-smi || true

# Echo the artefacts to stdout as well as writing them to the declared output.
# The output does get uploaded -- the node reaches the workspace storage over a
# private endpoint -- but that storage has publicNetworkAccess disabled, so a
# workstation on the public internet gets 403 AuthorizationFailure on both a
# direct read and a service-issued SAS link. The live job stream is the only
# path off the node that works from outside the VNet, so the numbers go there
# too. Bounded by the sweep's own size, which is six rows of summary stats.
echo "[bench] --- loadtest.json ---"
cat "${OUTPUT_DIR}/loadtest.json" 2>/dev/null || echo "[bench] (no loadtest.json)"
# `json.dump` leaves no trailing newline, so without this the closing brace and
# the end marker share a line and a naive extractor loses the brace.
echo
echo "[bench] --- end loadtest.json ---"

echo "[bench] loadtest exit ${RC}"
exit "${RC}"
