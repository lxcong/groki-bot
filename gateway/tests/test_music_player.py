# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Music on the device speaker: catalogue matching, pacing and the speaking envelopes."""

import asyncio
import random
from pathlib import Path

import pytest

from stackchan_mcp.music_player import (
    CHUNK_BYTES,
    MusicLibrary,
    MusicPlayer,
    Track,
    build_music_extension,
    parse_streams,
)


class RecordingSink:
    def __init__(self, *, refuse=False):
        self.events: list = []
        self.refuse = refuse
        self.fail_send = False

    async def begin(self, title):
        self.events.append(("begin", title))
        return not self.refuse

    async def send_pcm(self, pcm):
        if self.fail_send:
            return False
        self.events.append(("pcm", len(pcm)))
        return True

    async def end(self):
        self.events.append(("end",))


def make_decoder(chunks, *, fail_after=None):
    calls = []

    def decode(source, *, stop=None):
        calls.append(source)
        for index, chunk in enumerate(chunks):
            if fail_after is not None and index == fail_after:
                raise RuntimeError("bad stream")
            yield chunk

    decode.calls = calls
    return decode


def make_library(tmp_path: Path, names=("晴天", "Yellow", "夜曲"), streams=()):
    for name in names:
        (tmp_path / f"{name}.mp3").write_bytes(b"x")
    return MusicLibrary(tmp_path, streams)


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def wait_for(condition, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


def test_streams_parse_defaults_and_entries():
    assert [t.title for t in parse_streams(None)][:1] == ["SomaFM Groove Salad"]
    assert parse_streams("") == []
    tracks = parse_streams("Jazz=https://example.invalid/jazz; broken ;=https://x.invalid/u;Nope=ftp://x")
    assert tracks == [Track("Jazz", "https://example.invalid/jazz", True), Track("https://x.invalid/u", "https://x.invalid/u", True)]


def test_library_matches_loosely_and_picks_files_for_generic_queries(tmp_path):
    library = make_library(tmp_path, streams=[Track("SomaFM Lush", "https://x.invalid/lush", True)])
    assert library.find("晴天").title == "晴天"
    assert library.find("放 yellow").title == "Yellow"
    assert library.find("周杰伦的夜曲").title == "夜曲"
    assert library.find("lush").stream is True
    assert library.find("不存在的歌") is None
    rng = random.Random(1)
    for generic in ("放点音乐", "", "play something", "随便来一首歌吧", "music"):
        assert library.find(generic, rng=rng).stream is False, generic
    assert library.after(Track("Yellow", str(tmp_path / "Yellow.mp3"))).title == "夜曲"
    assert library.after(Track("夜曲", str(tmp_path / "夜曲.mp3"))).title == "晴天"
    assert MusicLibrary(tmp_path / "missing").find("") is None


@pytest.mark.asyncio
async def test_player_brackets_a_track_with_begin_and_end_and_paces_frames(tmp_path):
    library = make_library(tmp_path, names=("solo",))
    sink = RecordingSink()
    sleeps = []
    clock = {"now": 0.0}

    async def sleep(seconds):
        sleeps.append(round(seconds, 3))
        clock["now"] += seconds

    decoder = make_decoder([b"\0" * (CHUNK_BYTES * 3 + 10)])
    player = MusicPlayer(library, sink, decoder=decoder, queue_ahead_ms=120, sleep=sleep, clock=lambda: clock["now"], autoplay_next=False)
    result = await player.play("solo")
    assert result == {"ok": True, "title": "solo", "stream": False, "online": False}
    await player._task
    assert sink.events[0] == ("begin", "solo")
    assert [e for e in sink.events if e[0] == "pcm"] == [("pcm", CHUNK_BYTES)] * 3 + [("pcm", 10)]
    assert sink.events[-1] == ("end",)
    # 60 ms per chunk, 120 ms allowed ahead: the third chunk waits for the first to play out;
    # the 10-byte tail only adds a fraction of a millisecond.
    assert sleeps[0] == 0.06 and all(s < 0.001 for s in sleeps[1:])
    assert player.playing is False and player.current is None
    assert decoder.calls == [str(tmp_path / "solo.mp3")]


@pytest.mark.asyncio
async def test_pause_ends_speaking_and_resume_begins_again(tmp_path):
    library = make_library(tmp_path, names=("solo",))
    sink = RecordingSink()
    gate = asyncio.Event()

    def decoder(source, *, stop=None):
        yield b"\0" * CHUNK_BYTES
        yield b"\0" * CHUNK_BYTES
        # Block the decoder thread until the test releases it (stop() sets the event too).
        while not gate.is_set() and not stop.is_set():
            __import__("time").sleep(0.005)
        yield b"\0" * CHUNK_BYTES

    async def no_sleep(_):
        pass

    player = MusicPlayer(library, sink, decoder=decoder, sleep=no_sleep, autoplay_next=False)
    await player.play("solo")
    await settle()
    assert ("begin", "solo") in sink.events
    assert (await player.pause())["ok"]
    gate.set()
    await wait_for(lambda: sink.events[-1] == ("end",))
    assert player.status() == {"playing": True, "paused": True, "title": "solo", "stream": False}
    assert (await player.resume())["ok"]
    await player._task
    begins = [e for e in sink.events if e[0] == "begin"]
    assert len(begins) == 2 and sink.events[-1] == ("end",)


@pytest.mark.asyncio
async def test_hold_keeps_the_device_speaking_until_the_proxy_reports_a_stop(tmp_path):
    library = make_library(tmp_path, names=("solo",))
    sink = RecordingSink()

    async def no_sleep(_):
        pass

    decoder = make_decoder([b"\0" * CHUNK_BYTES] * 3)
    player = MusicPlayer(library, sink, decoder=decoder, sleep=no_sleep, autoplay_next=False)
    player.hold()  # the model is talking when the user asks for music
    await player.play("solo")
    await settle()
    assert sink.events == []  # nothing sent while held
    player.notify_stopped()  # the model's tts.stop went out
    player.release()
    await player._task
    assert sink.events[0] == ("begin", "solo") and sink.events[-1] == ("end",)
    assert sum(1 for e in sink.events if e[0] == "pcm") == 3


@pytest.mark.asyncio
async def test_stop_next_autoplay_and_decoder_errors(tmp_path):
    library = make_library(tmp_path, names=("alpha", "beta"))
    sink = RecordingSink()

    async def no_sleep(_):
        pass

    decoder = make_decoder([b"\0" * CHUNK_BYTES])
    player = MusicPlayer(library, sink, decoder=decoder, sleep=no_sleep)
    await player.play("alpha")
    # autoplay: alpha, then beta, then back to alpha (the library wraps around)
    begins = lambda: [e[1] for e in sink.events if e[0] == "begin"]
    await wait_for(lambda: len(begins()) >= 3)
    assert begins()[:3] == ["alpha", "beta", "alpha"]
    assert (await player.stop())["ok"]
    assert player.playing is False and sink.events[-1] == ("end",)

    failing = MusicPlayer(library, RecordingSink(), decoder=make_decoder([b"\0" * CHUNK_BYTES, b"x"], fail_after=1), sleep=no_sleep, autoplay_next=False)
    await failing.play("alpha")
    await failing._task
    assert failing.last_error == "RuntimeError: bad stream"
    assert failing.status() == {"playing": False, "last_error": "RuntimeError: bad stream"}

    refused = MusicPlayer(library, RecordingSink(refuse=True), decoder=make_decoder([b"\0" * CHUNK_BYTES]), sleep=no_sleep, autoplay_next=False)
    await refused.play("alpha")
    await refused._task
    assert refused.last_error == "device refused playback"


@pytest.mark.asyncio
async def test_extension_dispatch_and_empty_library_message(tmp_path):
    sink = RecordingSink()

    async def no_sleep(_):
        pass

    empty = MusicPlayer(MusicLibrary(tmp_path / "none", []), sink, decoder=make_decoder([]), sleep=no_sleep)
    extension = build_music_extension(empty)
    assert [d["name"] for d in extension.declarations] == ["play_music", "control_music"]
    assert "## Music" in extension.instructions
    result = await extension.dispatch("play_music", {"query": "anything"})
    assert result["ok"] is False and "no music available" in result["error"]
    assert (await extension.dispatch("control_music", {"action": "pause"})) == {"ok": False, "error": "nothing is playing"}
    assert (await extension.dispatch("control_music", {"action": "status"})) == {"ok": True, "playing": False, "last_error": None}

    library = make_library(tmp_path, names=("a",))
    player = MusicPlayer(library, sink, decoder=make_decoder([b"\0" * CHUNK_BYTES]), sleep=no_sleep, autoplay_next=False)
    extension = build_music_extension(player)
    assert (await extension.dispatch("play_music", {"query": "zzz"})) == {"ok": False, "error": "no track matches 'zzz'", "available": ["a"]}
    assert (await extension.dispatch("play_music", {"query": "a"}))["ok"]
    await extension.close()
    assert player.playing is False


@pytest.mark.asyncio
async def test_proxy_brackets_music_and_lets_a_reply_borrow_the_speaker(monkeypatch, tmp_path):
    """The proxy's sink: tts envelopes with the title, Opus frames, hold while the model talks."""
    import json
    from test_gemini_voice_proxy import _make_proxy, info as info_fixture  # noqa: F401

    from stackchan_mcp.gemini_voice_proxy import CloudConnectionInfo

    info = CloudConnectionInfo(cloud_url="wss://example.invalid/x", authorization="", protocol_version="1", device_id="dev", client_id="cli")
    proxy, _esp32, _sent = _make_proxy(monkeypatch=monkeypatch)
    sent: list = []

    async def send(msg):
        sent.append(msg)

    assert await proxy.start(info, {"type": "hello"}, send)
    sink = proxy._MusicSink(proxy) if hasattr(proxy, "_MusicSink") else __import__("stackchan_mcp.gemini_voice_proxy", fromlist=["_MusicSink"])._MusicSink(proxy)

    assert await sink.begin("Lush")
    assert json.loads(sent[-2]) == {"type": "tts", "state": "start"}
    assert json.loads(sent[-1]) == {"type": "tts", "state": "sentence_start", "text": "♪ Lush"}
    assert await sink.send_pcm(b"\0" * CHUNK_BYTES)
    assert sent[-1] == b"OPUS-FRAME"
    # A tool-only turn ends without touching the music's Speaking state.
    await proxy.end_tts()
    assert sent[-1] == b"OPUS-FRAME"
    # A spoken reply takes the speaker over: no second tts.start, and the reply's
    # tts.stop hands it back so the player begins again.
    player = type("P", (), {"playing": True, "held": False, "stopped": False})()
    player.hold = lambda: setattr(player, "held", True)
    player.release = lambda: setattr(player, "held", False)
    player.notify_stopped = lambda: setattr(player, "stopped", True)

    async def fake_stop(quiet=False):
        player.playing = False

    player.stop = fake_stop
    proxy._music = player
    await proxy._handle_gemini_audio(b"\0" * 480)
    assert player.held is True
    starts = lambda: sum(1 for m in sent if isinstance(m, str) and json.loads(m) == {"type": "tts", "state": "start"})
    assert starts() == 1  # the reply did not send a second tts.start
    await proxy.end_tts()
    assert json.loads(sent[-1]) == {"type": "tts", "state": "stop"}
    assert player.stopped is True and player.held is False
    await sink.end()  # nothing left to stop: the reply already sent tts.stop
    assert sum(1 for m in sent if isinstance(m, str) and json.loads(m) == {"type": "tts", "state": "stop"}) == 1
    await proxy.stop()


@pytest.mark.asyncio
async def test_online_search_is_the_fallback_for_named_songs_only(tmp_path):
    library = make_library(tmp_path, names=("alpha",))
    sink = RecordingSink()
    searched = []

    async def no_sleep(_):
        pass

    async def search(query):
        searched.append(query)
        return Track("Golden (Official Video)", "https://example.invalid/golden.m4a", online=True) if "golden" in query.lower() else None

    player = MusicPlayer(library, sink, decoder=make_decoder([b"\0" * CHUNK_BYTES]), sleep=no_sleep, online_search=search)
    result = await player.play("Golden 的音乐")
    assert result == {"ok": True, "title": "Golden (Official Video)", "stream": False, "online": True}
    assert searched == ["Golden 的音乐"]
    await wait_for(lambda: player.playing is False)
    assert player.current is None  # an online song does not autoplay into the library
    assert (await player.play("放点音乐"))["title"] == "alpha"  # generic: never searched online
    assert searched == ["Golden 的音乐"]
    await player.stop()
    assert (await player.play("nonexistent zzz")) == {"ok": False, "error": "no track matches 'nonexistent zzz'", "available": ["alpha"]}
    assert searched[-1] == "nonexistent zzz"


@pytest.mark.asyncio
async def test_yt_dlp_search_parses_title_and_url(monkeypatch):
    from stackchan_mcp import music_player

    class Proc:
        returncode = 0

        async def communicate(self):
            return (b"Some Song (Official)\nhttps://example.invalid/a.m4a\n", b"")

    async def fake_exec(*argv, **kwargs):
        assert argv[-1] == "ytsearch1:some song" and "--get-url" in argv
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    track = await music_player.yt_dlp_search("some song")
    assert track == Track("Some Song (Official)", "https://example.invalid/a.m4a", False, True)

    class Failing(Proc):
        returncode = 1

        async def communicate(self):
            return (b"", b"ERROR: no results")

    async def failing_exec(*argv, **kwargs):
        return Failing()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", failing_exec)
    assert await music_player.yt_dlp_search("nothing") is None


def test_music_declarations_are_plain_json_schema():
    """Grok drops tools whose schema is not plain JSON Schema (uppercase types, format=enum)."""
    from stackchan_mcp.music_player import music_declarations

    def walk(schema):
        assert schema.get("type") in {"object", "string", "integer", "number", "boolean", "array"}, schema
        assert "format" not in schema
        for child in schema.get("properties", {}).values():
            walk(child)

    for declaration in music_declarations():
        walk(declaration["parameters"])

