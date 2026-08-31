# Shared preamble for the workshop shell scripts. Source it, do not run it.
#
#   . "$(dirname "$0")/_common.sh"
#
# Every script here talks to a live subscription and several of them cost money
# or move production traffic, so the identity check is not optional. If you are
# signed in to more than one tenant -- which anyone running this workshop from a
# work laptop usually is -- `az` will happily target the wrong subscription and
# the failure surfaces later as a permission error on a resource you did not
# mean to touch.
#
# Configure with the same FFSFT_* variables the Python side reads
# (`AzureTarget.from_env`), so a shell script and `ffsft-deploy` never disagree
# about which workspace they are pointed at:
#
#   FFSFT_SUBSCRIPTION_ID   required, no default (AZURE_SUBSCRIPTION_ID also read)
#   FFSFT_RESOURCE_GROUP    default rg-ffsft-kc
#   FFSFT_WORKSPACE         default mlw-ffsft
#   FFSFT_LOCATION          default koreacentral
#   FFSFT_ACCOUNT           optional; if set, `az account show` must match it
#
# "never disagree" is a claim about the RULE as much as the variable names, and
# it is `_ffsft_env` below that keeps it. Blank and whitespace-only mean unset on
# both sides. The defaults are written out twice -- once here, once in
# `AzureTarget.from_env` -- because a shell preamble cannot import Python;
# `tests/test_shell_and_python_resolve_the_same_target.py` measures the two
# halves side by side so the copies cannot drift in silence.
#
# Put them in a file outside the repo and source it, so nothing account-specific
# is ever a candidate for a commit:
#
#   set -a; . ~/.ffsft.env; set +a

set -uo pipefail

#: `_env_setting` from `src/ffsft/azure_ml.py`, in shell: print the first of
#: NAME... that holds something other than whitespace, stripped, else DEFAULT.
#:
#: `${VAR:-default}` is not that rule. It fires on empty and NOT on "  ", so a
#: variable holding a space took one branch here and the other one in Python:
#:
#:   FFSFT_RESOURCE_GROUP="  "   bash -> "  "   from_env() -> 'rg-ffsft-kc'
#:
#: Two halves of a single workshop step pointed at two different resource groups,
#: which is precisely what the comment at the top of this file promises cannot
#: happen. Stripping rather than testing for empty also repairs a pasted
#: " rg-ffsft-kc", which Azure 404s on while echoing the name back looking right.
_ffsft_env() {
  local _default="$1"; shift
  local _name _value
  for _name in "$@"; do
    _value="${!_name-}"
    _value="${_value#"${_value%%[![:space:]]*}"}"
    _value="${_value%"${_value##*[![:space:]]}"}"
    if [ -n "$_value" ]; then
      printf '%s' "$_value"
      return 0
    fi
  done
  printf '%s' "$_default"
}

FFSFT_SUBSCRIPTION_ID="$(_ffsft_env '' FFSFT_SUBSCRIPTION_ID AZURE_SUBSCRIPTION_ID)"
if [ -z "$FFSFT_SUBSCRIPTION_ID" ]; then
  # Was `${VAR:?...}`, which accepted "  " and handed a subscription of spaces to
  # `az`, where it surfaces as a not-found on an id that prints as blank.
  echo "set FFSFT_SUBSCRIPTION_ID (or AZURE_SUBSCRIPTION_ID) to the target subscription id" >&2
  exit 1
fi
FFSFT_RESOURCE_GROUP="$(_ffsft_env rg-ffsft-kc FFSFT_RESOURCE_GROUP)"
FFSFT_WORKSPACE="$(_ffsft_env mlw-ffsft FFSFT_WORKSPACE)"
FFSFT_LOCATION="$(_ffsft_env koreacentral FFSFT_LOCATION)"
# Exported, because the disagreement this file promises against is between these
# scripts and the `ffsft-*` commands the labs run beside them. A profile sourced
# without `set -a` leaves these as plain shell variables: `az` here would then use
# the value resolved above while a child `ffsft-deploy` fell back to the
# documented default. Exporting hands the child exactly what was resolved here.
export FFSFT_SUBSCRIPTION_ID FFSFT_RESOURCE_GROUP FFSFT_WORKSPACE FFSFT_LOCATION
# Shell-only, so parity does not reach it -- but the same accident does, and an
# untrimmed "  " here fails every identity check against `expected '  '`.
FFSFT_ACCOUNT="$(_ffsft_env '' FFSFT_ACCOUNT)"

command -v az >/dev/null || { echo "az CLI not found on PATH" >&2; exit 1; }

if [ -n "${FFSFT_ACCOUNT:-}" ]; then
  _got="$(az account show --query 'user.name' -o tsv 2>/dev/null)"
  if [ "$_got" != "$FFSFT_ACCOUNT" ]; then
    echo "IDENTITY MISMATCH: signed in as '${_got:-<nobody>}', expected '$FFSFT_ACCOUNT'" >&2
    echo "  az login --tenant <your-tenant-id>" >&2
    exit 1
  fi
fi

_sub="$(az account show --query id -o tsv 2>/dev/null)"
if [ "$_sub" != "$FFSFT_SUBSCRIPTION_ID" ]; then
  echo "SUBSCRIPTION MISMATCH: az is on '${_sub:-<none>}', FFSFT_SUBSCRIPTION_ID is '$FFSFT_SUBSCRIPTION_ID'" >&2
  echo "  az account set --subscription $FFSFT_SUBSCRIPTION_ID" >&2
  exit 1
fi

#: ARM base URI for the workspace everything below hangs off.
FFSFT_WS_URI="subscriptions/$FFSFT_SUBSCRIPTION_ID/resourceGroups/$FFSFT_RESOURCE_GROUP/providers/Microsoft.MachineLearningServices/workspaces/$FFSFT_WORKSPACE"
FFSFT_ARM="https://management.azure.com/$FFSFT_WS_URI"

#: Fetch an endpoint key into a variable. Never redirect this to a file.
ffsft_endpoint_key() {
  az rest --method post \
    --url "$FFSFT_ARM/onlineEndpoints/$1/listKeys?api-version=2024-04-01" \
    --query primaryKey -o tsv
}

#: The OpenAI-compatible base (".../v1") for an endpoint, read from the control
#: plane rather than assembled from a template -- the region in the hostname is
#: the endpoint's, not necessarily the workspace's.
#:
#: `scoringUri` is not one shape. A deployment built on the default inference
#: server reports `.../score`; one built on a custom image that declares an
#: OpenAI route reports the route itself, `.../v1/chat/completions`. Appending
#: `/chat/completions` to the second produces a 404 whose body parses as JSON
#: and reads downstream as an empty reply -- a working endpoint that looks dead.
#: Both tails are stripped, so what comes back is always the `/v1` base.
ffsft_scoring_base() {
  az rest --method get \
    --url "$FFSFT_ARM/onlineEndpoints/$1?api-version=2024-04-01" \
    --query "properties.scoringUri" -o tsv \
    | sed -E 's#/(score|chat/completions|completions)$##'
}
