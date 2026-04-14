"""
测试 TTS WebSocket 流式：ws://host/tts/realtime
用法: python test_tts_realtime.py [完整句子]
"""
import asyncio
import json
import sys
import uuid

import websockets

WS_URL = "ws://localhost:9000/tts/realtime"
OUT_BASE = "./tts_realtime_output"
# 首包太短时上游往往不出首帧音频，会触发服务端 TTS_FIRST_CHUNK_TIMEOUT
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
            t = data.get("type")
            print(f"  <- {data}")
            if t == "tts_start":
                codec_info = data
            elif t == "completed":
                return
            elif t == "error":
                raise RuntimeError(f"{data.get('code')}: {data.get('message')}")

    full = text.strip() or "你好，这是流式分片测试。"
    if len(full) <= MIN_FIRST_CHUNK:
        chunks = [full]
    else:
        chunks = [full[:MIN_FIRST_CHUNK]]
        tail = full[MIN_FIRST_CHUNK:]
        chunks.extend([tail[i : i + CHUNK_CHARS] for i in range(0, len(tail), CHUNK_CHARS)])

    async with websockets.connect(WS_URL) as ws:
        recv_task = asyncio.create_task(recv_loop(ws))
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "start",
                        "sessionId": str(uuid.uuid4()),
                        "terminalId": "test-tts-rt",
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
    print(f"音频共 {len(raw)} bytes -> {path}")


if __name__ == "__main__":
    arg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    asyncio.run(run_stream(arg))
