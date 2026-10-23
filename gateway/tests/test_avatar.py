# SPDX-License-Identifier: MIT
"""A new look from a conversation: generator contract, assets image, voice tools, download route."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from stackchan_mcp import avatar as avatar_mod
from stackchan_mcp import capture_server
from stackchan_mcp import persona as persona_mod
from stackchan_mcp.voice_tools import VoiceToolExecutor

pytestmark = pytest.mark.skipif(not avatar_mod.generator_path().is_file(), reason="assets/xiaole/generate.py not in this workspace")


@pytest.fixture
def store(monkeypatch, tmp_path):
    path = tmp_path / "persona.json"
    monkeypatch.setenv("STACKCHAN_PERSONA_FILE", str(path))
    monkeypatch.setattr(persona_mod, "_default_store", None)
    return persona_mod.default_store()


@pytest.fixture(autouse=True)
def device_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("STACKCHAN_GEMINI_DEVICE_TOOLS", "1")
    monkeypatch.setenv("STACKCHAN_MAC_CONTROL", "0")
    monkeypatch.setenv("STACKCHAN_ASK_CLAUDE", "0")
    monkeypatch.delenv("STACKCHAN_TOOL_BOT", raising=False)
    monkeypatch.delenv("STACKCHAN_ASSETS_BASE_URL", raising=False)
    monkeypatch.setenv("STACKCHAN_AVATAR_DIR", str(tmp_path / "avatars"))
    capture_server._last_ota_origin.clear()


def _device_text(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}]}


def _fake_base_image(builder, path, wake):
    """A tiny assets image with the index.json shape package_firmware.py produces."""
    index = {"version": 1, "srmodels": "srmodels.bin", "text_font": "font.bin", "multinet_model": wake}
    files = {
        "index.json": (json.dumps(index).encode(), 0, 0),
        "srmodels.bin": (b"\x01\x00\x00\x00fake-models", 0, 0),
        "font.bin": (b"font", 0, 0),
    }
    path.write_bytes(builder.pack_assets_entries(files, 32))


# ---- generator contract ------------------------------------------------------------

def test_generator_renders_every_species_inside_the_contract():
    gen = avatar_mod.load_generator()
    for species in gen.SPECIES:
        rendered = gen.render_spec({"species": species, "accessories": [{"type": "hat", "style": "crown"}, {"type": "glasses"}]})
        assert len(rendered.frames) == 21 and all(len(f) == 64 * 64 for f in rendered.frames)
        data = gen.pack_bytes(rendered)
        parsed = gen.read_pack(data)
        assert parsed["id"] == rendered.id and parsed["layout"]["mouth_w"] == 10 and parsed["size"] == len(data)
        assert parsed["frames"] == rendered.frames and parsed["mouth"] == rendered.mouth


def test_default_spec_is_the_builtin_rabbit():
    gen = avatar_mod.load_generator()
    spec = gen.normalize_spec({})
    assert spec["species"] == "rabbit" and spec["accessories"] == [{"type": "scarf", "color": "#a8e4cc"}]
    assert gen.spec_id(spec) == gen.spec_id(gen.normalize_spec(dict(gen.DEFAULT_SPEC)))


def test_spec_normalisation_is_lenient_and_speaks_on_errors():
    gen = avatar_mod.load_generator()
    spec = gen.normalize_spec({"species": "Cat", "body_color": "橘色", "accessories": ["glasses", {"type": "hat", "color": "红"}]})
    assert spec["body_color"] == "#f4a35a" and spec["ear_shape"] == "pointed"
    assert [a["type"] for a in spec["accessories"]] == ["glasses", "hat"] and spec["accessories"][1]["style"] == "beanie"
    assert spec["outline_color"] != spec["body_color"]
    with pytest.raises(gen.SpecError) as info:
        gen.normalize_spec({"species": "dragon"})
    assert info.value.spoken
    with pytest.raises(gen.SpecError):
        gen.normalize_spec({"body_color": "not a colour"})
    look = gen.describe_spec(spec)
    assert look.startswith("一只橘色的小猫") and "#" not in look and "红色的毛线帽" in look
    assert avatar_mod.appearance_text(spec, look).startswith("一只住在主人桌上的像素橘色的小猫：")
    assert gen.color_word("#000000") == "黑色" and gen.color_word("#4a90d9") == "蓝色" and gen.color_word("#fff0d5") == "米白色"


# ---- designer ----------------------------------------------------------------------

def test_designer_writes_pack_and_contact_sheet(tmp_path):
    designer = avatar_mod.AvatarDesigner(root=tmp_path / "avatars")
    pending = designer.design({"species": "fox", "name": "狐狐"}, "一只小狐狸")
    folder = tmp_path / "avatars" / pending.id
    assert (folder / "avatar.bin").read_bytes() == pending.pack
    assert (folder / "contact-sheet.png").read_bytes().startswith(b"\x89PNG")
    assert json.loads((folder / "spec.json").read_text())["name"] == "狐狐"
    assert pending.look.startswith("一只橘色的小狐狸")


def test_assets_image_keeps_models_and_wake_word_and_adds_the_pack(tmp_path):
    builder = avatar_mod.load_assets_builder()
    wake = {"language": "cn", "duration": 3000, "threshold": 0.2, "commands": [{"command": "a fu", "text": "阿福", "action": "wake"}]}
    base = tmp_path / "base.bin"
    _fake_base_image(builder, base, wake)
    designer = avatar_mod.AvatarDesigner(root=tmp_path / "avatars", base_image=base, models_dir=tmp_path / "no-models")
    pending = designer.design({"species": "bear"})
    build = designer.build(pending)
    assert build.image.is_file() and build.image.parent.name == "images" and build.image.name.endswith(".bin")
    entries = builder.read_assets_entries(str(build.image), 32)
    index = json.loads(entries["index.json"][0])
    assert index["avatar_pack"] == "avatar.bin" and entries["avatar.bin"][0] == pending.pack
    assert index["multinet_model"] == wake and entries["srmodels.bin"][0].endswith(b"fake-models") and "font.bin" in entries
    assert build.image_size == build.image.stat().st_size < avatar_mod.ASSETS_PARTITION_SIZE
    # prune keeps the current image only
    other = build.image.parent / ("0" * 32 + ".bin")
    other.write_bytes(b"x")
    designer.prune({build.file_name})
    assert build.image.exists() and not other.exists()


@pytest.mark.skipif(not (avatar_mod.multinet_model_dir() / "fst").is_dir() or not avatar_mod.base_image_path().is_file(),
                    reason="MultiNet models or the packaged assets image are not in this workspace")
def test_assets_image_with_the_real_models_fits_the_partition(tmp_path):
    builder = avatar_mod.load_assets_builder()
    designer = avatar_mod.AvatarDesigner(root=tmp_path / "avatars")
    build = designer.build(designer.design({"species": "cat"}))
    entries = builder.read_assets_entries(str(build.image), 32)
    index = json.loads(entries["index.json"][0])
    base_index = json.loads(builder.read_assets_entries(str(avatar_mod.base_image_path()), 32)["index.json"][0])
    assert index["multinet_model"] == base_index["multinet_model"] and index["text_font"] == base_index["text_font"]
    assert b"mn6_cn" in entries["srmodels.bin"][0] and b"fst" in entries["srmodels.bin"][0]
    assert build.image_size <= avatar_mod.ASSETS_PARTITION_SIZE


# ---- persona -----------------------------------------------------------------------

def test_persona_records_avatar_and_announces_once(store):
    saved = store.set_avatar("一只住在主人桌上的像素小猫：橘色", {"species": "cat"}, {"id": "abc", "image": "x.bin"})
    assert saved.avatar_announce and saved.avatar_spec == {"species": "cat"} and saved.appearance.endswith("橘色")
    again = persona_mod.PersonaStore(store.path).load()
    assert again.avatar_pack == {"id": "abc", "image": "x.bin"}
    assert store.take_avatar_announcement() == saved.appearance
    assert store.take_avatar_announcement() == ""
    assert not store.load().avatar_announce and store.load().avatar_spec == {"species": "cat"}
    assert store.load().render("你是{{name}}，{{appearance}}。").endswith("橘色。")


# ---- voice tools -------------------------------------------------------------------

class _StubDesigner:
    def __init__(self, root, build=None, fail=None):
        self.root = root
        self.built = []
        self._build = build
        self._fail = fail
        self.pruned = None

    def design(self, spec, description):
        return avatar_mod.AvatarDesigner(root=self.root).design(spec, description)

    def build(self, pending):
        if self._fail:
            raise self._fail
        self.built.append(pending)
        image = self.root / "images" / ("ab" * 16 + ".bin")
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"image")
        return avatar_mod.AvatarBuild(pending.id, "橘橘", pending.look, pending.spec, "p" * 64, image, "i" * 64, 5)

    def prune(self, keep):
        self.pruned = keep


@pytest.mark.asyncio
async def test_design_rounds_preview_on_device_then_apply(store, tmp_path, monkeypatch):
    device = SimpleNamespace(
        device_connected=True,
        known_tool_names={"self.avatar.update", "self.avatar.preview", "self.avatar.preview_end", "self.avatar.get"},
        call_tool=AsyncMock(return_value=(_device_text({"ok": True}), None)),
    )
    executor = VoiceToolExecutor(device, mac_controller=SimpleNamespace())
    stub = _StubDesigner(tmp_path / "avatars")
    executor._avatar_designer = stub
    capture_server._last_ota_origin["origin"] = "http://192.168.10.58:8766"
    try:
        names = {d["name"] for d in executor.declarations()}
        assert {"design_avatar", "apply_avatar", "discard_avatar_draft"} <= names
        design = next(d for d in executor.declarations() if d["name"] == "design_avatar")
        assert design["parameters"]["properties"]["spec"]["type"] == "object"
        assert design["parameters"]["properties"]["mode"]["enum"] == ["new", "adjust", "back"]
        assert "format" not in design["parameters"]["properties"]["spec"]["properties"]["species"]
        assert "## Your look" in executor.instructions() and "## New look" not in executor.instructions()

        early = await executor.execute("apply_avatar", {})
        assert not early["ok"] and early["spoken"]

        # Round 1: a new look, previewed live on the device from its pack URL.
        first = await executor.execute(
            "design_avatar",
            {"description": "变成一只戴眼镜的橘猫", "mode": "new",
             "spec": {"species": "cat", "name": "橘橘", "body_color": "橘色", "accessories": [{"type": "glasses"}]}},
        )
        assert first["ok"] and "屏幕" in first["spoken"] and "就这个了" in first["spoken"]
        assert first["preview_on_device"] and first["draft"] == 1 and first["drafts"] == 1
        assert first["preview_url"] == f"http://192.168.10.58:8766/xiaozhi/avatars/{first['id']}.png"
        device.call_tool.assert_awaited_once_with("self.avatar.preview", {"url": f"http://192.168.10.58:8766/xiaozhi/assets/{first['id']}.bin"})
        assert first["spec"]["accessories"] == [{"type": "glasses", "color": "#343f35"}]

        # Round 2: adjust keeps everything else (species, name, glasses stay; ears get longer, a scarf is added).
        second = await executor.execute(
            "design_avatar",
            {"description": "耳朵再长一点，加条蓝围巾", "mode": "adjust",
             "spec": {"ear_length": "long", "accessories": [{"type": "glasses"}, {"type": "scarf", "color": "blue"}]}},
        )
        assert second["ok"] and second["draft"] == 2 and second["drafts"] == 2 and "改好啦" in second["spoken"]
        assert second["spec"]["species"] == "cat" and second["spec"]["name"] == "橘橘" and second["spec"]["ear_length"] == "long"
        assert [a["type"] for a in second["spec"]["accessories"]] == ["scarf", "glasses"]
        assert device.call_tool.await_count == 2

        # Back to the first draft, then the user settles on it.
        back = await executor.execute("design_avatar", {"description": "回到上一个", "mode": "back"})
        assert back["ok"] and back["id"] == first["id"] and back["draft"] == 1 and "回到上一个" in back["spoken"]
        too_far = await executor.execute("design_avatar", {"description": "再回去", "mode": "back"})
        assert not too_far["ok"] and too_far["spoken"]

        applied = await executor.execute("apply_avatar", {})
        assert applied["ok"], applied
        expected_url = "http://192.168.10.58:8766/xiaozhi/assets/" + "ab" * 16 + ".bin"
        assert device.call_tool.await_args_list[-1].args == ("self.avatar.update", {"url": expected_url})
        assert stub.built[0].id == first["id"]
        persona = store.load()
        assert persona.avatar_pack["url"] == expected_url and persona.avatar_spec["species"] == "cat"
        assert persona.appearance.startswith("一只住在主人桌上的像素橘色的小猫：") and persona.avatar_announce
        assert persona.name == "小乐"  # the look never touches the name or wake word
        assert executor._avatar_draft is None and stub.pruned == {"ab" * 16 + ".bin"}

        # The next session mentions the new look once.
        assert "## New look" in executor.instructions()
        assert "## New look" not in executor.instructions()
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_discard_and_call_end_return_the_device_to_its_live_look(store, tmp_path):
    device = SimpleNamespace(
        device_connected=True,
        known_tool_names={"self.avatar.update", "self.avatar.preview", "self.avatar.preview_end"},
        call_tool=AsyncMock(return_value=(_device_text({"ok": True}), None)),
    )
    capture_server._last_ota_origin["origin"] = "http://192.168.10.58:8766"
    executor = VoiceToolExecutor(device, mac_controller=SimpleNamespace())
    executor._avatar_designer = _StubDesigner(tmp_path / "avatars")
    nothing = await executor.execute("discard_avatar_draft", {})
    assert nothing["ok"] and not nothing["discarded"]
    assert (await executor.execute("design_avatar", {"description": "一只熊", "mode": "new", "spec": {"species": "bear"}}))["ok"]
    dropped = await executor.execute("discard_avatar_draft", {})
    assert dropped["ok"] and dropped["discarded"] and "原来的我" in dropped["spoken"]
    assert device.call_tool.await_args_list[-1].args == ("self.avatar.preview_end", {})
    assert executor._avatar_draft is None
    # a draft still on the screen when the call ends is withdrawn too
    assert (await executor.execute("design_avatar", {"description": "一只狗", "mode": "new", "spec": {"species": "dog"}}))["ok"]
    calls_before = device.call_tool.await_count
    await executor.close()
    assert device.call_tool.await_args_list[-1].args == ("self.avatar.preview_end", {}) and device.call_tool.await_count == calls_before + 1


@pytest.mark.asyncio
async def test_design_without_a_device_preview_falls_back_to_words(store, tmp_path):
    device = SimpleNamespace(device_connected=False, known_tool_names=set(), call_tool=AsyncMock())
    executor = VoiceToolExecutor(device, mac_controller=SimpleNamespace())
    executor._avatar_designer = _StubDesigner(tmp_path / "avatars")
    try:
        result = await executor.execute("design_avatar", {"description": "一只狐狸", "mode": "new", "spec": {"species": "fox"}})
        assert result["ok"] and not result["preview_on_device"] and result["preview_note"] == "device offline"
        assert "口头" in result["spoken"] and device.call_tool.await_count == 0
    finally:
        await executor.close()


def test_merge_spec_replaces_accessories_and_ignores_blanks():
    base = {"species": "cat", "ear_length": "medium", "accessories": [{"type": "glasses"}]}
    merged = avatar_mod.merge_spec(base, {"ear_length": "long", "body_color": "", "name": None, "accessories": [{"type": "hat"}]})
    assert merged == {"species": "cat", "ear_length": "long", "accessories": [{"type": "hat"}]}
    assert avatar_mod.merge_spec(base, None) == base and avatar_mod.merge_spec(base, {}) is not base


@pytest.mark.asyncio
async def test_apply_avatar_keeps_persona_when_the_device_refuses_or_is_offline(store, tmp_path, monkeypatch):
    monkeypatch.setenv("STACKCHAN_ASSETS_BASE_URL", "https://xiaole.example.com/")
    device = SimpleNamespace(device_connected=True, call_tool=AsyncMock(return_value=(_device_text("failed: url must start with http"), None)))
    executor = VoiceToolExecutor(device, mac_controller=SimpleNamespace())
    executor._avatar_designer = _StubDesigner(tmp_path / "avatars")
    try:
        bad = await executor.execute("design_avatar", {"description": "x", "mode": "new", "spec": {"body_color": "not a colour"}})
        assert not bad["ok"] and bad["spoken"]
        unknown = await executor.execute("design_avatar", {"description": "x", "mode": "new", "spec": {"species": "dragon"}})
        assert not unknown["ok"] and unknown["error"] == "invalid tool arguments"  # enum checked before dispatch
        ok = await executor.execute("design_avatar", {"description": "一只熊", "mode": "new", "spec": {"species": "bear"}})
        assert ok["ok"]
        refused = await executor.execute("apply_avatar", {})
        assert not refused["ok"] and refused["spoken"]
        assert device.call_tool.await_args_list[-1].args == ("self.avatar.update", {"url": "https://xiaole.example.com/xiaozhi/assets/" + "ab" * 16 + ".bin"})
        assert not store.exists and executor._avatar_draft is not None
        device.device_connected = False
        offline = await executor.execute("apply_avatar", {})
        assert offline["error"] == "device offline"
    finally:
        await executor.close()


def test_devices_without_the_avatar_tool_do_not_get_the_declarations():
    device = SimpleNamespace(known_tool_names={"self.persona.set_name", "self.get_device_status"})
    executor = VoiceToolExecutor(device, mac_controller=SimpleNamespace())
    names = {d["name"] for d in executor.declarations()}
    assert "rename_assistant" in names and "design_avatar" not in names and "apply_avatar" not in names


# ---- download route ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_assets_route_serves_built_images_also_through_the_tunnel(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer

    root = tmp_path / "avatars"
    (root / "images").mkdir(parents=True)
    name = "c" * 32 + ".bin"
    (root / "images" / name).write_bytes(b"\x05\x00\x00\x00payload")
    app = capture_server.create_capture_app(avatar_dir=str(root))
    cf = {"CF-Connecting-IP": "203.0.113.9"}
    async with TestClient(TestServer(app)) as client:
        ok = await client.get(f"/xiaozhi/assets/{name}", headers={"Device-Id": "7c:0c:5f:42:41:6c"})
        assert ok.status == 200 and ok.headers["Content-Length"] == "11" and await ok.read() == b"\x05\x00\x00\x00payload"
        # reachable through the Cloudflare tunnel too (hash-named files carry no secret)
        assert (await client.get(f"/xiaozhi/assets/{name}", headers=cf)).status == 200
        assert (await client.get("/xiaozhi/assets/missing.bin")).status == 404
        assert (await client.get("/xiaozhi/assets/" + "d" * 32 + ".bin")).status == 404
        assert (await client.get("/xiaozhi/assets/..%2Fpersona.json")).status == 404
        # a design's pack and contact sheet, by its 16-hex id
        design = root / ("e" * 16)
        design.mkdir()
        (design / "avatar.bin").write_bytes(b"XLAVpack")
        (design / "contact-sheet.png").write_bytes(b"\x89PNGsheet")
        pack = await client.get("/xiaozhi/assets/" + "e" * 16 + ".bin", headers=cf)
        assert pack.status == 200 and await pack.read() == b"XLAVpack"
        sheet = await client.get("/xiaozhi/avatars/" + "e" * 16 + ".png")
        assert sheet.status == 200 and sheet.headers["Content-Type"] == "image/png" and await sheet.read() == b"\x89PNGsheet"
        assert (await client.get("/xiaozhi/avatars/" + "f" * 16 + ".png")).status == 404
        assert (await client.get("/xiaozhi/avatars/..%2Fpersona.json")).status == 404


@pytest.mark.asyncio
async def test_ota_check_remembers_where_the_device_reached_us():
    from tests.test_capture_server import FakeOtaRequest

    app = capture_server.create_capture_app(ota_ws_port=8765)
    await capture_server.handle_xiaozhi_ota(FakeOtaRequest(app, host="192.168.10.58:8766", headers={"Device-Id": "aa"}))
    assert capture_server.last_ota_origin() == "http://192.168.10.58:8766"
    assert avatar_mod.assets_url("x.bin", capture_server.last_ota_origin()) == "http://192.168.10.58:8766/xiaozhi/assets/x.bin"
    await capture_server.handle_xiaozhi_ota(
        FakeOtaRequest(app, host="xiaole.example.com", headers={"Device-Id": "aa", "CF-Connecting-IP": "1.2.3.4"})
    )
    assert capture_server.last_ota_origin() == "https://xiaole.example.com"


def test_persona_adopts_the_pack_the_device_came_back_with(store, tmp_path):
    designer = avatar_mod.AvatarDesigner(root=tmp_path / "avatars", base_image=None, models_dir=tmp_path / "none")
    pending = designer.design({"species": "bear", "accessories": [{"type": "hat", "color": "blue"}]}, "戴蓝帽子的小熊")
    (tmp_path / "avatars" / pending.id / "image.json").write_text(json.dumps({"image": "x.bin", "image_sha256": "y"}), encoding="utf-8")
    shown = {"source": "pack", "id": pending.id, "name": "小乐", "colors": 15}
    assert avatar_mod.adopt_device_avatar(store, shown, tmp_path / "avatars") == "adopted"
    persona = store.load()
    assert persona.avatar_pack["id"] == pending.id and persona.avatar_spec["species"] == "bear"
    assert persona.appearance.startswith("一只住在主人桌上的像素棕黄色的小熊：") and persona.avatar_announce
    assert avatar_mod.adopt_device_avatar(store, shown, tmp_path / "avatars") == "in_sync"
    assert avatar_mod.adopt_device_avatar(store, {"source": "pack", "id": "f" * 16}, tmp_path / "avatars") == "unknown"
    assert avatar_mod.adopt_device_avatar(store, {"source": "builtin", "id": "60c9"}, tmp_path / "avatars") == "builtin"
    assert avatar_mod.adopt_device_avatar(store, None, tmp_path / "avatars") == "skipped"
    assert store.load().avatar_pack["id"] == pending.id  # unknown or builtin reports never overwrite a known look
