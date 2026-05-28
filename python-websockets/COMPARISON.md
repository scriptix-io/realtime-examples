# Parakeet CPU vs Kaldi — reliability gap analysis

Companion to `compare_engines.py`. The harness measures latency / partial
churn / final WER end-to-end; this doc lists the **code-level gaps**
that surfaced when wiring the new Parakeet CPU engine against Kaldi's
years-tuned behavior.

Updated 2026-05-26. Ranked by impact on "reliable as Kaldi".

## P0 — must close before broad rollout

### 1. No session resume on reconnect
- **Kaldi**: client reconnects with the same session UUID; server keeps
  the decoding session alive for ~30 s after WebSocket close and rebinds
  on reconnect. Transcript continuity across drops.
- **Parakeet (today)**: every reconnect = fresh `OnlineStream`. Words
  spoken just before the drop are lost. Even worse, sliding-buffer
  replay isn't wired so the client sees a hard cut in offset_ms.
- **Fix**: implement the design in `HANDOFF.md` — keyed session cache in
  `SocketManager`, replay window from `SlidingWindowBuffer`, monotonic
  anchor in place of `time.time()` for `_session_start_ms`.

### 2. No graceful drain on SIGTERM
- **Kaldi**: pod termination flushes pending hypotheses and closes the
  upstream socket cleanly; client gets a final.
- **Parakeet (today)**: `ParakeetEngine.finalize()` is idempotent but the
  FastAPI shutdown hook only cleans up the Whisper model. Active streams
  on the worker process get killed.
- **Fix**: extend the `lifespan` shutdown branch — iterate the
  `SocketManager.client_connections`, call `engine.finalize()` per stream,
  then close WebSockets with a clean close code (1001 going-away).

### 3. Backpressure / decode-lag
- **Kaldi**: blocks the producer when decoding queue fills; client sees
  natural backpressure on `send()`.
- **Parakeet (today)**: `insert_audio` always succeeds; if decode can't
  keep up under multi-session load on a small CPU pod, latency grows
  unbounded. No drop policy, no warning to client.
- **Fix**: cap `_stream` buffered audio (say, 5 s); drop oldest on
  overflow + emit a `{"warning": "decode_lag"}` so client can throttle.
  Wire `PARAKEET_DECODE_WORKERS` into an explicit
  `asyncio.get_event_loop().set_default_executor(ThreadPoolExecutor(N))`
  at startup so concurrent sessions share a sized pool.

## P1 — close before declaring "as reliable as Kaldi"

### 4. Partial churn / hypothesis rewrites
- **Kaldi**: very low — partials extend, rarely rewrite. Average per-
  partial Δlength ~0.05 chars.
- **Parakeet streaming**: TDT decoder *can* rewrite recent words when
  later audio disambiguates. Better than Whisper but not Kaldi-stable.
  Today we dedup only consecutive-identical partials.
- **Mitigation**: track stable prefix (longest common prefix across the
  last N partials) and only emit when prefix grows. The
  `transcription/online_processor.py` scaffold already implements
  "local agreement" for the same purpose — wire it as a post-filter on
  Parakeet's output.

### 5. Endpoint detection tuning
- **Kaldi**: per-language silence + utterance-length thresholds tuned
  on real customer data, years.
- **Parakeet (today)**: hardcoded rule1=2.4s, rule2=1.2s,
  rule3=20s (config-driven via `PARAKEET_RULE*_SILENCE`). Generic
  defaults. Aggressive enough for clean audio, weak for noisy /
  call-center.
- **Fix**: build a per-language profile dict
  (`{"en": {"rule1": 2.0, ...}, "nl": {...}}`) and pick at session start.

### 6. Word-level confidence
- **Kaldi**: per-word posterior probability in final results.
- **Parakeet (today)**: `_word_from_token` hardcodes `confidence=1.0`.
  Sherpa-onnx exposes `result.ys_probs` (CTC posteriors) and
  `result.lm_probs` when LM fusion is on — we ignore both.
- **Fix**: map `result.ys_probs` to per-token confidence, propagate to
  `SpeechToTextResultWord.confidence`. Downstream UI and exporter already
  consume that field; today they always see 1.0 from Parakeet, hurting
  filtering and quality dashboards.

### 7. Per-language model
- **Kaldi**: 14 per-language models in the `asr` namespace, picked by
  `LanguageConfig.asr_url`.
- **Parakeet streaming (today)**: English-only bundle. Non-English
  sessions fall back to Whisper via the new `pick_endpoint(language)`
  selector — that's correct routing but it means we cannot match Kaldi
  on Dutch, German, French, etc. until a multilingual streaming Parakeet
  bundle ships (tracked in HANDOFF "Open questions").

## P2 — nice to have, not blocking parity

### 8. Server-side keepalive / ping
- Kaldi sends `{"state": "listening"}` pulses; idle proxies stay open.
  Our v2 protocol relies on the underlying WS ping/pong. Fine in cluster
  but some customer firewalls eat WS pings — `feat(idle)` already has the
  watchdog, but a 60 s `{"state": "ping"}` from the server would be
  belt-and-braces.

### 9. Speaker count metadata
- Kaldi returns `result.speaker` (rough diarization). Parakeet doesn't.
  Realtime currently sets a static `"Speaker 1"` for compatibility.
  Diarization on streaming CPU isn't realistic; document and move on.

### 10. Audio normalization / AGC
- Kaldi's frontend has gain normalization tuned per language. sherpa-onnx
  has `normalize_samples=True` (default, on) — we're fine here.

## What the harness measures

`compare_engines.py` records per backend:

| metric | what it means | Kaldi typical | Parakeet target |
|---|---|---|---|
| `first_partial_ms` | chunk-in → first partial out | 150–300 ms | < 600 ms |
| `first_final_ms`   | chunk-in → first final | 800–1500 ms | < 1500 ms |
| `partials`         | total partial messages | many | many |
| `churn`            | avg `Δlen` between consecutive partials | ~0.05 | < 0.5 |
| `finals`           | total finals | 1 per utterance | same |
| `errors`           | server / WS errors | 0 | 0 |

Run with the same audio source to compare side-by-side. Tokens come from
your usual realtime API key (the v2 protocol uses `x-zoom-s2t-key`).

## What this analysis does NOT cover

- WER on real audio — needs a labeled corpus; budget a separate eval.
- Concurrent-session scaling (50+ active streams on one pod). Parakeet
  decode is CPU-bound; Kaldi GPU-or-CPU per language. Different shape.
- Cold-start latency — already mitigated via `warmup()` at FastAPI
  lifespan startup. First-customer hit is now sub-second.
