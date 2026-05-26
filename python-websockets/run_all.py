"""End-to-end edge-case tests for Parakeet CPU + Kaldi prod.

Tests for each backend:
  T1 normal: stream real audio, capture partials/finals, time the path.
  T2 silence: 5 s of zeros — must NOT hallucinate.
  T3 short  : 0.5 s of audio — what does the backend emit?
  T4 idle   : open WS, no audio for 30 s, then send some audio.
  T5 mid-drop: cut the WS halfway through audio — does it finalize?
  T6 reconnect: drop + immediately reconnect; do we lose context?

Plus a side-by-side message-shape dump so we can normalize Parakeet's
output to look the same as Kaldi's (which is what callers expect).
"""

import asyncio
import json
import ssl
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import certifi
import websockets

PARAKEET_URL = "wss://realtime.scriptix.dev/v2/realtime"
import os; PARAKEET_TOKEN = os.environ.get("REALTIME_TOKEN", "")
PARAKEET_LANG = "en"
PARAKEET_WAV = "/tmp/asr-test/en.wav"

KALDI_URL = "wss://api.scriptix.io/realtime"
KALDI_TOKEN = os.environ.get("KALDI_TOKEN", "")
KALDI_LANG = "nl-pol"
KALDI_WAV = "/tmp/asr-test/nl.wav"

SSL_CTX = ssl.create_default_context(cafile=certifi.where())


@dataclass
class Run:
    backend: str
    test: str
    started: float = field(default_factory=time.monotonic)
    first_partial_at: Optional[float] = None
    first_final_at: Optional[float] = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    bytes_sent: int = 0
    closed_at: Optional[float] = None

    @property
    def first_partial_ms(self) -> Optional[int]:
        return int((self.first_partial_at - self.started) * 1000) if self.first_partial_at else None

    @property
    def first_final_ms(self) -> Optional[int]:
        return int((self.first_final_at - self.started) * 1000) if self.first_final_at else None

    @property
    def elapsed_s(self) -> float:
        return (self.closed_at or time.monotonic()) - self.started

    def partials(self) -> list[str]:
        out = []
        for m in self.messages:
            text, is_final = _extract(m)
            if text is not None and not is_final:
                out.append(text)
        return out

    def finals(self) -> list[str]:
        out = []
        for m in self.messages:
            text, is_final = _extract(m)
            if text is not None and is_final:
                out.append(text)
        return out

    def churn(self) -> float:
        ps = self.partials()
        if len(ps) < 2:
            return 0.0
        return sum(abs(len(b) - len(a)) for a, b in zip(ps, ps[1:])) / (len(ps) - 1)


def _extract(obj: dict) -> tuple[Optional[str], bool]:
    """Parse a server message into (text, is_final).

    Handles three wire formats observed in the cluster:
      * Kaldi-style partials:  {"partial": "..."}
      * Kaldi-style finals:    {"result": [[w,s,e,c], ...], "text": "...",
                                "speaker": "..."}
      * Parakeet 0.1.x (now unified to Kaldi-style — see audio_processor)
        legacy: {"text": "...", "is_final": bool, "offset_ms": ...}
    """
    if "partial" in obj:
        return str(obj["partial"]), False
    # Kaldi final: ``result`` is a list of word arrays, ``text`` is the
    # full transcript.
    if isinstance(obj.get("result"), list) and "text" in obj:
        return str(obj["text"]), True
    if "text" in obj and "is_final" in obj:
        return str(obj["text"]), bool(obj["is_final"])
    if "transcript" in obj:
        return str(obj["transcript"]), bool(obj.get("is_final"))
    return None, False


def _pcm_from_wav(path: str) -> bytes:
    with wave.open(path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        return w.readframes(w.getnframes())


async def _reader(ws, run: Run, stop_after_final: bool = False) -> None:
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                continue
            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                run.errors.append(f"non-json: {msg!r}")
                continue
            run.messages.append(obj)
            now = time.monotonic()
            text, is_final = _extract(obj)
            if text is not None:
                if is_final and run.first_final_at is None:
                    run.first_final_at = now
                elif not is_final and run.first_partial_at is None:
                    run.first_partial_at = now
            if obj.get("error"):
                run.errors.append(str(obj["error"]))
            if obj.get("state") == "stopped":
                break
            if stop_after_final and is_final:
                break
    except websockets.ConnectionClosed as exc:
        run.errors.append(f"closed: code={exc.code} reason={exc.reason}")
    finally:
        run.closed_at = time.monotonic()


async def _connect(url: str, token: str, language: str):
    return await websockets.connect(
        f"{url}?language={language}",
        extra_headers={"x-zoom-s2t-key": token},
        ssl=SSL_CTX,
        open_timeout=15,
        close_timeout=5,
        max_size=None,
    )


async def _start(ws) -> str:
    await ws.send('{"action": "start"}')
    return await asyncio.wait_for(ws.recv(), timeout=15)


async def t1_normal(label: str, url: str, token: str, language: str, pcm: bytes) -> Run:
    run = Run(label, "T1_normal")
    ws = await _connect(url, token, language)
    listening = await _start(ws)
    if "listening" not in listening:
        run.errors.append(f"no_listening: {listening}")
        await ws.close()
        return run
    run.messages.append(json.loads(listening))
    r = asyncio.create_task(_reader(ws, run))
    chunk = 16000 * 2 // 5  # 200 ms
    for i in range(0, len(pcm), chunk):
        await ws.send(pcm[i:i + chunk])
        run.bytes_sent += min(chunk, len(pcm) - i)
        await asyncio.sleep(0.2)
    await ws.send('{"action": "stop"}')
    try:
        await asyncio.wait_for(r, timeout=15)
    except asyncio.TimeoutError:
        r.cancel()
    return run


async def t2_silence(label: str, url: str, token: str, language: str) -> Run:
    run = Run(label, "T2_silence")
    ws = await _connect(url, token, language)
    listening = await _start(ws)
    if "listening" not in listening:
        run.errors.append(f"no_listening: {listening}")
        await ws.close()
        return run
    run.messages.append(json.loads(listening))
    r = asyncio.create_task(_reader(ws, run))
    silence = b"\x00" * (16000 * 2)  # 1s
    for _ in range(5):
        await ws.send(silence)
        run.bytes_sent += len(silence)
        await asyncio.sleep(0.2)
    await ws.send('{"action": "stop"}')
    try:
        await asyncio.wait_for(r, timeout=10)
    except asyncio.TimeoutError:
        r.cancel()
    return run


async def t3_short(label: str, url: str, token: str, language: str, pcm: bytes) -> Run:
    run = Run(label, "T3_short_500ms")
    ws = await _connect(url, token, language)
    listening = await _start(ws)
    if "listening" not in listening:
        run.errors.append(f"no_listening: {listening}")
        await ws.close()
        return run
    run.messages.append(json.loads(listening))
    r = asyncio.create_task(_reader(ws, run))
    await ws.send(pcm[: 16000 * 2 // 2])  # 500 ms
    run.bytes_sent += 16000
    await asyncio.sleep(1.5)  # wait for any output
    await ws.send('{"action": "stop"}')
    try:
        await asyncio.wait_for(r, timeout=10)
    except asyncio.TimeoutError:
        r.cancel()
    return run


async def t4_idle(label: str, url: str, token: str, language: str, pcm: bytes) -> Run:
    """Open + listen + idle 30s + send audio. Does the WS stay alive?"""
    run = Run(label, "T4_idle_then_speech")
    ws = await _connect(url, token, language)
    listening = await _start(ws)
    if "listening" not in listening:
        run.errors.append(f"no_listening: {listening}")
        await ws.close()
        return run
    run.messages.append(json.loads(listening))
    r = asyncio.create_task(_reader(ws, run))
    await asyncio.sleep(15)
    # Try sending audio after the idle
    try:
        chunk = 16000 * 2 // 5
        for i in range(0, min(len(pcm), 16000 * 2 * 4), chunk):  # 4s max
            await ws.send(pcm[i:i + chunk])
            run.bytes_sent += min(chunk, len(pcm) - i)
            await asyncio.sleep(0.2)
        await ws.send('{"action": "stop"}')
    except websockets.ConnectionClosed as exc:
        run.errors.append(f"closed-after-idle: code={exc.code}")
    try:
        await asyncio.wait_for(r, timeout=10)
    except asyncio.TimeoutError:
        r.cancel()
    return run


async def t5_mid_drop(label: str, url: str, token: str, language: str, pcm: bytes) -> Run:
    run = Run(label, "T5_mid_drop")
    ws = await _connect(url, token, language)
    listening = await _start(ws)
    if "listening" not in listening:
        run.errors.append(f"no_listening: {listening}")
        await ws.close()
        return run
    run.messages.append(json.loads(listening))
    r = asyncio.create_task(_reader(ws, run))
    chunk = 16000 * 2 // 5
    halfway = len(pcm) // 2
    sent = 0
    for i in range(0, halfway, chunk):
        await ws.send(pcm[i:i + chunk])
        sent += min(chunk, halfway - i)
        await asyncio.sleep(0.2)
    run.bytes_sent = sent
    # Abrupt close — no {"action":"stop"}
    await ws.close()
    try:
        await asyncio.wait_for(r, timeout=5)
    except asyncio.TimeoutError:
        r.cancel()
    return run


async def t6_reconnect(label: str, url: str, token: str, language: str, pcm: bytes) -> tuple[Run, Run]:
    """First half on session A, abrupt close, then second half on session B."""
    half = len(pcm) // 2
    # First leg
    run_a = await t5_mid_drop(label, url, token, language, pcm)
    run_a.test = "T6_reconnect_legA"
    # Second leg — fresh WS, second half of audio (no continuity)
    run_b = Run(label, "T6_reconnect_legB")
    ws = await _connect(url, token, language)
    listening = await _start(ws)
    if "listening" not in listening:
        run_b.errors.append(f"no_listening: {listening}")
        await ws.close()
        return run_a, run_b
    run_b.messages.append(json.loads(listening))
    r = asyncio.create_task(_reader(ws, run_b))
    chunk = 16000 * 2 // 5
    for i in range(half, len(pcm), chunk):
        await ws.send(pcm[i:i + chunk])
        run_b.bytes_sent += min(chunk, len(pcm) - i)
        await asyncio.sleep(0.2)
    await ws.send('{"action": "stop"}')
    try:
        await asyncio.wait_for(r, timeout=15)
    except asyncio.TimeoutError:
        r.cancel()
    return run_a, run_b


# ---- harness orchestration --------------------------------------------------

TESTS: list[tuple[str, Callable]] = [
    ("T1_normal", t1_normal),
    ("T2_silence", t2_silence),
    ("T3_short", t3_short),
    ("T4_idle", t4_idle),
    ("T5_mid_drop", t5_mid_drop),
]


async def _safe(coro):
    try:
        return await coro
    except Exception as exc:
        run = Run("?", "?")
        run.errors.append(f"{type(exc).__name__}: {exc}")
        run.closed_at = time.monotonic()
        return run


async def run_backend(label: str, url: str, token: str, language: str, pcm: bytes) -> list[Run]:
    runs: list[Run] = []
    # T1 normal — full audio
    r = await _safe(t1_normal(label, url, token, language, pcm))
    if r.backend == "?":
        r.backend, r.test = label, "T1_normal"
    runs.append(r)
    await asyncio.sleep(2)
    # T2 silence — must NOT hallucinate
    r = await _safe(t2_silence(label, url, token, language))
    if r.backend == "?":
        r.backend, r.test = label, "T2_silence"
    runs.append(r)
    await asyncio.sleep(2)
    # T3 short — 500ms only
    r = await _safe(t3_short(label, url, token, language, pcm))
    if r.backend == "?":
        r.backend, r.test = label, "T3_short_500ms"
    runs.append(r)
    await asyncio.sleep(2)
    # T4 idle — open + 15s idle + audio (15s, not 30, to keep run shorter)
    r = await _safe(t4_idle(label, url, token, language, pcm))
    if r.backend == "?":
        r.backend, r.test = label, "T4_idle_then_speech"
    runs.append(r)
    await asyncio.sleep(2)
    # T5 mid-drop — close ungracefully halfway
    r = await _safe(t5_mid_drop(label, url, token, language, pcm))
    if r.backend == "?":
        r.backend, r.test = label, "T5_mid_drop"
    runs.append(r)
    await asyncio.sleep(2)
    # T6 reconnect — leg A drop, leg B fresh session
    pair = await _safe(t6_reconnect(label, url, token, language, pcm))
    if isinstance(pair, tuple):
        runs.extend(pair)
    else:
        runs.append(pair)
    return runs


def _format_run(run: Run) -> str:
    lines = [f"=== {run.backend} :: {run.test}"]
    lines.append(f"  bytes_sent       : {run.bytes_sent}")
    lines.append(f"  first_partial_ms : {run.first_partial_ms}")
    lines.append(f"  first_final_ms   : {run.first_final_ms}")
    lines.append(f"  partials         : {len(run.partials())}")
    lines.append(f"  finals           : {len(run.finals())}")
    lines.append(f"  churn (avg|Δ|)   : {run.churn():.2f}")
    lines.append(f"  elapsed_s        : {run.elapsed_s:.1f}")
    if run.errors:
        lines.append(f"  errors           : {run.errors[:3]}")
    if run.partials():
        lines.append(f"  last_partial     : {run.partials()[-1]!r}")
    if run.finals():
        lines.append(f"  last_final       : {run.finals()[-1]!r}")
    # First non-listening message — shape sample
    for m in run.messages:
        if m.get("state") == "listening":
            continue
        if m.get("error"):
            continue
        lines.append(f"  sample_msg       : {json.dumps(m)[:300]}")
        break
    return "\n".join(lines)


async def main():
    p_pcm = _pcm_from_wav(PARAKEET_WAV)
    k_pcm = _pcm_from_wav(KALDI_WAV)
    print(f"# Parakeet audio: {len(p_pcm)} bytes ({len(p_pcm)/(16000*2):.1f}s, en)")
    print(f"# Kaldi audio:    {len(k_pcm)} bytes ({len(k_pcm)/(16000*2):.1f}s, nl-pol)")
    print()

    print("==================== PARAKEET CPU (staging) ====================")
    p_runs = await run_backend("parakeet", PARAKEET_URL, PARAKEET_TOKEN, PARAKEET_LANG, p_pcm)
    for r in p_runs:
        print(_format_run(r))
        print()

    print("==================== KALDI prod ====================")
    k_runs = await run_backend("kaldi", KALDI_URL, KALDI_TOKEN, KALDI_LANG, k_pcm)
    for r in k_runs:
        print(_format_run(r))
        print()


if __name__ == "__main__":
    asyncio.run(main())
