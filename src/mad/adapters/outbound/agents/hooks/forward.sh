#!/bin/bash
set -uo pipefail

INPUT=$(cat)

# Skip if required env vars are absent
if [ -z "${MAD_HOOK_SOCKET:-}" ] || [ -z "${MAD_SESSION_ID:-}" ]; then
  exit 0
fi

EVENT=$(jq -r '.hook_event_name // "Unknown"' <<<"$INPUT")

BODY=$(jq -n \
  --arg sid "$MAD_SESSION_ID" \
  --arg type "agent.${MAD_PROVIDER:-unknown}.hook.${EVENT}" \
  --argjson data "$INPUT" \
  '{session_id:$sid, type:$type, data:$data}')

curl --silent --max-time 5 \
  --unix-socket "$MAD_HOOK_SOCKET" \
  -H 'Content-Type: application/json' \
  -d "$BODY" \
  http://mad/_internal/hooks >/dev/null 2>&1 || true

exit 0
