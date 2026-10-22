# SPDX-FileCopyrightText: 2026 Joseph Zhou
# SPDX-License-Identifier: MIT
"""Music on the device's own speaker.

The firmware only plays audio while it is in the Speaking state, so a track is
sent the same way a spoken reply is: a ``tts start`` envelope, a
``sentence_start`` carrying the title for the subtitle bar, 60 ms Opus frames at
a steady pace, and ``tts stop`` at the end. The proxy owns the envelopes and
the Opus encoder (:class:`MusicSink`); this module owns the catalogue, the
decoder thread and the pacing.

Sources:

- ``STACKCHAN_MUSIC_DIR`` (default ``~/Music/xiaole``): local audio files,
  matched by file name.
- ``STACKCHAN_MUSIC_STREAMS``: ``name=url;name=url`` internet streams. When the
  variable is unset a few SomaFM channels are offered (SomaFM publishes its
  stream URLs for personal listening); set it to an empty string to offer none.

Decoding uses PyAV (the gateway's ``music`` extra); it is imported lazily so the
gateway keeps working without it, and ``play_music`` then says what to install.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SAMPLE_RATE = 24000
CHUNK_MS = 60
CHUNK_BYTES = SAMPLE_RATE * 2 * CHUNK_MS // 1000  # 24 kHz int16 mono
AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".flac", ".wav", ".ogg", ".opus", ".wma")

DEFAULT_STREAMS = (
    ("SomaFM Groove Salad", "https://ice1.somafm.com/groovesalad-128-mp3"),
    ("SomaFM Lush", "https://ice1.somafm.com/lush-128-mp3"),
    ("SomaFM Drone Zone", "https://ice1.somafm.com/dronezone-128-mp3"),
)

# Words that only say "play something": a query made of these alone is generic.
GENERIC_WORDS = (
    "播放", "放点", "放首", "放个", "来点", "来首", "来个", "点首", "点个", "放", "来", "点", "听", "听听",
    "一首", "一些", "一点", "首", "些", "个", "歌曲", "歌", "音乐", "随便", "随机", "什么", "都行", "吧", "啊",
    "please", "play", "some", "something", "anything", "any", "music", "songs", "song", "radio", "a", "me",
)


@dataclass(frozen=True)
class Track:
    title: str
    source: str  # file path or URL
    stream: bool = False  # a live station: never ends, no "next" within it
    online: bool = False  # found by the online search; not part of the library


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def music_enabled() -> bool:
    """STACKCHAN_MUSIC=0 turns the music tools off."""
    return _env_flag("STACKCHAN_MUSIC", True)


def online_search_enabled() -> bool:
    """STACKCHAN_MUSIC_ONLINE=0 keeps play_music to the local folder and the streams."""
    return _env_flag("STACKCHAN_MUSIC_ONLINE", True)


async def yt_dlp_search(query: str, *, timeout_s: float = 25.0) -> Track | None:
    """Resolve a song by name to a playable audio URL with yt-dlp (first search hit)."""
    import sys

    argv = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--quiet",
        "-f", "bestaudio[ext=m4a]/bestaudio/best", "--get-title", "--get-url",
        f"ytsearch1:{query}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except asyncio.TimeoutError:
        logger.warning("online music search timed out: %s", query)
        return None
    except (OSError, ModuleNotFoundError) as exc:
        logger.warning("online music search unavailable: %s", exc)
        return None
    if proc.returncode != 0:
        logger.warning("online music search failed (%s): %s", proc.returncode, stderr.decode("utf-8", "replace").strip()[:200])
        return None
    lines = [line.strip() for line in stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    if len(lines) < 2 or not lines[-1].startswith(("http://", "https://")):
        return None
    return Track(lines[0], lines[-1], stream=False, online=True)


OnlineSearch = Callable[[str], Awaitable[Track | None]]


def parse_streams(raw: str | None) -> list[Track]:
    """``name=url;name=url`` → streams; None → the SomaFM defaults; "" → none."""
    if raw is None:
        return [Track(name, url, True) for name, url in DEFAULT_STREAMS]
    tracks: list[Track] = []
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        name, sep, url = item.partition("=")
        if not sep or not url.strip().startswith(("http://", "https://")):
            logger.warning("STACKCHAN_MUSIC_STREAMS entry ignored (want name=http...): %s", item)
            continue
        tracks.append(Track(name.strip() or url.strip(), url.strip(), True))
    return tracks


def _normalize(text: str) -> str:
    return "".join(ch.lower() for ch in text if not ch.isspace() and ch not in "-_.,()[]（）【】《》")


def _is_generic(needle: str) -> bool:
    rest = needle
    for word in sorted(GENERIC_WORDS, key=len, reverse=True):
        rest = rest.replace(word, "")
    return rest == ""


class MusicLibrary:
    """Local files plus internet streams, matched by a loose title search."""

    def __init__(self, directory: Path | None, streams: Iterable[Track] = ()):
        self.directory = directory
        self.streams = list(streams)

    @classmethod
    def from_env(cls) -> "MusicLibrary":
        raw_dir = os.getenv("STACKCHAN_MUSIC_DIR", "").strip() or "~/Music/xiaole"
        return cls(Path(raw_dir).expanduser(), parse_streams(os.getenv("STACKCHAN_MUSIC_STREAMS")))

    def files(self) -> list[Track]:
        if self.directory is None or not self.directory.is_dir():
            return []
        return [
            Track(path.stem, str(path))
            for path in sorted(self.directory.rglob("*"))
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES
        ]

    def tracks(self) -> list[Track]:
        return [*self.files(), *self.streams]

    def find(self, query: str, *, rng: random.Random | None = None) -> Track | None:
        """Best match for ``query``; an empty or generic query picks a file at random."""
        tracks = self.tracks()
        if not tracks:
            return None
        needle = _normalize(query or "")
        if _is_generic(needle):
            files = self.files()
            return (rng or random).choice(files or tracks)
        exact = [t for t in tracks if _normalize(t.title) == needle]
        if exact:
            return exact[0]
        partial = [t for t in tracks if needle in _normalize(t.title) or _normalize(t.title) in needle]
        if partial:
            return partial[0]
        words = [w for w in needle.replace("的", " ").split() if w]
        scored = [(sum(1 for w in words if w in _normalize(t.title)), t) for t in tracks]
        best = max(scored, key=lambda item: item[0], default=(0, None))
        return best[1] if best[0] > 0 else None

    def after(self, track: Track, *, rng: random.Random | None = None) -> Track | None:
        """The track for "next": the following file, else another one at random."""
        tracks = self.tracks()
        others = [t for t in tracks if t != track]
        if not others:
            return None
        files = [t for t in tracks if not t.stream]
        if not track.stream and track in files and len(files) > 1:
            return files[(files.index(track) + 1) % len(files)]
        return (rng or random).choice([t for t in others if not t.stream] or others)


class MusicSink(Protocol):
    """What the proxy provides: the envelopes and the Opus transport."""

    async def begin(self, title: str) -> bool: ...
    async def send_pcm(self, pcm: bytes) -> bool: ...
    async def end(self) -> None: ...


def pyav_decoder(source: str, *, stop: threading.Event | None = None) -> Iterator[bytes]:
    """Decode ``source`` (file or URL) into 24 kHz mono int16 PCM chunks."""
    import av  # lazy: optional dependency (the "music" extra)

    options = None
    if "://" in source:
        options = {"rw_timeout": "15000000", "reconnect": "1", "reconnect_streamed": "1"}
    with av.open(source, options=options, timeout=20.0) as container:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            if stop is not None and stop.is_set():
                return
            for out in resampler.resample(frame):
                data = bytes(out.planes[0])[: out.samples * 2]
                if data:
                    yield data
        for out in resampler.resample(None):
            data = bytes(out.planes[0])[: out.samples * 2]
            if data:
                yield data


Decoder = Callable[..., Iterator[bytes]]


class MusicPlayer:
    """Plays one track at a time through a :class:`MusicSink`.

    ``hold`` / ``release`` belong to the proxy: the model's own speech borrows
    the speaker (the firmware has one audio queue), so frames pause while it
    talks and continue afterwards unless the user paused or stopped the music.
    ``notify_stopped`` tells the player the proxy sent ``tts stop`` for some
    other reason, so the next frame is preceded by a fresh ``begin``.
    """

    def __init__(
        self,
        library: MusicLibrary,
        sink: MusicSink,
        *,
        decoder: Decoder = pyav_decoder,
        queue_ahead_ms: int = 600,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        autoplay_next: bool = True,
        online_search: OnlineSearch | None = None,
    ) -> None:
        self.library = library
        self.sink = sink
        self.decoder = decoder
        self.online_search = online_search
        self.queue_ahead_ms = queue_ahead_ms
        self.sleep = sleep
        self.clock = clock
        self.autoplay_next = autoplay_next
        self.current: Track | None = None
        self.paused = False  # by the user
        self.held = False  # by the model's speech
        self.last_error: str | None = None
        self.began = False  # the device is in Speaking state for this track
        self._task: asyncio.Task | None = None
        self._stop_event: threading.Event | None = None
        self._resume = asyncio.Event()
        self._resume.set()

    @property
    def playing(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        if not self.playing or self.current is None:
            return {"playing": False, "last_error": self.last_error}
        return {
            "playing": True,
            "paused": self.paused,
            "title": self.current.title,
            "stream": self.current.stream,
        }

    async def play(self, query: str) -> dict[str, Any]:
        track = self.library.find(query)
        if track is None and self.online_search is not None and not _is_generic(_normalize(query or "")):
            track = await self.online_search(query)
        if track is None:
            titles = [t.title for t in self.library.tracks()][:12]
            if not titles:
                return {
                    "ok": False,
                    "error": "no music available: put audio files in the music folder or configure streams",
                    "music_dir": str(self.library.directory) if self.library.directory else None,
                }
            return {"ok": False, "error": f"no track matches {query!r}", "available": titles}
        await self._start(track)
        return {"ok": True, "title": track.title, "stream": track.stream, "online": track.online}

    async def _start(self, track: Track) -> None:
        await self.stop(quiet=True)
        self.current = track
        self.paused = False
        self.last_error = None
        self.began = False
        if not self.held:
            self._resume.set()
        self._stop_event = threading.Event()
        self._task = asyncio.create_task(self._run(track, self._stop_event), name="music-player")

    async def stop(self, *, quiet: bool = False) -> dict[str, Any]:
        task, stop_event = self._task, self._stop_event
        title = self.current.title if self.current else None
        self._task = None
        self._stop_event = None
        if stop_event is not None:
            stop_event.set()
        self._resume.set()
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self.held:
            self._resume.clear()  # the task is gone; keep the next one waiting
        self.current = None
        self.paused = False
        return {"ok": True, "stopped": title} if not quiet else {"ok": True}

    async def pause(self) -> dict[str, Any]:
        if not self.playing:
            return {"ok": False, "error": "nothing is playing"}
        self.paused = True
        self._resume.clear()
        return {"ok": True, "paused": self.current.title if self.current else None}

    async def resume(self) -> dict[str, Any]:
        if not self.playing:
            return {"ok": False, "error": "nothing is playing"}
        self.paused = False
        if not self.held:
            self._resume.set()
        return {"ok": True, "resumed": self.current.title if self.current else None}

    async def next(self) -> dict[str, Any]:
        if self.current is None:
            return await self.play("")
        following = self.library.after(self.current) if not self.current.online else self.library.find("")
        if following is None:
            return {"ok": False, "error": "no other track"}
        await self._start(following)
        return {"ok": True, "title": following.title, "stream": following.stream}

    def hold(self) -> None:
        """The model is about to speak: stop sending frames until released."""
        self.held = True
        self._resume.clear()

    def release(self) -> None:
        self.held = False
        if not self.paused:
            self._resume.set()

    def notify_stopped(self) -> None:
        """The proxy sent ``tts stop`` itself; the next frame needs a new ``begin``."""
        self.began = False

    async def _run(self, track: Track, stop_event: threading.Event) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None | Exception] = asyncio.Queue(maxsize=64)

        def put(item: bytes | None | Exception) -> None:
            asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()

        def produce() -> None:
            try:
                pending = bytearray()
                for chunk in self.decoder(track.source, stop=stop_event):
                    if stop_event.is_set():
                        return
                    pending.extend(chunk)
                    while len(pending) >= CHUNK_BYTES:
                        put(bytes(pending[:CHUNK_BYTES]))
                        del pending[:CHUNK_BYTES]
                        if stop_event.is_set():
                            return
                if pending:
                    put(bytes(pending))
                put(None)
            except Exception as exc:  # the decoder's failure reaches the consumer
                try:
                    put(exc)
                except Exception:
                    pass

        threading.Thread(target=produce, name="music-decoder", daemon=True).start()
        completed = False
        sent_ms = 0.0
        t0 = self.clock()
        try:
            while True:
                item = await queue.get()
                if item is None:
                    completed = True
                    break
                if isinstance(item, Exception):
                    self.last_error = f"{type(item).__name__}: {item}"
                    logger.warning("music decode failed for %s: %s", track.title, self.last_error)
                    completed = True
                    break
                if not self._resume.is_set():
                    if self.paused and self.began:
                        await self.sink.end()
                        self.began = False
                    await self._resume.wait()
                if not self.began:
                    if not await self.sink.begin(track.title):
                        self.last_error = "device refused playback"
                        break
                    self.began = True
                    sent_ms = 0.0
                    t0 = self.clock()
                if not await self.sink.send_pcm(item):
                    self.last_error = "device send failed"
                    break
                sent_ms += len(item) / (SAMPLE_RATE * 2) * 1000.0
                ahead_ms = sent_ms - (self.clock() - t0) * 1000.0
                if ahead_ms > self.queue_ahead_ms:
                    await self.sleep((ahead_ms - self.queue_ahead_ms) / 1000.0)
        finally:
            stop_event.set()
            if self.began:
                self.began = False
                try:
                    await asyncio.shield(self.sink.end())
                except Exception as exc:
                    logger.debug("music end envelope failed: %s", exc)
            if completed and self._stop_event is stop_event:
                self._task = None
                self._stop_event = None
                following = (
                    self.library.after(track)
                    if self.autoplay_next and not track.stream and not track.online
                    else None
                )
                self.current = None
                if following is not None and self.last_error is None:
                    loop.create_task(self._start(following), name="music-autoplay")


# --- the voice tools --------------------------------------------------------

MUSIC_INSTRUCTION = """## Music

Whenever the user asks for music or a song (放歌、放音乐、播放某首歌、来一首、听歌), call play_music(query) and nothing else: not send_task, not a game, not a Mac tool. It plays on this device's own speaker, from the local music folder, the configured stations, or an online search by song and artist name. Pass the song or artist words the user said; an empty or vague request ("放点音乐", "play something") is an empty query. control_music(action) with pause, resume, stop, next or status controls playback. While music plays you keep listening: when the user talks, answer briefly and the music continues afterwards; "停" / "别放了" means stop. Say which track or station is playing in one short sentence; play_music may take a few seconds while it searches, so tell the user it is on its way."""


def music_declarations() -> tuple[dict[str, Any], ...]:
    return (
        {
            "name": "play_music",
            "description": (
                "Play music on this device's own speaker: a song, artist or station by name "
                "(放歌 / 放音乐 / 播放 … / 来一首 / 听歌 / play …). Searches the local folder, the "
                "stations and then online; an empty query picks something. The only tool for music."
            ),
            # Plain JSON Schema (lowercase types, no "format"): Grok's realtime API
            # ignores a tool whose schema it cannot parse.
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "track, artist or station name; empty for any"},
                },
            },
        },
        {
            "name": "control_music",
            "description": "Control the music playing on the device: pause, resume, stop, next or status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["pause", "resume", "stop", "next", "status"],
                        "description": "pause | resume | stop | next | status",
                    },
                },
                "required": ["action"],
            },
        },
    )


def build_music_extension(player: MusicPlayer):
    """A :class:`stackchan_mcp.voice_tools.ToolExtension` for ``player``."""
    from .voice_tools import ToolExtension, ToolMetadata

    async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "play_music":
            return await player.play(str(args.get("query", "") or ""))
        if name == "control_music":
            action = str(args.get("action", "status"))
            if action == "pause":
                return await player.pause()
            if action == "resume":
                return await player.resume()
            if action == "stop":
                return await player.stop()
            if action == "next":
                return await player.next()
            return {"ok": True, **player.status()}
        return {"ok": False, "error": f"unknown tool {name}"}

    async def close() -> None:
        await player.stop(quiet=True)

    return ToolExtension(
        declarations=music_declarations(),
        dispatch=dispatch,
        metadata={
            "play_music": ToolMetadata(True, "music", 20.0),
            "control_music": ToolMetadata(True, "music", 10.0),
        },
        close=close,
        instructions=MUSIC_INSTRUCTION,
    )
