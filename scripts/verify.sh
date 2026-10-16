#!/usr/bin/env bash
# Verify a deployed openai-image-gen server the way claude.ai will reach it.
# Usage: scripts/verify.sh https://img.example.com <MCP_SECRET>
# Exit code 0 = everything claude.ai needs is working.
set -u

BASE="${1:?usage: verify.sh <public-base-url> <mcp-secret>}"
SECRET="${2:?usage: verify.sh <public-base-url> <mcp-secret>}"
BASE="${BASE%/}"
MCP_URL="$BASE/$SECRET/mcp"
FAIL=0

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; FAIL=1; }

# 1. reachable over the public URL
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$BASE/healthz")
[ "$code" = "200" ] && pass "healthz reachable ($BASE/healthz)" || fail "healthz returned '$code' (DNS, firewall, reverse proxy or container down)"

# 2. HTTPS (claude.ai requires it)
case "$BASE" in
  https://*) pass "uses HTTPS" ;;
  *) fail "PUBLIC URL is not HTTPS; claude.ai custom connectors need HTTPS" ;;
esac

# 3. requests without the secret are rejected
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -X POST "$BASE/mcp")
[ "$code" = "404" ] && pass "request without secret rejected (404)" || fail "request without secret returned '$code', expected 404"

# 4. MCP initialize handshake with the secret
init='{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}'
resp=$(curl -s --max-time 30 -X POST "$MCP_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d "$init")
echo "$resp" | grep -q '"serverInfo"' && pass "MCP initialize OK" || fail "MCP initialize failed. Response: $(echo "$resp" | head -c 300)"

# 5. tools are listed
list='{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
resp=$(curl -s --max-time 30 -X POST "$MCP_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d "$list")
echo "$resp" | grep -q 'generate_image' && pass "tools/list includes generate_image" || fail "tools/list missing generate_image. Response: $(echo "$resp" | head -c 300)"

# 6. server-side config (API key present, model, endpoint)
call='{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"image_config","arguments":{}}}'
resp=$(curl -s --max-time 30 -X POST "$MCP_URL" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d "$call")
if echo "$resp" | grep -q 'api_key: set'; then
  pass "OPENAI_API_KEY is set on the server"
else
  fail "OPENAI_API_KEY not set (check .env)"
fi
echo "$resp" | grep -q 'download links: on' && pass "download links enabled" || echo "WARN  download links off (PUBLIC_BASE_URL not set)"

echo
if [ "$FAIL" = 0 ]; then
  echo "ALL CHECKS PASSED. Connector URL for claude.ai:"
  echo "  $MCP_URL"
else
  echo "SOME CHECKS FAILED. Fix the items above and run again."
fi
exit $FAIL
