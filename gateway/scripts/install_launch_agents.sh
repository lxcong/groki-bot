#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Install (or reinstall) the user launch agents that keep the gateway stack
# running after login on this Mac:
#   com.xiaole.gbot-login-bridge  node scripts/gbot_voice_session_bridge.mjs
#   com.xiaole.groki-gateway      scripts/run_stackchan_gateway_launchd.sh
# (cloudflared has its own agent, installed by cloudflare_tunnel_setup.sh.)
# Both agents restart on crash and write logs to ~/Library/Logs/xiaole-*.log.
# Stop a manually started gateway/bridge first, or the ports will clash.
#   scripts/install_launch_agents.sh            # install and start
#   scripts/install_launch_agents.sh uninstall  # stop and remove
set -euo pipefail

GATEWAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS_DIR="${HOME}/Library/LaunchAgents"
LOG_DIR="${HOME}/Library/Logs"
UID_NUM="$(id -u)"
PATH_VALUE="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

write_plist() {
    local label="$1" script="$2" log="$3"
    cat > "${AGENTS_DIR}/${label}.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${script}</string>
    </array>
    <key>WorkingDirectory</key><string>${GATEWAY_DIR}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>${PATH_VALUE}</string>
        <key>HOME</key><string>${HOME}</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>10</integer>
    <key>ProcessType</key><string>Interactive</string>
    <key>StandardOutPath</key><string>${log}</string>
    <key>StandardErrorPath</key><string>${log}</string>
</dict>
</plist>
PLIST
}

unload_agent() {
    local label="$1"
    launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || true
}

load_agent() {
    local label="$1"
    launchctl bootstrap "gui/${UID_NUM}" "${AGENTS_DIR}/${label}.plist"
    launchctl enable "gui/${UID_NUM}/${label}"
}

mkdir -p "${AGENTS_DIR}" "${LOG_DIR}"

if [[ "${1:-}" == "uninstall" ]]; then
    for label in com.xiaole.groki-gateway com.xiaole.gbot-login-bridge; do
        unload_agent "${label}"
        rm -f "${AGENTS_DIR}/${label}.plist"
        echo "removed ${label}"
    done
    exit 0
fi

# bridge first: the gateway retries until it is up, the other way round the
# first voice session would fail once.
unload_agent com.xiaole.gbot-login-bridge
write_plist com.xiaole.gbot-login-bridge "${GATEWAY_DIR}/scripts/run_gbot_login_bridge_launchd.sh" "${LOG_DIR}/xiaole-login-bridge.log"
load_agent com.xiaole.gbot-login-bridge

unload_agent com.xiaole.groki-gateway
write_plist com.xiaole.groki-gateway "${GATEWAY_DIR}/scripts/run_stackchan_gateway_launchd.sh" "${LOG_DIR}/xiaole-gateway.log"
load_agent com.xiaole.groki-gateway

echo "installed. status:"
launchctl print "gui/${UID_NUM}/com.xiaole.gbot-login-bridge" | grep -E "state|pid" | head -2
launchctl print "gui/${UID_NUM}/com.xiaole.groki-gateway" | grep -E "state|pid" | head -2
echo "logs: ${LOG_DIR}/xiaole-login-bridge.log  ${LOG_DIR}/xiaole-gateway.log"
