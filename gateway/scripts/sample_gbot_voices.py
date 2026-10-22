"""Render one Xiaole line with each Grok voice id to wav, for picking STACKCHAN_GBOT_VOICE_NAME.

Usage: NO_PROXY="*" uv run --all-extras python scripts/sample_gbot_voices.py carina iris eve
Needs the Grok Bot login bridge on 127.0.0.1:18771. "default" renders the server default voice.
"""
import asyncio, base64, json, os, sys, time, wave
import aiohttp
from stackchan_mcp.gbot_voice_bridge import gather_with_cancel, voice_url_with_model
BRIDGE = "http://127.0.0.1:18771"
OUT = os.environ.get("VOICE_SAMPLE_DIR", "voice-samples")
LINE = "诶？真的呀！太好啦！呜，辛苦啦……先喝口水歇一歇好不好？我是小乐呀，你桌上的小兔子，嘿嘿。"

async def sample(name, timeout=40.0):
    out = {"voice": name, "bytes": 0, "errors": [], "transcript": ""}
    async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=90)) as http:
        async def local(path, body=None):
            async with http.request("GET" if body is None else "POST", BRIDGE + path, json=body, allow_redirects=False) as r:
                if r.status != 200: raise RuntimeError("local_failed %d" % r.status)
                v = await r.json()
                if v.get("ok") is False: raise RuntimeError("local_rejected")
                return v
        agent = (await local("/health"))["agentId"]
        minted, _ = await gather_with_cancel(local("/credential"), local("/session", {"agentId": agent, "agentName": "小乐", "spokenLanguage": "zh"}))
        cred = minted["credential"]
        url = voice_url_with_model(cred["websocketUrl"], cred["model"])
        config = {"instructions": "你是小乐。用户会给你一段话，请一字不差、用活泼有感情的语气把这段话读出来，不要加任何别的话，不要调用工具。",
                  "turn_detection": {"type": "server_vad"},
                  "audio": {"input": {"format": {"type": "audio/pcm", "rate": 24000}}, "output": {"format": {"type": "audio/pcm", "rate": 24000}}}}
        if name != "default":
            config["voice"] = name
        pcm = bytearray()
        async with http.ws_connect(url, protocols=["xai-client-secret." + cred["clientSecret"]], max_msg_size=8*1024*1024, heartbeat=20) as ws:
            await ws.send_json({"type": "session.update", "session": config})
            await ws.send_json({"type": "conversation.item.create", "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "请读这段话：" + LINE}]}})
            await ws.send_json({"type": "response.create"})
            async def recv():
                async for m in ws:
                    if m.type != aiohttp.WSMsgType.TEXT: break
                    ev = json.loads(m.data); t = ev.get("type")
                    if t in ("response.audio.delta", "response.output_audio.delta"):
                        pcm.extend(base64.b64decode(ev.get("delta", "")))
                    elif t in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
                        out["transcript"] = ev.get("transcript", "")
                    elif t == "response.done":
                        break
                    elif t == "error":
                        e = ev.get("error", ev); out["errors"].append({k: e.get(k) for k in ("code", "type", "message") if k in e}); break
            try: await asyncio.wait_for(recv(), timeout)
            except asyncio.TimeoutError: out["errors"].append("timeout")
        out["bytes"] = len(pcm); os.makedirs(OUT, exist_ok=True)
        if pcm:
            with wave.open(f"{OUT}/xiaole-{name}.wav", "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(bytes(pcm))
            out["seconds"] = round(len(pcm) / 48000, 1)
    return out

async def main():
    for name in sys.argv[1:]:
        try: print(json.dumps(await sample(name), ensure_ascii=False))
        except Exception as e: print(json.dumps({"voice": name, "failure": type(e).__name__}))
asyncio.run(main())
