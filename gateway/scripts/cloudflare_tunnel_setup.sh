#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Publish this gateway through a Cloudflare Tunnel so the robot can reach it
# from outside the LAN. Docs: ../docs/remote-access-cloudflare.zh-CN.md
#
# One-time prerequisites (done in a browser, by you):
#   1. a domain whose DNS is hosted by Cloudflare (free plan is enough)
#   2. `cloudflared tunnel login`   -> writes ~/.cloudflared/cert.pem
#
# Then:  scripts/cloudflare_tunnel_setup.sh xiaole.example.com
#
# What it does (idempotent):
#   - creates the named tunnel "xiaole-gateway" if it does not exist
#   - writes ~/.cloudflared/config.yml: only /xiaozhi/ota and /xiaozhi/assets/
#     go to the HTTP server (8766); every other path on the hostname goes to the WebSocket
#     server (8765); photo upload and the debug pages are never routed
#   - creates the DNS CNAME <hostname> -> <tunnel>.cfargotunnel.com
#   - installs cloudflared as a launch agent (starts at login) and starts it
set -euo pipefail

HOSTNAME_ARG="${1:-}"
TUNNEL_NAME="${TUNNEL_NAME:-xiaole-gateway}"
WS_PORT="${WS_PORT:-8765}"
CAPTURE_PORT="${CAPTURE_PORT:-8766}"
CF_DIR="${HOME}/.cloudflared"
CONFIG="${CF_DIR}/config.yml"

if [[ -z "${HOSTNAME_ARG}" ]]; then
    echo "usage: $0 <public hostname, e.g. xiaole.example.com>" >&2
    exit 2
fi
if ! command -v cloudflared >/dev/null; then
    echo "cloudflared not found: brew install cloudflared" >&2
    exit 1
fi
if [[ ! -f "${CF_DIR}/cert.pem" ]]; then
    echo "No ${CF_DIR}/cert.pem. Run 'cloudflared tunnel login' first (opens a browser)." >&2
    exit 1
fi

# 1. tunnel
if ! cloudflared tunnel list --name "${TUNNEL_NAME}" --output json | grep -q '"id"'; then
    echo "Creating tunnel ${TUNNEL_NAME}"
    cloudflared tunnel create "${TUNNEL_NAME}"
fi
TUNNEL_ID="$(cloudflared tunnel list --name "${TUNNEL_NAME}" --output json | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')"
CREDS="${CF_DIR}/${TUNNEL_ID}.json"
if [[ ! -f "${CREDS}" ]]; then
    echo "Credentials file ${CREDS} missing. Delete the tunnel (cloudflared tunnel delete ${TUNNEL_NAME}) and rerun." >&2
    exit 1
fi
echo "Tunnel ${TUNNEL_NAME} = ${TUNNEL_ID}"

# 2. config
if [[ -f "${CONFIG}" ]]; then
    cp "${CONFIG}" "${CONFIG}.bak.$(date +%Y%m%d%H%M%S)"
fi
cat > "${CONFIG}" <<YAML
# Written by gateway/scripts/cloudflare_tunnel_setup.sh
tunnel: ${TUNNEL_ID}
credentials-file: ${CREDS}

ingress:
  # OTA check: the robot POSTs here on boot and learns the wss:// address.
  - hostname: ${HOSTNAME_ARG}
    path: ^/xiaozhi/ota/?$
    service: http://127.0.0.1:${CAPTURE_PORT}
  # Avatar assets images (hash-named, no secret) served by the OTA server.
  - hostname: ${HOSTNAME_ARG}
    path: ^/xiaozhi/assets/
    service: http://127.0.0.1:${CAPTURE_PORT}
  # Everything else on the hostname is the robot's WebSocket (audio + MCP).
  - hostname: ${HOSTNAME_ARG}
    service: http://127.0.0.1:${WS_PORT}
    originRequest:
      connectTimeout: 10s
  # /capture, /debug/* and any other hostname never leave the LAN.
  - service: http_status:404
YAML
cloudflared tunnel ingress validate
echo "Routing check:"
cloudflared tunnel ingress rule "https://${HOSTNAME_ARG}/xiaozhi/ota/"
cloudflared tunnel ingress rule "https://${HOSTNAME_ARG}/xiaozhi/assets/abc.bin"
cloudflared tunnel ingress rule "https://${HOSTNAME_ARG}/"
cloudflared tunnel ingress rule "https://${HOSTNAME_ARG}/debug/status"

# 3. DNS
cloudflared tunnel route dns --overwrite-dns "${TUNNEL_NAME}" "${HOSTNAME_ARG}"

# 4. launch agent (runs at login, same account as the gateway)
PLIST="${HOME}/Library/LaunchAgents/com.cloudflare.cloudflared.plist"
if [[ -f "${PLIST}" ]]; then
    launchctl unload "${PLIST}" 2>/dev/null || true
    cloudflared service uninstall 2>/dev/null || true
fi
cloudflared service install
if ! grep -q "<string>run</string>" "${PLIST}"; then
    # some cloudflared builds write a plist without the 'tunnel run' arguments
    python3 - "${PLIST}" <<'PY'
import plistlib, sys
path = sys.argv[1]
with open(path, "rb") as f:
    plist = plistlib.load(f)
args = plist.get("ProgramArguments", [])
if "run" not in args:
    plist["ProgramArguments"] = [args[0], "tunnel", "run"]
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    print("patched ProgramArguments ->", plist["ProgramArguments"])
PY
    launchctl unload "${PLIST}" 2>/dev/null || true
    launchctl load "${PLIST}"
fi

echo
echo "Done. Next:"
echo "  - gateway/.env: STACKCHAN_OTA_WS_URL=wss://${HOSTNAME_ARG}/  then restart the gateway"
echo "  - firmware CONFIG_OTA_URL=\"https://${HOSTNAME_ARG}/xiaozhi/ota/\"  then rebuild and flash"
echo "  - check: cloudflared tunnel info ${TUNNEL_NAME}; curl -sS -X POST https://${HOSTNAME_ARG}/xiaozhi/ota/"
echo "  - logs: ~/Library/Logs/com.cloudflare.cloudflared.err.log"
