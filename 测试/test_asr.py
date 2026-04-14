"""
测试 ASR 实时识别 WebSocket
用法: python test_asr.py [wav文件路径]
"""
import asyncio
import json
import struct
import sys
import uuid
import wave
from pathlib import Path

import websockets

WS_URL = "ws://localhost:9000/asr/realtime"
DEFAULT_WAV = str(Path(__file__).resolve().parent / "test.wav")
CHUNK_FRAMES = 1600  # 100ms @ 16kHz


def read_wav_pcm16(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as wf:
        assert wf.getsampwidth() == 2, "需要 16bit PCM"
        assert wf.getnchannels() == 1, "需要单声道"
        sr = wf.getframerate()
        data = wf.readframes(wf.getnframes())
    return data, sr


def resample_nearest(data: bytes, src_sr: int, dst_sr: int = 16000) -> bytes:
    if src_sr == dst_sr:
        return data
    samples = struct.unpack(f"<{len(data)//2}h", data)
    ratio = src_sr / dst_sr
    new_len = int(len(samples) / ratio)
    resampled = [samples[min(int(i * ratio), len(samples) - 1)] for i in range(new_len)]
    return struct.pack(f"<{len(resampled)}h", *resampled)


async def main(wav_path: str):
    print(f"连接 {WS_URL}")
    print(f"音频文件: {wav_path}")

    pcm, sr = read_wav_pcm16(wav_path)
    if sr != 16000:
        print(f"采样率 {sr}Hz -> 重采样到 16000Hz")
        pcm = resample_nearest(pcm, sr)

    session_id = str(uuid.uuid4())

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({
            "type": "start",
            "sessionId": session_id,
            "terminalId": "test-terminal-01",
        }))
        print(f"已发送 start，sessionId={session_id}")

        chunk_bytes = CHUNK_FRAMES * 2
        offset = 0
        done = asyncio.Event()

        async def recv_loop():
            async for msg in ws:
                data = json.loads(msg)
                t = data.get("type")
                if t == "partial":
                    stable = data.get("stable", False)
                    print(f"  [中间{'·稳定' if stable else ''}] {data.get('text', '')}")
                elif t == "final":
                    print(f"  [最终] {data.get('text', '')}  fallback={data.get('fallbackFromPartial', False)}")
                    done.set()
                elif t == "error":
                    print(f"  [错误] {data}")
                    done.set()
                else:
                    print(f"  <- {data}")

        recv_task = asyncio.create_task(recv_loop())

        while offset < len(pcm):
            chunk = pcm[offset:offset + chunk_bytes]
            await ws.send(chunk)
            offset += chunk_bytes
            await asyncio.sleep(0.1)  # 实时速度，100ms/chunk

        await asyncio.sleep(0.5)  # 等云端处理完最后一帧
        await ws.send(json.dumps({"type": "stop"}))
        print("已发送 stop，等待识别结果...")

        try:
            await asyncio.wait_for(done.wait(), timeout=30)
        except asyncio.TimeoutError:
            print("等待超时")

        recv_task.cancel()

    print("完成")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WAV
    asyncio.run(main(path))
