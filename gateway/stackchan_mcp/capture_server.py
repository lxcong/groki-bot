"""HTTP capture server for receiving photos from ESP32.

ESP32's camera.Explain() POSTs multipart/form-data with:
- field 'question' (text)
- field 'file' (camera.jpg, JPEG image)

This server saves the JPEG and returns the file path so MCP client
can view the image via the Read tool.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import time
from pathlib import Path
from collections.abc import Awaitable, Callable

from aiohttp import web

from .debug_panel import PANEL_HTML
from .debug_status import DebugStatus, get_debug_status

logger = logging.getLogger(__name__)

CAPTURE_DIR = os.path.expanduser("~/.stackchan/captures")
CAPTURE_TOKEN_KEY = web.AppKey("capture_token", str)
InjectTextHandler = Callable[[str], bool | Awaitable[bool]]
INJECT_TEXT_HANDLER_KEY = web.AppKey(
    "inject_text_handler",
    object,
)
DEBUG_STATUS_KEY = web.AppKey("debug_status", DebugStatus)
OTA_WS_PORT_KEY = web.AppKey("ota_ws_port", int)
OTA_WS_URL_KEY = web.AppKey("ota_ws_url", str)
OTA_TOKEN_KEY = web.AppKey("ota_token", str)
OTA_DEVICE_IDS_KEY = web.AppKey("ota_device_ids", frozenset)
AVATAR_DIR_KEY = web.AppKey("avatar_dir", str)

# Paths a request that arrived through the public tunnel may reach. Everything
# else on this server (photo upload, debug status, text injection) is LAN only.
TUNNEL_ALLOWED_PATHS = frozenset({"/xiaozhi/ota", "/xiaozhi/ota/"})
# Assets images (speech models + avatar pack) are named by their hash and carry
# no secret, so the device may fetch them from outside too.
TUNNEL_ALLOWED_PREFIXES = ("/xiaozhi/assets/", "/xiaozhi/avatars/")
ASSET_FILE_NAME = re.compile(r"^[0-9a-f]{16,64}\.bin$")
DESIGN_ID = re.compile(r"^[0-9a-f]{16}$")

# Where the device last reached us (scheme + host[:port]); the avatar tool builds
# the download URL it hands the device from this.
_last_ota_origin: dict[str, str] = {}


def last_ota_origin() -> str | None:
    return _last_ota_origin.get("origin")


def remember_ota_origin(request: web.Request) -> None:
    host = request.host or ""
    if not host:
        return
    # Cloudflare adds CF-Connecting-IP to every request it forwards (see is_tunnel_request).
    via_tunnel = bool(request.headers.get("CF-Connecting-IP") or request.headers.get("Cf-Connecting-Ip"))
    _last_ota_origin["origin"] = f"{'https' if via_tunnel else 'http'}://{host}"


def parse_device_ids(value: str) -> frozenset[str]:
    """Normalise a comma/space separated list of device MACs (lower case)."""
    return frozenset(
        item.strip().lower() for item in value.replace(";", ",").replace(" ", ",").split(",") if item.strip()
    )


def is_tunnel_request(headers) -> bool:
    """Whether the request came in through Cloudflare Tunnel.

    cloudflared connects from loopback, so the peer address says nothing;
    Cloudflare always adds ``CF-Connecting-IP`` with the real client address.
    """
    return bool(headers.get("CF-Connecting-IP") or headers.get("Cf-Connecting-Ip"))


def device_allowed(headers, allowed: frozenset[str]) -> bool:
    """True when no allowlist is set or the Device-Id header is in it."""
    if not allowed:
        return True
    return headers.get("Device-Id", "").strip().lower() in allowed


@web.middleware
async def tunnel_guard(request: web.Request, handler):
    """Hide every non-OTA endpoint from requests that arrived via the tunnel."""
    if is_tunnel_request(request.headers) and request.path not in TUNNEL_ALLOWED_PATHS and not request.path.startswith(TUNNEL_ALLOWED_PREFIXES):
        logger.warning(
            "tunnel request to %s from %s refused",
            request.path,
            request.headers.get("CF-Connecting-IP", "?"),
        )
        return web.json_response({"error": "Not found"}, status=404)
    return await handler(request)


def _is_authorized(auth_header: str, expected_token: str) -> bool:
    """Return whether the bearer auth header matches the expected token."""
    return auth_header == f"Bearer {expected_token}"


async def handle_capture(request: web.Request) -> web.Response:
    """Handle photo upload from ESP32."""
    expected_token = request.app[CAPTURE_TOKEN_KEY]
    if expected_token and not _is_authorized(
        request.headers.get("Authorization", ""), expected_token
    ):
        logger.warning("Capture upload auth rejected")
        return web.Response(
            text='{"error": "Unauthorized"}',
            status=401,
            content_type="application/json",
        )

    os.makedirs(CAPTURE_DIR, exist_ok=True)

    reader = await request.multipart()
    question = ""
    image_path = ""

    async for part in reader:
        if part.name == "question":
            question = (await part.read()).decode("utf-8")
        elif part.name == "file":
            timestamp = int(time.time() * 1000)
            filename = f"capture_{timestamp}.jpg"
            image_path = os.path.join(CAPTURE_DIR, filename)
            with open(image_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(8192)
                    if not chunk:
                        break
                    f.write(chunk)

    if image_path and os.path.exists(image_path):
        file_size = os.path.getsize(image_path)
        logger.info(
            "Captured photo: %s (%d bytes), question: %s",
            image_path,
            file_size,
            question,
        )
        result = json.dumps({
            "image_path": image_path,
            "size_bytes": file_size,
            "question": question,
        })
        return web.Response(text=result, content_type="application/json")

    return web.Response(
        text='{"error": "No image received"}',
        status=400,
        content_type="application/json",
    )


async def handle_inject_text(request: web.Request) -> web.Response:
    """Inject a plain text user turn into the active Gemini Live session."""
    expected_token = request.app[CAPTURE_TOKEN_KEY]
    if expected_token and not _is_authorized(
        request.headers.get("Authorization", ""), expected_token
    ):
        logger.warning("Debug text injection auth rejected")
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    text = str(body.get("text", "")).strip() if isinstance(body, dict) else ""
    if not text:
        return web.json_response({"ok": False, "error": "text is required"}, status=400)

    handler = request.app[INJECT_TEXT_HANDLER_KEY]
    active_session = False
    if handler is not None:
        try:
            result = handler(text)
            if inspect.isawaitable(result):
                result = await result
            active_session = bool(result)
        except Exception as exc:
            logger.exception("Debug text injection failed")
            return web.json_response(
                {"ok": False, "active_session": False, "error": str(exc)}
            )

    return web.json_response({"ok": True, "active_session": active_session})


async def handle_debug_status(request: web.Request) -> web.Response:
    """Return the gateway status snapshot as JSON (read-only, no auth)."""
    return web.json_response(request.app[DEBUG_STATUS_KEY].snapshot())


async def handle_debug_panel(request: web.Request) -> web.Response:
    """Return the zero-dependency self-refreshing status panel."""
    return web.Response(text=PANEL_HTML, content_type="text/html")


def _ota_host(request: web.Request) -> str:
    """The address the device used to reach us, without the port."""
    host = request.host or ""
    if host.startswith("["):  # IPv6 literal
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def build_ota_response(
    *,
    device_version: str | None,
    websocket_url: str,
    token: str = "",
    now: float | None = None,
) -> dict:
    """The OTA check reply an upstream xiaozhi-esp32 firmware expects.

    The firmware chooses MQTT whenever the reply has an ``mqtt`` section, so
    only ``websocket`` is sent. Echoing the device's own version as the
    latest firmware keeps it on the ``Current is the latest version`` path
    without ever offering an upgrade.
    """
    stamp = time.time() if now is None else now
    local = time.localtime(stamp)
    websocket: dict[str, str] = {"url": websocket_url}
    if token:
        websocket["token"] = token
    return {
        "server_time": {
            "timestamp": int(stamp * 1000),
            "timezone_offset": int(local.tm_gmtoff // 60),
        },
        "firmware": {"version": device_version or "0.0.0", "url": ""},
        "websocket": websocket,
    }


async def handle_xiaozhi_ota(request: web.Request) -> web.Response:
    """``/xiaozhi/ota/`` for an upstream xiaozhi-esp32 firmware.

    The firmware POSTs its system info (``application.version`` among others)
    on every boot and reads the WebSocket server from the reply, so pointing
    its OTA URL at this gateway is enough to move it off the cloud server.
    """
    device_version: str | None = None
    if request.method == "POST" and request.can_read_body:
        try:
            body = await request.json()
            application = body.get("application") if isinstance(body, dict) else None
            if isinstance(application, dict) and isinstance(application.get("version"), str):
                device_version = application["version"]
        except (json.JSONDecodeError, UnicodeDecodeError):
            device_version = None
    if not device_allowed(request.headers, request.app.get(OTA_DEVICE_IDS_KEY, frozenset())):
        logger.warning(
            "xiaozhi OTA check from unknown device=%s refused",
            request.headers.get("Device-Id", "?"),
        )
        return web.json_response({"error": "Forbidden"}, status=403)
    remember_ota_origin(request)
    websocket_url = request.app.get(OTA_WS_URL_KEY) or (
        f"ws://{_ota_host(request)}:{request.app.get(OTA_WS_PORT_KEY, 8765)}/"
    )
    # The shared secret is handed out on the LAN only. A device that was
    # provisioned at home keeps the token in NVS; a reply without ``token``
    # leaves that stored value untouched (ota.cc only writes keys it receives).
    via_tunnel = is_tunnel_request(request.headers)
    payload = build_ota_response(
        device_version=device_version,
        websocket_url=websocket_url,
        token="" if via_tunnel else request.app.get(OTA_TOKEN_KEY, ""),
    )
    logger.info(
        "xiaozhi OTA check from device=%s client=%s version=%s via=%s -> websocket %s",
        request.headers.get("Device-Id", "?"),
        request.headers.get("Client-Id", "?"),
        device_version or "?",
        "tunnel" if via_tunnel else "lan",
        websocket_url,
    )
    return web.json_response(payload)


async def handle_xiaozhi_assets(request: web.Request) -> web.StreamResponse:
    """``/xiaozhi/assets/<sha>.bin``: an assets image built by the avatar tool.

    The firmware's Assets::Download needs a Content-Length and a 200; aiohttp's
    FileResponse provides both. Names are hashes, so there is nothing to guess
    and no path to traverse.
    """
    name = request.match_info.get("name", "")
    root = request.app.get(AVATAR_DIR_KEY, "")
    if not root or not ASSET_FILE_NAME.match(name):
        return web.json_response({"error": "Not found"}, status=404)
    path = Path(root) / "images" / name
    is_pack = False
    if not path.is_file() and DESIGN_ID.match(name[:-4]):
        path = Path(root) / name[:-4] / "avatar.bin"  # a design's pack, for the live preview
        is_pack = True
    if not path.is_file():
        return web.json_response({"error": "Not found"}, status=404)
    logger.info("assets download %s (%d bytes) by device=%s", name, path.stat().st_size, request.headers.get("Device-Id", "?"))
    if is_pack:
        # Small (~86 KB) and fetched while the device streams audio: send it as one
        # in-memory body with an explicit close, not through the sendfile path.
        return web.Response(body=path.read_bytes(),
                            headers={"Content-Type": "application/octet-stream", "Connection": "close", "Cache-Control": "no-store"})
    return web.FileResponse(path, headers={"Content-Type": "application/octet-stream"})


async def handle_xiaozhi_avatar_sheet(request: web.Request) -> web.StreamResponse:
    """``/xiaozhi/avatars/<id>.png``: the contact sheet of a design, for a phone or browser."""
    name = request.match_info.get("name", "")
    root = request.app.get(AVATAR_DIR_KEY, "")
    if not root or not name.endswith(".png") or not DESIGN_ID.match(name[:-4]):
        return web.json_response({"error": "Not found"}, status=404)
    path = Path(root) / name[:-4] / "contact-sheet.png"
    if not path.is_file():
        return web.json_response({"error": "Not found"}, status=404)
    return web.FileResponse(path, headers={"Content-Type": "image/png", "Cache-Control": "no-store"})


def create_capture_app(
    capture_token: str = "",
    *,
    inject_text_handler: InjectTextHandler | None = None,
    debug_status: DebugStatus | None = None,
    ota_ws_port: int = 8765,
    ota_ws_url: str = "",
    ota_token: str = "",
    device_ids: frozenset[str] | None = None,
    avatar_dir: str = "",
) -> web.Application:
    """Create the HTTP capture application."""
    app = web.Application(middlewares=[tunnel_guard])
    app[OTA_DEVICE_IDS_KEY] = frozenset(device_ids or ())
    app[AVATAR_DIR_KEY] = avatar_dir
    app[CAPTURE_TOKEN_KEY] = capture_token
    app[INJECT_TEXT_HANDLER_KEY] = inject_text_handler
    app[DEBUG_STATUS_KEY] = debug_status or get_debug_status()
    app[OTA_WS_PORT_KEY] = ota_ws_port
    app[OTA_WS_URL_KEY] = ota_ws_url
    app[OTA_TOKEN_KEY] = ota_token
    app.router.add_post("/capture", handle_capture)
    app.router.add_post("/debug/inject-text", handle_inject_text)
    app.router.add_get("/debug/status", handle_debug_status)
    app.router.add_get("/debug/panel", handle_debug_panel)
    for path in ("/xiaozhi/ota/", "/xiaozhi/ota"):
        app.router.add_get(path, handle_xiaozhi_ota)
        app.router.add_post(path, handle_xiaozhi_ota)
    app.router.add_get("/xiaozhi/assets/{name}", handle_xiaozhi_assets)
    app.router.add_get("/xiaozhi/avatars/{name}", handle_xiaozhi_avatar_sheet)
    return app
