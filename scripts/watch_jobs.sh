#!/usr/bin/env bash
# Follow one or more Azure ML jobs to a terminal state, printing only changes.
#
#   scripts/watch_jobs.sh <run-name> [<run-name> ...]
#   scripts/watch_jobs.sh TRAIN:olden_bean_302vkc7nbz MERGE:loving_pumpkin_h0slhvf2l6
#
# A `LABEL:run-name` pair labels the output; a bare run name labels itself.
#
# Why not `MLClient.jobs.stream()`: on a workspace whose storage account is
# network-isolated, the SAS URL it reads returns AuthorizationFailure to anyone
# outside the VNet. The job runs, the logs are written, and they are unreadable
# from a laptop. MLflow's tracking service is the channel that does work -- an
# ordinary ARM token, no blob access anywhere in the path -- so metrics come
# from `lastvalues` and status comes from ARM.
#
# `lastvalues` returns `[null]` for a metric whose name is registered but whose
# value has not landed yet. Calling .get() on that None is how an earlier
# version of this went silent at exactly the moment metrics started arriving.
#
# `log_metric`'s value write authenticates to the workspace storage account,
# and on a tenant that disables shared-key storage access it fails outright
# ("Authentication to workspace storage account failed") while `set_tag`,
# which never touches storage, keeps working -- see `ffsft/mlflow_report.py`.
# `publish()` now retries a failed metric as a tag holding its stringified
# value under the SAME name, so this script also reads tags via the MLflow
# `runs/get` endpoint and prints any that are not the metric's own registered
# name (already covered by `lastvalues`), catching the fallback wherever the
# metric channel is the one that is blocked.
. "$(dirname "$0")/_common.sh"

[ "$#" -ge 1 ] || { echo "usage: watch_jobs.sh [LABEL:]<run-name> ..." >&2; exit 2; }

API="https://$FFSFT_LOCATION.api.azureml.ms"
PY="${FFSFT_PYTHON:-.venv/bin/python}"
INTERVAL="${FFSFT_WATCH_INTERVAL:-45}"
MAX_POLLS="${FFSFT_WATCH_POLLS:-200}"

STATE="$(mktemp)"; SEEN="$(mktemp)"; JSON="$(mktemp)"
trap 'rm -f "$STATE" "$SEEN" "$JSON"' EXIT

T0=$(date +%s)
for i in $(seq 1 "$MAX_POLLS"); do
  EL=$(( ($(date +%s) - T0) / 60 ))
  TOK="$(az account get-access-token --resource https://ml.azure.com --query accessToken -o tsv 2>/dev/null)"
  DONE=0; TOTAL=0

  for PAIR in "$@"; do
    case "$PAIR" in *:*) TAG="${PAIR%%:*}"; RUN="${PAIR##*:}";; *) TAG="$PAIR"; RUN="$PAIR";; esac
    TOTAL=$((TOTAL+1))

    ST="$(az rest --method get \
          --url "https://management.azure.com/$FFSFT_WS_URI/jobs/$RUN?api-version=2024-10-01" \
          --query "properties.status" -o tsv 2>/dev/null)"
    [ -z "$ST" ] && ST="?"

    PREV="$(grep "^$TAG=" "$STATE" 2>/dev/null | cut -d= -f2)"
    if [ "$ST" != "$PREV" ]; then
      echo "$TAG [${EL}min] $ST"
      if [ "$ST" = "Failed" ]; then
        curl -s --max-time 40 -H "Authorization: Bearer $TOK" \
          "$API/history/v1.0/$FFSFT_WS_URI/runs/$RUN/details" -o "$JSON" 2>/dev/null
        "$PY" -c '
import json, sys
try: d = json.load(open(sys.argv[1]))
except Exception: raise SystemExit
e = (d.get("error") or {}).get("error") or {}
m = e.get("message") or ""
if m: print(f"{sys.argv[2]}-REASON [{sys.argv[3]}min] " + " ".join(m.split())[:400])
' "$JSON" "$TAG" "$EL"
      fi
      grep -v "^$TAG=" "$STATE" > "$STATE.n" 2>/dev/null; mv "$STATE.n" "$STATE"
      echo "$TAG=$ST" >> "$STATE"
    fi

    curl -s --max-time 40 -X POST -H "Authorization: Bearer $TOK" \
      -H "Content-Type: application/json" -d '{}' \
      "$API/metric/v2.0/$FFSFT_WS_URI/runs/$RUN/lastvalues" -o "$JSON" 2>/dev/null
    "$PY" -c '
import json, sys
path, seen_path, tag, el = sys.argv[1:5]
try: d = json.load(open(path))
except Exception: raise SystemExit
try: seen = set(open(seen_path).read().split("\n"))
except Exception: seen = set()
fresh = []
for m in d.get("value") or []:
    name = m.get("name")
    if not name:
        continue
    # `lastvalues` yields [null] while a name is registered but its value has
    # not landed. A name appearing at all is worth reporting -- it says the run
    # reached the block that logs it.
    rows = [r for r in (m.get("value") or []) if isinstance(r, dict)]
    v = rows[-1].get("data", {}).get(name) if rows else None
    key = f"{tag}|{name}={v}"
    if key not in seen:
        fresh.append((key, name, "(logged, no value yet)" if v is None else v))
        seen.add(key)
with open(seen_path, "a") as fh:
    for key, _n, _v in fresh:
        fh.write(key + "\n")
for _k, name, v in fresh:
    print(f"{tag}-METRIC [{el}min] {name} = {v}")
' "$JSON" "$SEEN" "$TAG" "$EL"

    curl -s --max-time 40 -H "Authorization: Bearer $TOK" \
      "$API/mlflow/v1.0/$FFSFT_WS_URI/api/2.0/mlflow/runs/get?run_id=$RUN" -o "$JSON" 2>/dev/null
    "$PY" -c '
import json, sys
path, seen_path, tag, el = sys.argv[1:5]
try: d = json.load(open(path))
except Exception: raise SystemExit
try: seen = set(open(seen_path).read().split("\n"))
except Exception: seen = set()
tags = (d.get("run") or {}).get("data", {}).get("tags") or []
fresh = []
for t in tags:
    name = t.get("key")
    v = t.get("value")
    # mlflow.* are the tracking service'"'"'s own bookkeeping tags, not report
    # content -- skip them so this only surfaces what publish() sent.
    if not name or name.startswith("mlflow."):
        continue
    key = f"{tag}|TAG|{name}={v}"
    if key not in seen:
        fresh.append((key, name, v))
        seen.add(key)
with open(seen_path, "a") as fh:
    for key, _n, _v in fresh:
        fh.write(key + "\n")
for _k, name, v in fresh:
    print(f"{tag}-TAG [{el}min] {name} = {v}")
' "$JSON" "$SEEN" "$TAG" "$EL"

    case "$ST" in Completed|Failed|Canceled) DONE=$((DONE+1));; esac
  done

  [ "$DONE" -ge "$TOTAL" ] && { echo "ALL-TERMINAL [${EL}min]"; exit 0; }
  [ $((i % 10)) -eq 0 ] && echo "WAIT [${EL}min] $(tr '\n' ' ' < "$STATE")"
  sleep "$INTERVAL"
done
echo "gave up after $MAX_POLLS polls" >&2
exit 1
