#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Run the Grok Bot login bridge as a background service (launchd).
# The bridge does not read .env itself, so this wrapper exports the gateway's
# .env first (STACKCHAN_GBOT_VOICE_BOT_ID, STACKCHAN_TOOL_BOT, ...).
# It must run inside the logged-in user's session: it reads the
# "Grok Bot Safe Storage" item from the login keychain.
set -euo pipefail

GATEWAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_BIN="${NODE_BIN:-$(command -v node || echo /opt/homebrew/bin/node)}"

cd "${GATEWAY_DIR}"
if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi
exec "${NODE_BIN}" scripts/gbot_voice_session_bridge.mjs
