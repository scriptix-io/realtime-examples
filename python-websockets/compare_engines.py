"""
Side-by-side reliability comparison: Parakeet CPU (v2) vs Kaldi (legacy).

Drives the *same* audio source through both backends and prints a
timing table — first-partial latency, partial count, partial-churn,
final transcript, dropped frames, reconnect behavior.

USAGE:
  # 1. From an audio FILE:
  REALTIME_TOKEN=...  KALDI_TOKEN=...  python compare_engines.py \\
      --language en --file /path/to/16k_mono.wav

  # 2. From a live HTTP stream (ffmpeg pipes to both):
  REALTIME_TOKEN=...  KALDI_TOKEN=...  python compare_engines.py \\
      --language en --stream https://stream.bnr.nl/bnr_mp3_128_20 \\
      --seconds 30

  # 3. Override endpoints (defaults: staging Parakeet + zoommedia Kaldi):
  --parakeet-url wss://realtime.scriptix.dev/v2/realtime \\
  --kaldi-url    wss://api.zoommedia.ai/realtime

NOTES:
  - Audio MUST be 16 kHz mono signed-16-bit PCM. ``--stream`` runs
    ffmpeg locally to do the transcode. ``--file`` assumes already-
    correct WAV (no resample done here).
  - Set ``--no-kaldi`` to benchmark just the new engine.
  - Set ``--no-parakeet`` to baseline Kaldi alone.

Requires: websockets, python>=3.10. ffmpeg only if ``--stream``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from dataclasses import dataclass, field
from typing import Optional

import websockets


PARAKEET_DEFAULT = "wss://realtime.scriptix.dev/v2/realtime"
KALDI_DEFAULT = "wss://api.zoommedia.ai/realtime"


@dataclass
class RunStats:
    backend: str
    started_at: float
    first_partial_at: Optional[float] = None
    first_final_at: Optional[float] = None
    partials: int = 0
    finals: int = 0
    bytes_sent: int = 0
    partial_texts: list[str] = field(default_factory=list)
    final_texts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    closed_at: Optional[float] = None

    def first_partial_ms(self) -> Optional[int]:
        if self.first_partial_at is None:
            return None
        return int((self.first_partial_at - self.started_at) * 1000)

    def first_final_ms(self) -> Optional[int]:
        if self.first_final_at is None:
            return None
        return int((self.first_final_at - self.started_at) * 1000)

    def churn(self) -> float:
        """Average partial length change between consecutive partials.

        A high churn means the engine rewrites a lot — bad for UX. Kaldi
        sits near 0.05 (very stable). Streaming Whisper can hit 0.5+.
        """
        if len(self.partial_texts) < 2:
            return 0.0
        total = 0
        for a, b in zip(self.partial_texts, self.partial_texts[1:]):
            total += abs(len(a) - len(b))
        return total / (len(self.partial_texts) - 1)


async def _reader(ws, stats: RunStats) -> None:
    try:
        async for msg in ws:
            if isinstance(msg, bytes):
                continue
            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                continue
            now = time.monotonic()
            # v2 / Parakeet protocol: {"transcript": "...", "is_final": bool, ...}
            # Kaldi legacy:           {"result": {"hypotheses": [{"transcript": "..."}], "final": bool}}
            text, is_final = _extract_text(obj)
            if text is None:
                # Control messages (state, error, listening, ...)
                if obj.get("error"):
                    stats.errors.append(str(obj["error"]))
                continue
            if is_final:
                stats.finals += 1
                if stats.first_final_at is None:
                    stats.first_final_at = now
                stats.final_texts.append(text)
            else:
                stats.partials += 1
                if stats.first_partial_at is None:
                    stats.first_partial_at = now
                stats.partial_texts.append(text)
    except websockets.ConnectionClosed:
        pass
    finally:
        stats.closed_at = time.monotonic()


def _extract_text(obj: dict) -> tuple[Optional[str], bool]:
    # Unified Kaldi-shape (current realtime engine): partials are
    # {"partial": "..."}, finals are {"result": [[w,s,e,c],...], "text": "..."}.
    if "partial" in obj:
        return str(obj["partial"]), False
    if "text" in obj and isinstance(obj.get("result"), list):
        return str(obj["text"]), True
    # v2 Whisper format: {"transcript": "...", "is_final": bool}
    if "transcript" in obj:
        return str(obj["transcript"]), bool(obj.get("is_final"))
    # Legacy Kaldi gstreamer: {"result": {"hypotheses": [{"transcript": ...}], "final": bool}}
    result = obj.get("result")
    if isinstance(result, dict):
        hyps = result.get("hypotheses") or []
        if hyps and isinstance(hyps, list):
            return str(hyps[0].get("transcript", "")), bool(result.get("final"))
    return None, False


async def _writer(ws, pcm: bytes, stats: RunStats, chunk_ms: int = 200) -> None:
    chunk = int(16000 * 2 * (chunk_ms / 1000))  # 16-bit samples
    sleep_s = chunk_ms / 1000
    for i in range(0, len(pcm), chunk):
        await ws.send(pcm[i:i + chunk])
        stats.bytes_sent += min(chunk, len(pcm) - i)
        await asyncio.sleep(sleep_s)
    await ws.send('{"action": "stop"}')


async def _drive(url: str, token: str, language: str, pcm: bytes, label: str) -> RunStats:
    stats = RunStats(backend=label, started_at=time.monotonic())
    full_url = f"{url}?language={language}"
    # websockets >=14 renamed ``extra_headers`` -> ``additional_headers``.
    import inspect

    hdr_kw = (
        "additional_headers"
        if "additional_headers" in inspect.signature(websockets.connect).parameters
        else "extra_headers"
    )
    connect_kwargs = {
        hdr_kw: {"x-zoom-s2t-key": token},
        "open_timeout": 10,
        "close_timeout": 5,
        "max_size": None,
    }
    try:
        async with websockets.connect(full_url, **connect_kwargs) as ws:
            await ws.send('{"action": "start"}')
            try:
                listening = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                stats.errors.append("listening_timeout")
                return stats
            if "listening" not in str(listening):
                stats.errors.append(f"no_listening: {listening!r}")
                return stats
            r = asyncio.create_task(_reader(ws, stats))
            w = asyncio.create_task(_writer(ws, pcm, stats))
            await w
            try:
                await asyncio.wait_for(r, timeout=15)
            except asyncio.TimeoutError:
                r.cancel()
    except Exception as exc:
        stats.errors.append(f"{type(exc).__name__}: {exc}")
    return stats


def _load_pcm_from_file(path: str) -> bytes:
    with wave.open(path) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(
                f"need 16 kHz mono s16le wav; got "
                f"{w.getframerate()}Hz {w.getnchannels()}ch {w.getsampwidth()*8}-bit"
            )
        return w.readframes(w.getnframes())


def _load_pcm_from_stream(url: str, seconds: int) -> bytes:
    import subprocess

    cmd = [
        "ffmpeg", "-loglevel", "panic",
        "-i", url, "-t", str(seconds),
        "-ac", "1", "-ar", "16000",
        "-acodec", "pcm_s16le", "-f", "s16le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    return proc.stdout


def _print_row(stats: RunStats) -> None:
    fp = stats.first_partial_ms()
    ff = stats.first_final_ms()
    elapsed = (stats.closed_at or time.monotonic()) - stats.started_at
    final = stats.final_texts[-1] if stats.final_texts else (
        stats.partial_texts[-1] if stats.partial_texts else "<no output>"
    )
    print(f"=== {stats.backend} ===")
    print(f"  first_partial_ms : {fp}")
    print(f"  first_final_ms   : {ff}")
    print(f"  partials         : {stats.partials}")
    print(f"  finals           : {stats.finals}")
    print(f"  churn (avg|Δ|)   : {stats.churn():.2f}")
    print(f"  bytes_sent       : {stats.bytes_sent}")
    print(f"  elapsed_s        : {elapsed:.1f}")
    if stats.errors:
        print(f"  errors           : {stats.errors}")
    print(f"  final            : {final!r}")
    print()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--language", default="en")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="16 kHz mono s16le wav")
    src.add_argument("--stream", help="HTTP audio stream URL (ffmpeg transcodes)")
    p.add_argument("--seconds", type=int, default=30, help="--stream duration")
    p.add_argument("--parakeet-url", default=PARAKEET_DEFAULT)
    p.add_argument("--kaldi-url", default=KALDI_DEFAULT)
    p.add_argument("--no-parakeet", action="store_true")
    p.add_argument("--no-kaldi", action="store_true")
    args = p.parse_args()

    if args.file:
        pcm = _load_pcm_from_file(args.file)
    else:
        pcm = _load_pcm_from_stream(args.stream, args.seconds)
    print(f"audio: {len(pcm)} bytes ({len(pcm)/(16000*2):.1f}s)")
    print()

    parakeet_token = os.environ.get("REALTIME_TOKEN", "")
    kaldi_token = os.environ.get("KALDI_TOKEN", "")

    async def go():
        tasks = []
        if not args.no_parakeet:
            if not parakeet_token:
                print("ERROR: REALTIME_TOKEN not set (Parakeet)", file=sys.stderr)
                sys.exit(2)
            tasks.append(_drive(args.parakeet_url, parakeet_token, args.language, pcm, "parakeet-cpu"))
        if not args.no_kaldi:
            if not kaldi_token:
                print("ERROR: KALDI_TOKEN not set", file=sys.stderr)
                sys.exit(2)
            tasks.append(_drive(args.kaldi_url, kaldi_token, args.language, pcm, "kaldi-legacy"))
        return await asyncio.gather(*tasks)

    results = asyncio.run(go())
    for r in results:
        _print_row(r)


if __name__ == "__main__":
    main()
