#!/usr/bin/env bash
# Point the token viewer at a live managed endpoint.
#
#   scripts/run_token_viewer.sh <endpoint> [port]
#
# The key is fetched into this process's environment and never written to disk.
# The browser never sees it either: the page and the proxy share an origin, so
# the Authorization header is added server-side. The viewer binds to 127.0.0.1.
. "$(dirname "$0")/_common.sh"

EP="${1:?usage: run_token_viewer.sh <endpoint> [port]}"
PORT="${2:-8112}"
PY="${FFSFT_PYTHON:-.venv/bin/python}"

BASE="$(ffsft_scoring_base "$EP")"
[ -n "$BASE" ] || { echo "endpoint '$EP' has no scoring URI -- is it created?" >&2; exit 1; }

FFSFT_ENDPOINT_KEY="$(ffsft_endpoint_key "$EP")"
export FFSFT_ENDPOINT_KEY
[ -n "$FFSFT_ENDPOINT_KEY" ] || { echo "key lookup failed" >&2; exit 1; }

echo "upstream: $BASE"
echo "viewer  : http://127.0.0.1:$PORT"
exec "$PY" scripts/token_viewer.py --upstream "$BASE" --port "$PORT"
