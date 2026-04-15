#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=".env"

echo "============================================"
echo "  Claude Code Token Setup"
echo "============================================"
echo ""
echo "To generate a token, run this on your Mac"
echo "(where you're already logged into Claude):"
echo ""
echo "  claude setup-token"
echo ""
echo "It will print a token starting with 'sk-ant-oaut01-...'"
echo "Copy that token and paste it below."
echo ""

read -rp "Paste your CLAUDE_CODE_OAUTH_TOKEN: " token

if [ -z "$token" ]; then
  echo "Error: No token provided. Exiting."
  exit 1
fi

# Create or update .env file
if [ -f "$ENV_FILE" ]; then
  # Replace existing token line or append
  if grep -q "^CLAUDE_CODE_OAUTH_TOKEN=" "$ENV_FILE"; then
    sed -i.bak "s|^CLAUDE_CODE_OAUTH_TOKEN=.*|CLAUDE_CODE_OAUTH_TOKEN=${token}|" "$ENV_FILE"
    rm -f "${ENV_FILE}.bak"
  else
    echo "CLAUDE_CODE_OAUTH_TOKEN=${token}" >> "$ENV_FILE"
  fi
else
  cp .env.example "$ENV_FILE"
  sed -i.bak "s|^CLAUDE_CODE_OAUTH_TOKEN=.*|CLAUDE_CODE_OAUTH_TOKEN=${token}|" "$ENV_FILE"
  rm -f "${ENV_FILE}.bak"
fi

echo ""
echo "Token saved to ${ENV_FILE}"
echo "All containers will use this token on next start."
