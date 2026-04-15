"""
Test the Qwen realtime TTS WebSocket endpoint: ws://host/tts/realtime/qwen
Usage: python test_tts_qwen_realtime.py [full sentence]
"""
import asyncio
import json
import sys
import uuid

import websockets

WS_URL = "ws://localhost:9000/tts/realtime/qwen"
OUT_BASE = "./tts_qwen_realtime_output"
MIN_FIRST_CHUNK = 20
CHUNK_CHARS = 4
CHUNK_DELAY_S = 0.06

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _suffix_for_codec(codec: str) -> str:
    if codec == "audio/mpeg":
        return ".mp3"
    if codec == "pcm_s16le":
        return ".pcm"
    if codec == "wav":
        return ".wav"
    if codec == "audio/ogg":
        return ".ogg"
    return ".bin"


async def run_stream(text: str) -> None:
    audio: list[bytes] = []
    codec_info: dict | None = None

    async def recv_loop(ws):
        nonlocal codec_info
        while True:
            msg = await ws.recv()
            if isinstance(msg, bytes):
                audio.append(msg)
                print(f"  <- binary {len(msg)} bytes")
                continue
            data = json.loads(msg)
            event_type = data.get("type")
            print(f"  <- {data}")
            if event_type == "tts_start":
                codec_info = data
            elif event_type == "completed":
                return
            elif event_type == "error":
                raise RuntimeError(f"{data.get('code')}: {data.get('message')}")

    full = text.strip() or "你好，这是 Qwen 实时语音合成独立接口测试。"
    if len(full) <= MIN_FIRST_CHUNK:
        chunks = [full]
    else:
        chunks = [full[:MIN_FIRST_CHUNK]]
        tail = full[MIN_FIRST_CHUNK:]
        chunks.extend([tail[i: i + CHUNK_CHARS] for i in range(0, len(tail), CHUNK_CHARS)])

    async with websockets.connect(WS_URL) as ws:
        recv_task = asyncio.create_task(recv_loop(ws))
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "start",
                        "sessionId": str(uuid.uuid4()),
                        "terminalId": "test-tts-qwen-rt",
                        "roundId": "round-1",
                    },
                    ensure_ascii=False,
                )
            )

            for piece in chunks:
                await ws.send(json.dumps({"type": "text", "content": piece}, ensure_ascii=False))
                await asyncio.sleep(CHUNK_DELAY_S)

            await ws.send(json.dumps({"type": "finish"}, ensure_ascii=False))
            await recv_task
        except BaseException:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass
            raise

    raw = b"".join(audio)
    ext = _suffix_for_codec((codec_info or {}).get("codec") or "")
    path = OUT_BASE + ext
    with open(path, "wb") as f:
        f.write(raw)
    print(f"audio bytes={len(raw)} -> {path}")


if __name__ == "__main__":
    arg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    asyncio.run(run_stream(arg))
