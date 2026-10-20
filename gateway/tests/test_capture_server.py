"""Tests for HTTP capture upload helpers."""

import json

import pytest

from stackchan_mcp.capture_server import (
    CAPTURE_TOKEN_KEY,
    INJECT_TEXT_HANDLER_KEY,
    _is_authorized,
    create_capture_app,
    handle_inject_text,
)


def test_capture_app_stores_capture_token():
    """Capture app keeps the expected bearer token in app state."""
    app = create_capture_app(capture_token="capture-token")

    assert app[CAPTURE_TOKEN_KEY] == "capture-token"
    assert app[INJECT_TEXT_HANDLER_KEY] is None


def test_is_authorized_accepts_matching_bearer():
    """Bearer auth must match exactly."""
    assert _is_authorized("Bearer capture-token", "capture-token") is True


def test_is_authorized_rejects_missing_or_wrong_bearer():
    """Missing or mismatched bearer auth is rejected."""
    assert _is_authorized("", "capture-token") is False
    assert _is_authorized("Bearer wrong-token", "capture-token") is False


class FakeJsonRequest:
    def __init__(self, app, body, *, token: str = "") -> None:
        self.app = app
        self._body = body
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_inject_text_calls_handler_and_reports_active_session():
    """Debug text injection becomes a plain user turn through the wired handler."""
    seen: list[str] = []

    async def handler(text: str) -> bool:
        seen.append(text)
        return True

    app = create_capture_app(inject_text_handler=handler)
    response = await handle_inject_text(FakeJsonRequest(app, {"text": "放一首歌"}))

    assert response.status == 200
    assert '"active_session": true' in response.text
    assert seen == ["放一首歌"]


@pytest.mark.asyncio
async def test_inject_text_reports_no_active_session_without_handler():
    app = create_capture_app()
    response = await handle_inject_text(FakeJsonRequest(app, {"text": "测试"}))

    assert response.status == 200
    assert '"active_session": false' in response.text


@pytest.mark.asyncio
async def test_inject_text_reuses_capture_bearer_token():
    app = create_capture_app(capture_token="secret")

    response = await handle_inject_text(FakeJsonRequest(app, {"text": "测试"}))

    assert response.status == 401


class FakeOtaRequest:
    """Enough of aiohttp's Request for handle_xiaozhi_ota."""

    def __init__(self, app, *, method="POST", body=None, host="192.168.10.20:8766", headers=None):
        self.app = app
        self.method = method
        self._body = body
        self.host = host
        self.headers = headers or {}
        self.can_read_body = body is not None

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.mark.asyncio
async def test_xiaozhi_ota_points_device_at_this_gateway():
    """The reply carries only a websocket section, built from the host the device called."""
    from stackchan_mcp.capture_server import handle_xiaozhi_ota

    app = create_capture_app(ota_ws_port=8765, ota_token="secret")
    request = FakeOtaRequest(
        app,
        body={"application": {"version": "2.5.3"}, "board": {"type": "esp32-s3-touch-amoled-1.75c-xiaole"}},
        headers={"Device-Id": "7c:0c:5f:42:41:6c"},
    )
    response = await handle_xiaozhi_ota(request)

    assert response.status == 200
    payload = json.loads(response.text)
    assert payload["websocket"] == {"url": "ws://192.168.10.20:8765/", "token": "secret"}
    assert payload["firmware"] == {"version": "2.5.3", "url": ""}
    assert "mqtt" not in payload and "activation" not in payload
    assert payload["server_time"]["timestamp"] > 1_700_000_000_000
    assert isinstance(payload["server_time"]["timezone_offset"], int)


@pytest.mark.asyncio
async def test_xiaozhi_ota_get_without_body_and_url_override():
    """GET (no body) still answers; STACKCHAN_OTA_WS_URL wins over the derived URL; no token key when unset."""
    from stackchan_mcp.capture_server import handle_xiaozhi_ota

    app = create_capture_app(ota_ws_url="ws://robot-pc.local:9000/")
    response = await handle_xiaozhi_ota(FakeOtaRequest(app, method="GET"))

    payload = json.loads(response.text)
    assert payload["websocket"] == {"url": "ws://robot-pc.local:9000/"}
    assert payload["firmware"]["version"] == "0.0.0"


@pytest.mark.asyncio
async def test_xiaozhi_ota_tolerates_bad_json_and_ipv6_host():
    from stackchan_mcp.capture_server import handle_xiaozhi_ota

    app = create_capture_app(ota_ws_port=8765)
    bad = FakeOtaRequest(app, body=json.JSONDecodeError("x", "y", 0), host="[fe80::1]:8766")
    payload = json.loads((await handle_xiaozhi_ota(bad)).text)
    assert payload["websocket"]["url"] == "ws://[fe80::1]:8765/"
    assert payload["firmware"]["version"] == "0.0.0"


def test_build_ota_response_is_deterministic_for_a_fixed_clock():
    from stackchan_mcp.capture_server import build_ota_response

    payload = build_ota_response(device_version="2.5.3", websocket_url="ws://h:1/", now=1_760_000_000.5)
    assert payload["server_time"]["timestamp"] == 1_760_000_000_500
    assert payload["websocket"] == {"url": "ws://h:1/"}
