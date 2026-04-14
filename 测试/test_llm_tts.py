"""
测试 LLM 和 TTS 接口
用法: python test_llm_tts.py
"""
import asyncio
import json
import sys
import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_URL = "http://localhost:9000"
_NO_PROXY = httpx.AsyncHTTPTransport()


async def test_llm(text: str = "你好，请介绍一下你自己"):
    url = f"{BASE_URL}/v1/chat/completions"
    payload = {
        "model": "qwen-plus",
        "messages": [{"role": "user", "content": text}],
        "stream": False,
    }
    print(f"\n[LLM] 请求: {text}")
    async with httpx.AsyncClient(timeout=30, transport=_NO_PROXY) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            print(f"[LLM] 错误 {resp.status_code}: {resp.text}")
            return None
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        print(f"[LLM] 响应: {content}")
        return content


async def test_llm_stream(text: str = "用一句话介绍医院资产管理"):
    url = f"{BASE_URL}/v1/chat/completions"
    payload = {
        "model": "qwen-plus",
        "messages": [{"role": "user", "content": text}],
        "stream": True,
    }
    print(f"\n[LLM-Stream] 请求: {text}")
    print("[LLM-Stream] 响应: ", end="", flush=True)
    full = ""
    async with httpx.AsyncClient(timeout=30, transport=_NO_PROXY) as client:
        async with client.stream("POST", url, json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                print(f"\n[LLM-Stream] 错误 {resp.status_code}: {body.decode()}")
                return ""
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                    delta = data["choices"][0]["delta"].get("content", "")
                    print(delta, end="", flush=True)
                    full += delta
                except Exception:
                    pass
    print()
    return full


async def test_tts(text: str = "你好，欢迎使用医院智能语音助手"):
    url = f"{BASE_URL}/tts/synthesize"
    payload = {"text": text, "speed": 1.0}
    print(f"\n[TTS] 请求: {text}")
    async with httpx.AsyncClient(timeout=30, transport=_NO_PROXY) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            print(f"[TTS] 错误 {resp.status_code}: {resp.text}")
            return None
        audio_bytes = resp.content
        out_file = "./tts_output.mp3"
        with open(out_file, "wb") as f:
            f.write(audio_bytes)
        print(f"[TTS] 成功，音频大小: {len(audio_bytes)} bytes，已保存到 {out_file}")
        return audio_bytes


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode in ("llm", "all"):
        await test_llm()

    if mode in ("llm-stream", "all"):
        await test_llm_stream()

    if mode in ("tts", "all"):
        await test_tts()


if __name__ == "__main__":
    asyncio.run(main())
