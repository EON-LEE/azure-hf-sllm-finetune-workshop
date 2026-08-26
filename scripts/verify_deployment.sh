#!/usr/bin/env bash
# Prove a deployment serves, without moving any traffic to it.
#
#   scripts/verify_deployment.sh <endpoint> <deployment> [<deployment> ...]
#
# A deployment created at `--traffic 0` is invisible from the endpoint URL. The
# `azureml-model-deployment` header is what addresses one directly, which is why
# this step is curl: it is the only way to test a deployment before it is
# carrying anything. Load testing comes after the cutover.
#
# The check is not "did it return 200". `ffsft.serve.smoke` reads the body and
# fails on a reasoning trace left inside `content`, on an empty reply, and on a
# reply truncated before the answer began. Naming two deployments compares them
# -- run it against the old one and the new one to see what changed.
. "$(dirname "$0")/_common.sh"

EP="${1:?usage: verify_deployment.sh <endpoint> <deployment> [<deployment> ...]}"
shift
[ "$#" -ge 1 ] || { echo "name at least one deployment" >&2; exit 2; }

PROMPT="${FFSFT_SMOKE_PROMPT:-한국어로 한 문장만: 서울은 어떤 도시야?}"
MAX_TOKENS="${FFSFT_SMOKE_MAX_TOKENS:-400}"
PY="${FFSFT_PYTHON:-.venv/bin/python}"

BASE="$(ffsft_scoring_base "$EP")"
[ -n "$BASE" ] || { echo "endpoint '$EP' has no scoring URI -- is it created?" >&2; exit 1; }
echo "endpoint: $BASE"

KEY="$(ffsft_endpoint_key "$EP")"   # into a variable only; never to disk
[ -n "$KEY" ] || { echo "key lookup failed" >&2; exit 1; }
echo "key acquired (length ${#KEY})"

RC=0
FAILED=""
for DEP in "$@"; do
  echo
  echo "=== $DEP ==="
  ST="$(az rest --method get \
        --url "$FFSFT_ARM/onlineEndpoints/$EP/deployments/$DEP?api-version=2024-04-01" \
        --query "properties.provisioningState" -o tsv 2>/dev/null)"
  echo "  provisioningState  : ${ST:-<not found>}"
  if [ "$ST" != "Succeeded" ]; then
    echo "  -> not ready; skipping the call"
    RC=1; FAILED="$FAILED $DEP"
    continue
  fi

  curl -sS -m "${FFSFT_SMOKE_TIMEOUT:-180}" "$BASE/chat/completions" \
    -H "Authorization: Bearer $KEY" \
    -H "Content-Type: application/json" \
    -H "azureml-model-deployment: $DEP" \
    -d "$("$PY" -c '
import json, os, sys
print(json.dumps({
    "model": os.environ.get("FFSFT_SERVED_MODEL_NAME", "ffsft"),
    "messages": [{"role": "user", "content": sys.argv[1]}],
    "max_tokens": int(sys.argv[2]),
    "temperature": 0.0,
}))' "$PROMPT" "$MAX_TOKENS")" \
    | "$PY" -m ffsft.serve.smoke --max-tokens "$MAX_TOKENS" || { RC=1; FAILED="$FAILED $DEP"; }
done

echo
if [ "$RC" -eq 0 ]; then
  echo "OK. Traffic has not been touched -- cut over with:"
  echo "  ffsft-deploy shift --endpoint $EP --to <deployment>"
else
  echo "did NOT pass:${FAILED}"
  echo "Do not shift traffic to those. A deployment named only as a"
  echo "comparison baseline is expected to fail here -- read the lines above,"
  echo "not just this exit code."
fi
exit "$RC"
