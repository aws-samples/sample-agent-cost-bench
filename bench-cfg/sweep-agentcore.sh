#!/usr/bin/env bash
# List, and optionally delete, AgentCore gateways and runtimes.
#
# WHY THIS IS SHAPED THE WAY IT IS
#
# The first version took a name prefix and deleted everything matching it. That failed
# twice over on the first real run:
#
#   * The agents under test invented their own resource names instead of using the
#     prefix they were given, so the sweep matched nothing and reported the account
#     clean while seven gateways were still billing.
#   * The obvious fallback — select on creation time instead — would have deleted three
#     production runtimes belonging to an unrelated project
#     that happened to be created inside the same window.
#
# So this script no longer deletes anything it chose itself. It lists, it marks what
# falls in a time window, and deletion requires explicit ids that a human has read.
#
#   # review (default): list everything, mark the window, delete nothing
#   ./bench-cfg/sweep-agentcore.sh --region us-west-2 \
#       --created-after 2026-08-20T23:00:00Z --created-before 2026-08-21T01:30:00Z
#
#   # delete, by explicit id only
#   ./bench-cfg/sweep-agentcore.sh --region us-west-2 --apply \
#       --gateway-ids id1,id2 --runtime-names name1,name2
set -uo pipefail

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
AFTER=""; BEFORE=""; GW_IDS=""; RT_NAMES=""; APPLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region)         REGION="${2:-}";   shift 2 ;;
    --created-after)  AFTER="${2:-}";    shift 2 ;;
    --created-before) BEFORE="${2:-}";   shift 2 ;;
    --gateway-ids)    GW_IDS="${2:-}";   shift 2 ;;
    --runtime-names)  RT_NAMES="${2:-}"; shift 2 ;;
    --apply)          APPLY=1;           shift ;;
    -h|--help)        sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ $APPLY -eq 1 && -z "$GW_IDS" && -z "$RT_NAMES" ]]; then
  cat >&2 <<'USAGE'
--apply needs explicit --gateway-ids and/or --runtime-names.

This script will not delete resources it selected itself. Selecting by name prefix
missed seven live gateways on the first real run, and selecting by creation time
would have deleted an unrelated project's production runtimes from the same window.

Run it without --apply first, read the list, then pass the ids you mean to delete.
USAGE
  exit 2
fi

echo "region:  $REGION"
echo "account: $(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo '?')"
echo "mode:    $([[ $APPLY -eq 1 ]] && echo 'APPLY — deleting the ids given' || echo 'review only — nothing will be deleted')"
[[ -n "$AFTER$BEFORE" ]] && echo "window:  ${AFTER:-(open)} .. ${BEFORE:-(open)}"
echo

# `python3 - <<HEREDOC` would take the program on stdin, leaving json.load(sys.stdin)
# with nothing to read, so the JSON is passed as a file argument instead.
MARKER="$(mktemp)"
trap 'rm -f "$MARKER" "$MARKER".gw "$MARKER".rt' EXIT
cat > "$MARKER" <<'PY'
import sys, json, os, datetime as dt

def parse(v):
    return dt.datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None

after, before = parse(os.environ.get("AFTER")), parse(os.environ.get("BEFORE"))
kind, path = sys.argv[1], sys.argv[2]
with open(path) as fh:
    d = json.load(fh)
items = d.get("items") if kind == "gateway" else d.get("agentRuntimes")
for it in items or []:
    name = it.get("name") or it.get("agentRuntimeName") or "?"
    ident = it.get("gatewayId") or it.get("agentRuntimeId") or "?"
    created = it.get("createdAt") or it.get("lastUpdatedAt") or ""
    inwin = "-"
    if created:
        t = dt.datetime.fromisoformat(str(created))
        if (after is None or t >= after) and (before is None or t <= before):
            inwin = "WINDOW"
    print(f"{inwin}\t{name}\t{ident}\t{created}")
PY

mark_window() {  # kind, json-file
  AFTER="$AFTER" BEFORE="$BEFORE" python3 "$MARKER" "$1" "$2"
}

echo "── gateways ─────────────────────────────────────────────────────────────"
aws bedrock-agentcore-control list-gateways --region "$REGION" --output json \
  > "$MARKER".gw 2>/dev/null
mark_window gateway "$MARKER".gw | while IFS=$'\t' read -r w n i c; do
  printf "  %-7s %-34s %-46s %s\n" "$w" "$n" "$i" "$c"
done

echo
echo "── agent runtimes ───────────────────────────────────────────────────────"
aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" --output json \
  > "$MARKER".rt 2>/dev/null
mark_window runtime "$MARKER".rt | while IFS=$'\t' read -r w n i c; do
  printf "  %-7s %-36s %-40s %s\n" "$w" "$n" "$i" "$c"
done

if [[ $APPLY -eq 0 ]]; then
  cat <<'NOTE'

Nothing was deleted. Rows marked WINDOW fall inside the time range you gave — that is
a hint, not a verdict: an unrelated project's production resources can land in the same
window. Check each name before you pass it to --apply.
NOTE
  exit 0
fi

echo
echo "── deleting ─────────────────────────────────────────────────────────────"
ok=0; failed=0

if [[ -n "$GW_IDS" ]]; then
  IFS=',' read -ra IDS <<< "$GW_IDS"
  for GW in "${IDS[@]}"; do
    GW="$(echo "$GW" | tr -d '[:space:]')"
    [[ -z "$GW" ]] && continue
    echo "  gateway $GW"
    TARGETS=$(aws bedrock-agentcore-control list-gateway-targets --region "$REGION" \
                --gateway-identifier "$GW" --query 'items[].targetId' --output text 2>/dev/null)
    for T in $TARGETS; do
      aws bedrock-agentcore-control delete-gateway-target --region "$REGION" \
        --gateway-identifier "$GW" --target-id "$T" >/dev/null 2>&1 \
        && echo "    target $T deleted" || echo "    target $T FAILED"
    done
    [[ -n "$TARGETS" ]] && sleep 5   # targets go asynchronously
    aws bedrock-agentcore-control delete-gateway --region "$REGION" \
      --gateway-identifier "$GW" >/dev/null 2>&1 \
      && { echo "    gateway deleted"; ok=$((ok+1)); } \
      || { echo "    gateway DELETE FAILED"; failed=$((failed+1)); }
  done
fi

if [[ -n "$RT_NAMES" ]]; then
  IFS=',' read -ra NAMES <<< "$RT_NAMES"
  for RN in "${NAMES[@]}"; do
    RN="$(echo "$RN" | tr -d '[:space:]')"
    [[ -z "$RN" ]] && continue
    RID=$(RN="$RN" RTFILE="$MARKER".rt python3 -c "
import sys, json, os
d = json.load(open(os.environ['RTFILE']))
want = os.environ['RN']
for r in d.get('agentRuntimes') or []:
    if r.get('agentRuntimeName') == want:
        print(r.get('agentRuntimeId', '')); break
")
    if [[ -z "$RID" ]]; then
      echo "  runtime $RN — not found, skipped"; continue
    fi
    echo "  runtime $RN ($RID)"
    aws bedrock-agentcore-control delete-agent-runtime --region "$REGION" \
      --agent-runtime-id "$RID" >/dev/null 2>&1 \
      && { echo "    deleted"; ok=$((ok+1)); } \
      || { echo "    DELETE FAILED"; failed=$((failed+1)); }
  done
fi

echo
echo "$ok delete(s) succeeded, $failed failed."
echo "Re-run without --apply and confirm the resources are gone; anything left is billing."
