"""
测试唤醒 WebSocket
用法: python test_wakeup.py [wav文件路径]
默认使用 sherpa-onnx 模型目录内的测试音频
"""
import asyncio
import json
import struct
import sys
import wave

import websockets

WS_URL = "ws://localhost:9000/wakeup"
DEFAULT_WAV = "./test.wav"
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

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({
            "type": "start",
            "terminalId": "test-terminal-01",
            "wakeWord": "小万小万",
        }))
        print("已发送 start，开始推送音频...")

        chunk_bytes = CHUNK_FRAMES * 2
        offset = 0
        detected = False

        async def recv_loop():
            nonlocal detected
            async for msg in ws:
                data = json.loads(msg)
                print(f"  <- {data}")
                if data.get("type") == "wake_detected":
                    detected = True

        recv_task = asyncio.create_task(recv_loop())

        while offset < len(pcm):
            chunk = pcm[offset:offset + chunk_bytes]
            await ws.send(chunk)
            offset += chunk_bytes
            await asyncio.sleep(0.05)

        print("音频推送完毕，等待 2 秒...")
        await asyncio.sleep(2)
        recv_task.cancel()

    if detected:
        print("\n✓ 唤醒词检测成功")
    else:
        print("\n✗ 未检测到唤醒词")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WAV
    asyncio.run(main(path))
