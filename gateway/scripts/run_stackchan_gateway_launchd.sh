#!/usr/bin/env bash
# Run the gateway as a background service (launchd, systemd, tmux, ...).
# The gateway is a stdio MCP server and exits when stdin reaches EOF, so this
# wrapper keeps stdin open with a FIFO that never receives data.
# Reads .env from the gateway directory. Override the uv binary with UV_BIN.
set -euo pipefail

GATEWAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="${UV_BIN:-$(command -v uv || echo /opt/homebrew/bin/uv)}"
RUNTIME_DIR=""
STDIN_FIFO=""
KEEPER_PID=""
GATEWAY_PID=""

cleanup() {
    local status=$?
    trap - EXIT INT TERM

    if [[ -n "${GATEWAY_PID}" ]] && kill -0 "${GATEWAY_PID}" 2>/dev/null; then
        pkill -TERM -P "${GATEWAY_PID}" 2>/dev/null || true
        kill "${GATEWAY_PID}" 2>/dev/null || true
        wait "${GATEWAY_PID}" 2>/dev/null || true
    fi

    if [[ -n "${KEEPER_PID}" ]] && kill -0 "${KEEPER_PID}" 2>/dev/null; then
        pkill -TERM -P "${KEEPER_PID}" 2>/dev/null || true
        kill "${KEEPER_PID}" 2>/dev/null || true
        wait "${KEEPER_PID}" 2>/dev/null || true
    fi

    if [[ -n "${RUNTIME_DIR}" ]]; then
        rm -rf "${RUNTIME_DIR}"
    fi

    exit "${status}"
}
trap cleanup EXIT INT TERM

cd "${GATEWAY_DIR}"

RUNTIME_DIR="$(mktemp -d "${TMPDIR:-/tmp}/stackchan-gateway.XXXXXX")"
STDIN_FIFO="${RUNTIME_DIR}/stdin"
mkfifo "${STDIN_FIFO}"

# Open the FIFO's write end without writing: the reader never gets EOF, and the gateway still behaves as if there is no interactive input.
(
    sleep_pid=""
    trap 'if [[ -n "${sleep_pid}" ]]; then kill "${sleep_pid}" 2>/dev/null || true; fi; exit 0' TERM INT
    exec 3>"${STDIN_FIFO}"
    while true; do
        sleep 3600 &
        sleep_pid=$!
        wait "${sleep_pid}" || true
    done
) &
KEEPER_PID=$!

"${UV_BIN}" run \
    --all-extras \
    --with python-socks \
    --with 'httpx[socks]' \
    python -m stackchan_mcp <"${STDIN_FIFO}" &
GATEWAY_PID=$!

gateway_status=0
wait "${GATEWAY_PID}" || gateway_status=$?
exit "${gateway_status}"
